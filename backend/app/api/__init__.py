"""HTTP 接口层。"""

from .routes_admin import router as admin_router
from .routes_collect import router as collect_router
from .routes_data import router as data_router
from .routes_engine import router as engine_router
from .routes_health import router as health_router
from .routes_persons import router as persons_router

ROUTERS = (
    admin_router,
    data_router,
    persons_router,
    collect_router,
    engine_router,
    health_router,
)

__all__ = ["ROUTERS"]
