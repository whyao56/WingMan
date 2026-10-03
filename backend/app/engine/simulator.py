"""走向推演：发出这条回复之后，对话会怎么走。

**这里有一个刻意的产品决策。**

模型直接编的百分比是不可信的，但它对"大概率/有可能/不太可能"的
三档判断相对稳定。所以提示词里只允许填 `likely / possible / unlikely`，
再由代码映射成数值并归一化。这比让模型吐 `58%` 靠谱得多。

另一个决策：推演结果必须带免责声明，并且前端要显眼地展示。
把一个语言模型的情景模拟包装成"预言"，是在骗用户 —— 而用户会因此
做出真实的、可能有代价的决策。这条线不能越。
"""

from __future__ import annotations

import logging
from typing import Any

from ..schemas import SimBranch, SimStep, SimTree
from . import prompts
from .context import ContextPack

log = logging.getLogger("wingman.engine.simulator")

LIKELIHOOD_PROB = {"likely": 0.62, "possible": 0.26, "unlikely": 0.12}
MAX_BRANCHES = 3


def _coerce_steps(raw: Any) -> list[SimStep]:
    steps: list[SimStep] = []
    if not isinstance(raw, list):
        return steps
    for s in raw[:3]:
        if not isinstance(s, dict):
            continue
        sp = str(s.get("speaker") or "peer").strip().lower()
        if sp not in ("peer", "me"):
            sp = "peer"
        text = str(s.get("text") or "").strip()
        if not text:
            continue
        steps.append(SimStep(speaker=sp, text=text[:200], note=str(s.get("note") or "")[:160]))
    return steps


def _coerce_branches(raw: Any) -> list[SimBranch]:
    branches: list[SimBranch] = []
    if not isinstance(raw, list):
        return []
    for i, b in enumerate(raw[:MAX_BRANCHES]):
        if not isinstance(b, dict):
            continue
        lk = str(b.get("likelihood") or "possible").strip().lower()
        if lk not in LIKELIHOOD_PROB:
            lk = "possible"
        branches.append(SimBranch(
            label=str(b.get("label") or f"分支 {i + 1}")[:80],
            likelihood=lk,
            steps=_coerce_steps(b.get("steps")),
            outcome=str(b.get("outcome") or "")[:300],
            temperature=str(b.get("temperature") or "")[:8],
        ))
    return branches


def _normalize(branches: list[SimBranch]) -> None:
    if not branches:
        return
    total = sum(LIKELIHOOD_PROB[b.likelihood] for b in branches) or 1.0
    for b in branches:
        b.probability = round(LIKELIHOOD_PROB[b.likelihood] / total, 3)


async def simulate(
    ctx: Any,
    pack: ContextPack,
    option_text: str,
    *,
    option_id: str = "",
) -> SimTree:
    user = prompts.simulate_user(
        profile=pack.profile_text,
        stage=pack.persona.stage,
        taboos=pack.persona.taboos,
        style_samples=pack.style_text,
        recent=pack.recent_text,
        peer_message=pack.peer_message,
        option_text=option_text,
    )
    data = await ctx.llm.complete_json(
        prompts.SIMULATE_SYSTEM, user,
        schema_hint=prompts.SIMULATE_SCHEMA, temperature=0.85,
    )
    branches = _coerce_branches(data.get("branches"))
    _normalize(branches)

    if not branches:
        log.warning("推演没有产出任何分支")

    return SimTree(
        option_id=option_id,
        option_text=option_text,
        branches=branches,
        advice=str(data.get("advice") or "")[:600],
    )
