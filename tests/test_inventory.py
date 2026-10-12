import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from dock_ray import DockRayClient, InventoryReporter, collect_inventory
from dock_ray import inventory
from dock_ray.fastapi_integration import DockRayFastAPIMiddleware


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / 'app'
    root.mkdir()
    root.joinpath('pyproject.toml').write_text('''[project]
name="shop"
dependencies=["Foo_Bar[db]>=1.0", "unix-only; sys_platform == 'never'"]
[project.optional-dependencies]
dev=["dev-tool"]
''')
    root.joinpath('.env').write_text('PRIVATE_PASSWORD=DO_NOT_SEND')
    root.joinpath('setup.py').write_text('raise RuntimeError("DO_NOT_EXECUTE")')
    packages = tmp_path / 'site-packages'
    packages.mkdir()
    for name, version, dependencies in [('Foo_Bar', '1.0', ['child>=2', 'db-driver; extra == "db"', 'dev-tool; extra == "dev"']),
                                        ('child', '2.0', []), ('db-driver', '3.0', []), ('dev-tool', '9.0', []), ('unrelated', '8.0', [])]:
        dist = packages / f'{name}-{version}.dist-info'
        dist.mkdir()
        dist.joinpath('METADATA').write_text(f'Name: {name}\nVersion: {version}\n' + ''.join(f'Requires-Dist: {dep}\n' for dep in dependencies))
    monkeypatch.setattr(inventory, '_metadata_directories', lambda: [packages])
    return root, packages


def reporter(project, **kwargs):
    root, _ = project
    return InventoryReporter(enabled=True, token='project/token', private_key='TOP_SECRET',
                             project_directory=root, state_directory=root / 'state', **kwargs)


def test_production_graph_markers_extras_normalization_and_privacy(project):
    payload = collect_inventory(project[0])
    assert [c['slug'] for c in payload['components']] == ['child', 'db-driver', 'foo-bar']
    assert payload['components'][-1]['version'] == '1.0'
    assert payload['environment'] == 'prod'
    assert payload['platform'] == 'python'
    for forbidden in ['DO_NOT_SEND', 'DO_NOT_EXECUTE', 'dev-tool', 'unrelated', 'site_url']:
        assert forbidden not in json.dumps(payload)
    assert 'dev-tool' in [c['slug'] for c in collect_inventory(project[0], optional_groups=['dev'])['components']]
    assert 'foo-bar' not in [c['slug'] for c in collect_inventory(project[0], exclude_packages=['Foo_*'])['components']]


def test_direct_url_packages_are_not_claimed_as_pypi_releases(project):
    _, packages = project
    packages.joinpath('Foo_Bar-1.0.dist-info/direct_url.json').write_text('{"url":"https://user:SECRET@private.example/repo"}')
    payload = collect_inventory(project[0])
    assert [c['slug'] for c in payload['components']] == ['child', 'db-driver']
    assert 'SECRET' not in json.dumps(payload)


def test_requirements_include_and_mismatch_fail_closed(project):
    root, packages = project
    root.joinpath('requirements.txt').write_text('-r production.in\n--index-url https://user:SECRET@private.example\n')
    root.joinpath('production.in').write_text('Foo_Bar[db]>=1.0\n')
    assert len(collect_inventory(root, requirements_file='requirements.txt')['components']) == 3
    root.joinpath('production.in').write_text('-r requirements.txt')
    with pytest.raises(ValueError):
        collect_inventory(root, requirements_file='requirements.txt')
    root.joinpath('production.in').write_text('Foo_Bar>=2.0')
    with pytest.raises(ValueError):
        collect_inventory(root, requirements_file='requirements.txt')
    with pytest.raises(ValueError):
        collect_inventory(root, requirements_file='../outside.txt')
    packages.joinpath('Foo_Bar-1.0.dist-info/METADATA').unlink()
    with pytest.raises(FileNotFoundError):
        collect_inventory(root)


def test_dynamic_and_ambiguous_distributions_are_rejected(project):
    root, packages = project
    other = packages / 'foo_bar-2.0.dist-info'
    other.mkdir()
    other.joinpath('METADATA').write_text('Name: Foo.Bar\nVersion: 2.0\n')
    with pytest.raises(ValueError):
        collect_inventory(root)
    root.joinpath('pyproject.toml').write_text('[project]\nname="shop"\ndynamic=["dependencies"]\n')
    with pytest.raises(ValueError):
        collect_inventory(root)


