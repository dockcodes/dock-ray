from .client import SDK_VERSION, DockThorClient
from .fastapi_integration import DockThorFastAPIMiddleware
from .models import DEFAULT_URL, AuthData, Event, Span

__version__ = SDK_VERSION

__all__ = [
    "AuthData",
    "DEFAULT_URL",
    "DockThorClient",
    "DockThorFastAPIMiddleware",
    "Event",
    "Span",
    "__version__",
]
