"""HTTP 接口层。"""

from .routes_admin import router as admin_router
from .routes_asr_models import router as asr_models_router
from .routes_asr_probe import router as asr_probe_router
from .routes_data import router as data_router
from .routes_engine import router as engine_router
from .routes_voice import router as voice_router

ROUTERS = (
    admin_router,
    data_router,
    engine_router,
    voice_router,
    asr_models_router,
    asr_probe_router,
)

__all__ = ["ROUTERS"]