@pytest.mark.asyncio
async def test_disabled_and_no_credentials_do_not_touch_files_or_network(project):
    disabled = InventoryReporter(project_directory='/does/not/exist')
    assert await disabled.report(force=True) == {'status': 'disabled'}
    assert await InventoryReporter(enabled=True).report() == {'status': 'not_configured'}
    assert not project[0].joinpath('state').exists()


@pytest.mark.asyncio
async def test_secure_delivery_private_state_throttling_and_environment_separation(project):
    with respx.mock as router:
        route = router.post('https://dockray.io/api/v1/project%2Ftoken/inventory').mock(return_value=httpx.Response(200, json={'success': True, 'vulnerable': 1, 'next_report_in': 43200}))
        instance = reporter(project)
        result = await instance.report()
        assert result == {'status': 'sent', 'components': 3, 'vulnerable': 1}
        request = route.calls[0].request
        assert request.headers['authorization'] == 'Bearer TOP_SECRET'
        assert 'TOP_SECRET' not in request.content.decode()
        assert 'site_url' not in json.loads(request.content)
        assert (await instance.report())['status'] == 'not_due'
        assert (await reporter(project, environment='prod').report())['status'] == 'not_due'
        assert (await reporter(project, environment='preview').report())['status'] == 'sent'
        assert route.call_count == 2
        for file in project[0].joinpath('state').glob('*.json'):
            assert set(json.loads(file.read_text())) == {'hash', 'nextAt', 'retryAt'}
            assert file.stat().st_mode & 0o077 == 0
            assert 'TOP_SECRET' not in file.read_text()


@pytest.mark.asyncio
async def test_changes_send_but_rate_limit_backoff_cannot_be_forced(project):
    with respx.mock as router:
        route = router.post('https://dockray.io/api/v1/project%2Ftoken/inventory').mock(return_value=httpx.Response(429, headers={'Retry-After': '3600'}, text='TOP_SECRET'))
        instance = reporter(project)
        assert (await instance.report())['http_status'] == 429
        assert (await instance.report(force=True))['status'] == 'backoff'
        assert route.call_count == 1


