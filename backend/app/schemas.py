"""全项目共用的数据模型。

命名约定：
- 带 `Parsed` 前缀的是适配器解析中间态
- 其余为 API / 引擎的输入输出结构
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["me", "peer", "system"]


# ============================================================= 基础数据


class Msg(BaseModel):
    """统一消息模型 —— 所有适配器最终都产出它。"""

    chat_id: str
    platform: str
    sender: str
    role: Role
    ts: datetime
    text: str
    msg_type: str = "text"
    ext_id: str | None = None

    def to_row(self) -> tuple:
        return (
            self.chat_id,
            self.platform,
            self.sender,
            self.role,
            self.ts.isoformat(),
            self.msg_type,
            self.text,
            self.ext_id,
        )

    def brief(self, limit: int = 200) -> str:
        t = self.text.replace("\n", " ")
        if len(t) > limit:
            t = t[:limit] + "…"
        return t


class ParsedMsg(BaseModel):
    """适配器解析出的中间态：还不知道 chat_id，role 也只是猜测。"""

    sender: str
    ts: datetime
    text: str
    msg_type: str = "text"
    role_hint: Role | None = None


class ChatInfo(BaseModel):
    id: str
    platform: str
    name: str
    peer_name: str = ""
    me_name: str = ""
    created_at: str = ""
    message_count: int = 0
    peer_count: int = 0
    me_count: int = 0
    first_ts: str | None = None
    last_ts: str | None = None
    indexed: int = 0


# ============================================================= 导入


class AdapterProbe(BaseModel):
    name: str
    display_name: str
    confidence: float
    note: str = ""


class ImportPreview(BaseModel):
    adapter: str
    probes: list[AdapterProbe]
    total_parsed: int
    speakers: list[str]
    sample: list[ParsedMsg] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ImportResult(BaseModel):
    chat_id: str
    adapter: str
    parsed: int
    inserted: int
    skipped: int
    speakers: list[str]
    warnings: list[str] = Field(default_factory=list)


# ============================================================= 记忆 / 画像


class Fact(BaseModel):
    id: int | None = None
    chat_id: str = ""
    subject: str = "peer"          # peer | me | relationship
    key: str
    value: str
    confidence: float = 0.6
    evidence: str = ""             # 逗号分隔的 message_id，供前端核对
    updated_at: str = ""


class Summary(BaseModel):
    id: int | None = None
    chat_id: str = ""
    kind: str = "weekly"           # daily | weekly | milestone
    period: str = ""
    content: str = ""
    created_at: str = ""


class Persona(BaseModel):
    chat_id: str = ""
    goal: str = ""                 # 用户自己的目标，例如"想约她周末看展"
    my_style: str = ""             # 我平时的说话风格
    peer_profile: str = ""         # 对方画像（模型综合生成）
    taboos: str = ""               # 雷区，逗号/换行分隔
    stage: str = ""                # 关系阶段
    updated_at: str = ""


class ProfileBuildResult(BaseModel):
    facts_extracted: int
    facts_total: int
    persona: Persona
    warnings: list[str] = Field(default_factory=list)


# ============================================================= 引擎


class PeerAnalysis(BaseModel):
    """对对方最新一条消息的解读。观测与推测分开，避免用户盲目信任。"""

    emotion: str = ""
    emotion_intensity: int = 5           # 0~10
    interest_delta: int = 0              # -3 ~ +3
    intent: str = ""
    subtext: str = ""
    topics: list[str] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)   # 观测到的可验证事实
    risk: str = ""
    stage_guess: str = ""


class Strategy(BaseModel):
    stage: str = ""
    goal_this_turn: str = ""
    tone: str = ""
    must_do: list[str] = Field(default_factory=list)
    must_not: list[str] = Field(default_factory=list)


class Scores(BaseModel):
    naturalness: float = 0.0   # 自然度 0~1
    warmth: float = 0.0        # 共情温度 0~1
    progress: float = 0.0      # 目标推进 0~1
    risk: float = 0.0          # 风险 0~1，越低越好


class Prediction(BaseModel):
    direction: str = ""        # 升温 | 持平 | 降温
    confidence: float = 0.5


class ReplyOption(BaseModel):
    id: str
    style: str
    text: str
    rationale: str = ""
    expected_effect: str = ""
    risk: str = ""
    scores: Scores = Field(default_factory=Scores)
    score_notes: list[str] = Field(default_factory=list)
    total: float = 0.0
    prediction: Prediction = Field(default_factory=Prediction)


class SuggestionBundle(BaseModel):
    analysis: PeerAnalysis
    strategy: Strategy
    options: list[ReplyOption]
    context_used: list[dict[str, Any]] = Field(default_factory=list)
    persona: Persona = Field(default_factory=Persona)
    warnings: list[str] = Field(default_factory=list)
    trace: dict[str, Any] = Field(default_factory=dict)   # 调试用：各步骤耗时


class SimStep(BaseModel):
    speaker: Literal["peer", "me"]
    text: str
    note: str = ""


class SimBranch(BaseModel):
    label: str = ""
    likelihood: str = "possible"     # likely | possible | unlikely
    probability: float = 0.0         # 归一化后的数值，供前端画条
    steps: list[SimStep] = Field(default_factory=list)
    outcome: str = ""
    temperature: str = ""            # 升温 | 持平 | 降温


class SimTree(BaseModel):
    option_id: str
    option_text: str
    branches: list[SimBranch]
    advice: str = ""
    disclaimer: str = (
        "以上为 AI 情景推演，不是对未来的预测。它的用途是横向比较不同回复的相对优劣，"
        "请勿据此认定对方一定会如何反应。"
    )


# ============================================================= 设置


class ProviderInfo(BaseModel):
    kind: str                    # llm | embedder
    name: str
    available: bool = True
    note: str = ""


class HealthOut(BaseModel):
    version: str
    db: str
    counts: dict[str, int]
    providers: list[ProviderInfo]
