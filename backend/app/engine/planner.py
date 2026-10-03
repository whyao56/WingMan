"""策略规划：结合关系阶段，确定"这一轮"该怎么聊。

同样的消息在不同阶段目标完全不同。
"周末有空吗"在暧昧期是推进，在陌生期就是越界。
"""

from __future__ import annotations

import logging
from typing import Any

from ..schemas import PeerAnalysis, Strategy
from . import prompts
from .context import ContextPack

log = logging.getLogger("wingman.engine.planner")

STAGES = ("陌生期", "熟悉期", "暧昧期", "推进期", "稳定期")


def _str_list(value: Any, limit: int) -> list[str]:
    if isinstance(value, str):
        return [v.strip(" -·•\t") for v in value.replace("；", ";").split(";") if v.strip()][:limit]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()][:limit]
    return []


def coerce(data: dict[str, Any], fallback_stage: str) -> Strategy:
    stage = str(data.get("stage") or fallback_stage or "熟悉期")[:12]
    if stage not in STAGES:
        stage = fallback_stage or "熟悉期"
    return Strategy(
        stage=stage,
        goal_this_turn=str(data.get("goal_this_turn") or "")[:300],
        tone=str(data.get("tone") or "")[:100],
        must_do=_str_list(data.get("must_do"), 4),
        must_not=_str_list(data.get("must_not"), 5),
    )


async def plan(ctx: Any, pack: ContextPack, analysis: PeerAnalysis) -> Strategy:
    analysis_text = (
        f"情绪：{analysis.emotion}（强度 {analysis.emotion_intensity}/10）\n"
        f"兴趣变化：{analysis.interest_delta:+d}\n"
        f"意图：{analysis.intent}\n"
        f"潜台词：{analysis.subtext}\n"
        f"风险：{analysis.risk}"
    )
    user = prompts.strategy_user(
        profile=pack.profile_text,
        stage=pack.persona.stage or analysis.stage_guess,
        goal=pack.persona.goal,
        taboos=pack.persona.taboos,
        peer_message=pack.peer_message,
        analysis=analysis_text,
    )
    data = await ctx.llm.complete_json(
        prompts.STRATEGY_SYSTEM, user,
        schema_hint=prompts.STRATEGY_SCHEMA, temperature=0.4,
    )
    result = coerce(data, pack.persona.stage or analysis.stage_guess)
    # 雷区必须进 must_not —— 模型有时候会漏，这里兜一层
    if pack.persona.taboos:
        existing = " ".join(result.must_not)
        for t in [x.strip() for x in pack.persona.taboos.replace("，", "、").replace(",", "、").split("、") if x.strip()]:
            if t and t not in existing:
                result.must_not.append(t)
        result.must_not = result.must_not[:6]
    return result