@pytest.mark.asyncio
async def test_cross_instance_lock_and_cancellation_cleanup(project, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    async def send(self, body):
        entered.set()
        await release.wait()
        return 200, {}, {'success': True}
    monkeypatch.setattr(InventoryReporter, '_send', send)
    first = asyncio.create_task(reporter(project).report())
    await entered.wait()
    assert (await reporter(project).report())['status'] == 'busy'
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert (await reporter(project).report())['status'] == 'sent'


@pytest.mark.asyncio
async def test_invalid_configuration_and_public_or_symlink_state_never_send(project, monkeypatch):
    async def forbidden(*args):
        raise AssertionError('Unexpected network')
    monkeypatch.setattr(InventoryReporter, '_send', forbidden)
    for url in ['http://dockray.io', 'https://user:pass@dockray.io', 'https://dockray.io/?secret=true']:
        assert (await reporter(project, url=url).report())['status'] == 'invalid_configuration'
    assert (await reporter(project, environment='unknown').report())['status'] == 'failed'
    public = project[0] / 'state'
    public.mkdir(mode=0o755)
    assert (await reporter(project).report())['status'] == 'failed'
    public.rmdir()
    public.symlink_to(project[1], target_is_directory=True)
    assert (await reporter(project).report())['status'] == 'failed'


@pytest.mark.asyncio
async def test_redirects_and_oversized_or_malformed_responses_are_not_followed_or_logged(project, caplog):
    with respx.mock as router:
        route = router.post('https://dockray.io/api/v1/project%2Ftoken/inventory').mock(return_value=httpx.Response(302, headers={'Location': 'https://attacker.example'}))
        assert (await reporter(project).report())['http_status'] == 302
        assert route.call_count == 1
    for content in [b'TOP_SECRET' * 10000, b'not JSON TOP_SECRET']:
        for file in project[0].joinpath('state').glob('*.json'):
            file.unlink()
        with respx.mock as router:
            router.post('https://dockray.io/api/v1/project%2Ftoken/inventory').mock(return_value=httpx.Response(200, content=content))
            assert (await reporter(project).report())['status'] == 'failed'
    assert 'TOP_SECRET' not in caplog.text


@pytest.mark.asyncio
async def test_opt_in_client_schedule_starts_once_and_close_waits_for_reports(project, monkeypatch):
    monkeypatch.setenv('RAY_INVENTORY', 'true')
    monkeypatch.setenv('RAY_ENVIRONMENT', 'development')
    disabled = DockRayClient(inventory_enabled=False)
    assert disabled.inventory._schedule is None
    await disabled.close()
    client = DockRayClient(token='project/token', private_key='TOP_SECRET', inventory_project_directory=project[0], inventory_state_directory=project[0] / 'state')
    assert client.environment == 'development'
    task = client.inventory._schedule
    assert task is not None
    assert client.start_inventory() is False
    with respx.mock as router:
        route = router.post('https://dockray.io/api/v1/project%2Ftoken/inventory').mock(return_value=httpx.Response(200, json={'success': True}))
        while not route.called:
            await asyncio.sleep(.001)
        await client.close()
        assert task.done()
        assert client.inventory._schedule is None
        assert route.call_count == 1


def test_cli_never_logs_credentials_and_environment_is_optional(monkeypatch, capsys):
    from dock_ray.inventory_cli import main
    monkeypatch.setenv('RAY_PRIVATE_KEY', 'TOP_SECRET')
    monkeypatch.setenv('RAY_INVENTORY', 'false')
    monkeypatch.setattr('sys.argv', ['dockray-inventory'])
    assert main() == 0
    assert 'disabled' in capsys.readouterr().out
    monkeypatch.setattr('sys.argv', ['dockray-inventory', '--bad=TOP_SECRET'])
    assert main() == 1
    assert 'TOP_SECRET' not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_existing_error_reporting_remains_compatible_and_credentials_stay_in_header():
    client = DockRayClient(token='project', private_key='TOP_SECRET', inventory_enabled=False, compress=False)
    with respx.mock as router:
        route = router.post('https://dockray.io/api/v1/project/project').mock(return_value=httpx.Response(200, json={'success': True}))
        assert await client.capture_message('hello') is True
        client.send_later(client.capture_exception(ValueError('boom')))
        await client.flush()
        assert route.call_count == 2
        assert route.calls[0].request.headers['authorization'] == 'Bearer TOP_SECRET'
        assert 'TOP_SECRET' not in route.calls[0].request.content.decode()
    await client.close()


@pytest.mark.asyncio
async def test_dependency_changes_are_reported_before_unchanged_refresh(project):
    with respx.mock as router:
        route = router.post('https://dockray.io/api/v1/project%2Ftoken/inventory').mock(return_value=httpx.Response(200, json={'success': True}))
        instance = reporter(project)
        assert (await instance.report())['status'] == 'sent'
        path = project[1] / 'child-2.0.dist-info/METADATA'
        path.write_text('Name: child\nVersion: 2.1\n')
        assert (await instance.report())['status'] == 'sent'
        assert route.call_count == 2


@pytest.fixture
def outside_loop_client():
    client = DockRayClient(inventory_enabled=True)
    assert client.inventory._schedule is None
    return client


@pytest.mark.asyncio
async def test_asgi_lifespan_starts_client_created_before_the_event_loop(outside_loop_client):
    async def app(scope, receive, send):
        assert outside_loop_client.inventory._schedule is not None
    middleware = DockRayFastAPIMiddleware(app, outside_loop_client)
    await middleware({'type': 'lifespan'}, None, None)
    await outside_loop_client.close()


def test_tool_only_pyproject_falls_back_to_production_requirements_and_extra_negation(project):
    root, packages = project
    root.joinpath('pyproject.toml').write_text('[tool.example]\nconfig=true\n')
    root.joinpath('requirements.txt').write_bytes(b'Foo_Bar[db]>=1.0 \\\r\n --hash=sha256:' + b'0' * 64 + b'\r\n')
    packages.joinpath('Foo_Bar-1.0.dist-info/METADATA').write_text('Name: Foo_Bar\nVersion: 1.0\nRequires-Dist: child>=2;\n python_version >= "3.9"\nRequires-Dist: absent; extra != "db"\n')
    assert [c['slug'] for c in collect_inventory(root)['components']] == ['child', 'foo-bar']
