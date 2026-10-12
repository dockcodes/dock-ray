"""Opt-in production dependency inventory; metadata only, never pip or imports."""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import os
import platform
import re
import site
import stat
import sysconfig
import tempfile
import time
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from .models import DEFAULT_URL

logger = logging.getLogger("dock_ray")
SDK_VERSION = "1.5.0"


def normalize_environment(value):
    if value in ("prod", "production"):
        return "prod"
    if value in ("dev", "development", "staging", "preview", "test", "local"):
        return "dev"
    raise ValueError("Unsupported inventory environment")


def _read(path, limit=1048576):
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Invalid metadata file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Metadata limit exceeded")
        return data
    finally:
        os.close(fd)


def _name(value):
    if not isinstance(value, str) or len(value) > 191 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", value):
        raise ValueError("Invalid package name")
    return str(canonicalize_name(value))


def _requirements(root, path, visited=None, stack=None):
    visited = set() if visited is None else visited
    stack = set() if stack is None else stack
    if path.is_symlink() or path.suffix not in (".txt", ".in"):
        raise ValueError("Invalid requirements file")
    path = path.resolve(strict=True)
    path.relative_to(root)
    if path in stack or len(visited) >= 16:
        raise ValueError("Recursive requirements file")
    if path in visited:
        return []
    visited.add(path)
    stack = stack | {path}
    text = _read(path).decode("utf-8").replace("\r\n", "\n").replace("\\\n", " ")
    result = []
    for line in text.splitlines():
        line = re.split(r"\s+#", line, maxsplit=1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r ", "--requirement ")):
            relative = line.split(None, 1)[1]
            if ".." in Path(relative).parts or Path(relative).is_absolute():
                raise ValueError("Invalid requirements include")
            result.extend(_requirements(root, path.parent / relative, visited, stack))
            continue
        # Installer options are never executed or sent. Constraints and editable
        # installs need an explicit production manifest, rather than a guessed graph.
        if line.startswith(("--index-url ", "--extra-index-url ", "--trusted-host ", "--require-hashes")):
            continue
        if line.startswith("-"):
            raise ValueError("Unsupported requirements option")
        line = re.sub(r"\s+--hash=sha256:[a-fA-F0-9]{64}", "", line)
        result.append(Requirement(line))
    return result


def _roots(root, requirements_file, optional_groups):
    root_name = None
    if requirements_file:
        filename = Path(requirements_file)
        if filename.is_absolute() or ".." in filename.parts:
            raise ValueError("Invalid requirements file")
        return _requirements(root, root / filename), root_name
    manifest = root / "pyproject.toml"
    if manifest.exists():
        project = tomllib.loads(_read(manifest).decode("utf-8")).get("project", {})
        if not project and not optional_groups:
            return _requirements(root, root / "requirements.txt"), None
        if "dependencies" not in project or "dependencies" in project.get("dynamic", []):
            raise ValueError("Static production dependencies required")
        dependencies = project["dependencies"]
        if not isinstance(dependencies, list):
            raise ValueError("Invalid dependencies")
        dependencies = list(dependencies)
        for group in optional_groups:
            extra = project.get("optional-dependencies", {}).get(group)
            if not isinstance(extra, list):
                raise ValueError("Unknown optional group")
            dependencies.extend(extra)
        root_name = _name(project["name"]) if project.get("name") else None
        return [Requirement(value) for value in dependencies], root_name
    if optional_groups:
        raise ValueError("Optional groups need pyproject.toml")
    return _requirements(root, root / "requirements.txt"), root_name


def _metadata_directories():
    paths = [sysconfig.get_path("purelib"), sysconfig.get_path("platlib")]
    if site.ENABLE_USER_SITE:
        paths.append(site.getusersitepackages())
    return sorted({Path(path).resolve() for path in paths if path and Path(path).is_dir()})


def _installed():
    installed = {}
    for root in _metadata_directories():
        for count, directory in enumerate(root.iterdir()):
            if count > 10000:
                raise ValueError("Too many installed entries")
            if not directory.name.endswith(".dist-info") or directory.is_symlink() or not directory.is_dir():
                continue
            metadata = BytesParser().parsebytes(_read(directory / "METADATA"), headersonly=True)
            name = _name(metadata.get("Name"))
            version = str(Version(metadata.get("Version")))
            if len(version) > 64:
                raise ValueError("Invalid version")
            if name in installed:
                raise ValueError("Ambiguous installed distribution")
            installed[name] = (version, metadata.get_all("Requires-Dist", []), (directory / "direct_url.json").exists())
    return installed


def collect_inventory(project_directory=None, environment="production", exclude_packages=(),
                      requirements_file=None, optional_groups=()):
    """Return the installed production dependency graph from bounded metadata."""
    env = normalize_environment(environment)
    if isinstance(exclude_packages, str) or len(exclude_packages) > 1000 or any(not isinstance(x, str) or len(x) > 191 for x in exclude_packages):
        raise ValueError("Invalid exclusions")
    if isinstance(optional_groups, str) or len(optional_groups) > 100 or any(not isinstance(x, str) for x in optional_groups):
        raise ValueError("Invalid optional groups")
    root = Path(project_directory or os.getcwd()).resolve(strict=True)
    requirements, root_name = _roots(root, requirements_file, optional_groups)
    installed = _installed()
    queue = [(req, {""}) for req in requirements]
    seen = {}
    components = {}
    steps = 0
    while queue:
        req, parent_extras = queue.pop()
        steps += 1
        if steps > 20000:
            raise ValueError("Dependency graph too large")
        if req.marker and not any(req.marker.evaluate({"extra": extra}) for extra in parent_extras):
            continue
        name = _name(req.name)
        if name not in installed:
            raise ValueError("Production dependency is not installed")
        version, dependencies, direct = installed[name]
        if not req.specifier.contains(version, prereleases=True):
            raise ValueError("Installed dependency mismatch")
        requested = {_name(extra) for extra in req.extras} or {""}
        if name in seen and requested.issubset(seen[name]):
            continue
        seen.setdefault(name, set()).update(requested)
        if len(seen) > 1000:
            raise ValueError("Too many packages")
        # Local/editable/direct-URL packages and the application itself are not
        # claimed to be public PyPI releases; still follow their public dependencies.
        excluded = any(fnmatch.fnmatchcase(name, re.sub(r"[-_.]+", "-", pattern.lower())) for pattern in exclude_packages)
        if not direct and not req.url and name != root_name and not excluded:
            components[name] = {"type": "package", "slug": name, "name": name, "version": version, "active": True}
        for value in dependencies:
            queue.append((Requirement(re.sub(r"\r?\n[ \t]+", " ", value)), seen[name].copy()))
    return {"schema": 1, "platform": "python", "platform_version": platform.python_version(),
            "agent_version": SDK_VERSION, "environment": env,
            "components": [components[name] for name in sorted(components)]}


class InventoryReporter:
    def __init__(self, enabled=False, token="", private_key="", url=DEFAULT_URL,
                 environment=None, project_directory=None, state_directory=None,
                 exclude_packages=(), requirements_file=None, optional_groups=()):
        self.enabled = enabled is True
        self.token, self.private_key, self.url = token, private_key, url
        self.environment = environment if environment is not None else os.getenv("RAY_ENVIRONMENT", "production")
        self.project_directory = project_directory
        self.state_directory = Path(state_directory or Path(tempfile.gettempdir()) / f"dockray-python-inventory-{getattr(os, 'getuid', lambda: 'user')()}")
        self.exclude_packages, self.requirements_file, self.optional_groups = exclude_packages, requirements_file, optional_groups
        self._pending = self._schedule = self._stop = None

    def start(self):
        if not self.enabled or self._schedule is not None:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._stop = asyncio.Event()
        self._schedule = loop.create_task(self._run())
        return True

    async def _run(self):
        while not self._stop.is_set():
            await self.report()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=3600)
            except asyncio.TimeoutError:
                pass

    async def stop(self):
        if self._stop:
            self._stop.set()
        tasks = {task for task in (self._schedule, self._pending) if task is not None and task is not asyncio.current_task()}
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._schedule = None

    async def report(self, force=False):
        if not self.enabled:
            return {"status": "disabled"}
        if not self.token or not self.private_key:
            return {"status": "not_configured"}
        if self._pending:
            return {"status": "busy"}
        self._pending = asyncio.create_task(self._report(force))
        try:
            return await self._pending
        finally:
            self._pending = None

    async def _report(self, force):
        fd = None
        try:
            env = normalize_environment(self.environment)
            parsed = urlsplit(self.url)
            if (os.name != "posix" or parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or "?" in self.url or "#" in self.url
                or any(ord(c) <= 32 or ord(c) == 127 for c in self.url + self.private_key)
                or not isinstance(self.token, str) or len(self.token) > 255):
                return {"status": "invalid_configuration"}
            identity = hashlib.sha256(f"{self.url.rstrip('/')}|{self.token}|{env}".encode()).hexdigest()
            fd, state_file = self._lock(identity)
            if fd is None:
                return {"status": "busy"}
            try:
                state = json.loads(_read(state_file, 8192))
            except FileNotFoundError:
                state = {}
            now = int(time.time())
            if state.get("retryAt", 0) > now:
                return {"status": "backoff"}
            try:
                payload = await asyncio.to_thread(collect_inventory, self.project_directory, env,
                                                  self.exclude_packages, self.requirements_file, self.optional_groups)
                body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                if len(body) > 512000:
                    raise ValueError("Payload too large")
                digest = hashlib.sha256(body).hexdigest()
                if not force and state.get("hash") == digest and state.get("nextAt", 0) > now:
                    return {"status": "not_due"}
                status, headers, result = await asyncio.wait_for(self._send(body), timeout=8)
                if status != 200 or not isinstance(result, dict) or result.get("success") is not True:
                    retry = self._retry(headers.get("retry-after"), now)
                    self._save(state_file, {**state, "retryAt": now + retry})
                    return {"status": "failed", "http_status": status}
                interval = min(604800, max(3600, int(result.get("next_report_in", 43200))))
                self._save(state_file, {"hash": digest, "nextAt": now + interval, "retryAt": 0})
                vulnerable = result.get("vulnerable", 0)
                vulnerable = vulnerable if isinstance(vulnerable, int) and 0 <= vulnerable <= 100000 else 0
                return {"status": "sent", "components": len(payload["components"]), "vulnerable": vulnerable}
            except Exception:
                self._save(state_file, {**state, "retryAt": now + 300})
                raise
        except Exception:
            logger.warning("DockRay inventory report failed; check configuration and installed metadata.")
            return {"status": "failed"}
        finally:
            if fd is not None:
                os.close(fd)  # OS releases the cross-process lock, also after a crash.

    def _lock(self, identity):
        import fcntl
        self.state_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.state_directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("State directory must be private")
        fd = os.open(str(self.state_directory / (identity + ".lock")),
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError("State file must be private")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return None, None
            return fd, self.state_directory / (identity + ".json")
        except Exception:
            os.close(fd)
            raise

    @staticmethod
    def _save(path, value):
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _retry(value, now):
        try:
            seconds = int(value)
        except (ValueError, TypeError):
            try:
                seconds = int(parsedate_to_datetime(value).timestamp()) - now
            except (ValueError, TypeError, OverflowError):
                seconds = 300
        return min(86400, max(300, seconds))

    async def _send(self, body):
        endpoint = self.url.rstrip("/") + "/api/v1/" + quote(self.token, safe="") + "/inventory"
        async with httpx.AsyncClient(timeout=8, follow_redirects=False, verify=True, trust_env=False) as client:
            async with client.stream("POST", endpoint, content=body, headers={"Content-Type": "application/json",
                                      "Authorization": "Bearer " + self.private_key, "Accept-Encoding": "identity"}) as response:
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise ValueError("Unexpected encoded response")
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    data.extend(chunk)
                    if len(data) > 65536:
                        raise ValueError("Response too large")
                # HTTP errors need not contain JSON; preserve Retry-After without logging bodies.
                result = json.loads(data) if response.status_code == 200 else None
                return response.status_code, response.headers, result
