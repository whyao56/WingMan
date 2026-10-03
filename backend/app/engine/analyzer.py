"""分析器：把对方的一句话拆成 情绪 / 意图 / 潜台词 / 兴趣变化 / 风险。

输出的 `signals` 必须是**可观测事实**，`subtext` 才是推测 ——
这个区分会一路透传到前端（观测蓝色、推测橙色），
目的是让用户能自己判断结论靠不靠谱，而不是全盘接受。
"""

from __future__ import annotations

import logging
from typing import Any

from ..schemas import PeerAnalysis
from . import prompts
from .context import ContextPack

log = logging.getLogger("chatwing.engine.analyzer")


def _str_list(value: Any, limit: int) -> list[str]:
    if isinstance(value, str):
        return [v.strip() for v in value.replace("；", ";").split(";") if v.strip()][:limit]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()][:limit]
    return []


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(float(value))))
    except (TypeError, ValueError):
        return default


def coerce(data: dict[str, Any]) -> PeerAnalysis:
    """把模型输出规整成合法结构。字段缺失或超界都不该让整条链路崩掉。"""
    return PeerAnalysis(
        emotion=str(data.get("emotion") or "平静")[:20],
        emotion_intensity=_int(data.get("emotion_intensity"), 5, 0, 10),
        interest_delta=_int(data.get("interest_delta"), 0, -3, 3),
        intent=str(data.get("intent") or "")[:200],
        subtext=str(data.get("subtext") or "")[:500],
        topics=_str_list(data.get("topics"), 6),
        signals=_str_list(data.get("signals"), 6),
        risk=str(data.get("risk") or "")[:300],
        stage_guess=str(data.get("stage_guess") or "")[:20],
    )


async def analyze(ctx: Any, pack: ContextPack) -> PeerAnalysis:
    user = prompts.analyze_user(
        profile=pack.profile_text,
        stage=pack.persona.stage,
        taboos=pack.persona.taboos,
        facts=pack.facts_text,
        summaries=pack.summaries_text,
        retrieved=pack.retrieved_text,
        recent=pack.recent_text,
        peer_message=pack.peer_message,
    )
    data = await ctx.llm.complete_json(
        prompts.ANALYZE_SYSTEM, user,
        schema_hint=prompts.ANALYZE_SCHEMA, temperature=0.4,
    )
    result = coerce(data)
    log.debug("分析结果：%s", result.model_dump())
    return result
