"""参谋引擎：理解 → 定策略 → 出选项 → 推演。"""

from .context import ContextPack, build_context
from .analyzer import analyze
from .planner import plan
from .suggestor import suggest, local_score
from .simulator import simulate
from .pipeline import run_analysis

__all__ = [
    "ContextPack", "build_context",
    "analyze", "plan", "suggest", "local_score", "simulate", "run_analysis",
]
