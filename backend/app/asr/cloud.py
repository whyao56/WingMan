"""云端 ASR（OpenAI 兼容 /v1/audio/transcriptions）。

适合没显卡、想快速看到效果的场景。
**注意隐私代价**：通话音频会上传到服务商。如果聊的内容敏感，请改用本地 whisper。
"""

from __future__ import annotations

import logging

import httpx
import numpy as np

from .base import ASREngine, ASRResult, wav_bytes

log = logging.getLogger("wingman.asr.cloud")


class CloudASR(ASREngine):
    name = "cloud"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "whisper-1",
        timeout: float = 60.0,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or "whisper-1"
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.model)

    @property
    def note(self) -> str:
        if not self.available:
            return "未配置 ASR base_url"
        return f"{self.base_url} · {self.model}（音频将上传到该服务）"

    @property
    def ready(self) -> bool:
        # 云 ASR 没有「权重下载」这一步，配好地址就算能用。
        # 能不能连通要真的发一次请求才知道，自检面板里另有「语音链路实测」负责。
        return self.available

    @property
    def not_ready_reason(self) -> str:
        return "" if self.available else "没有填接口地址（base_url）"

    async def transcribe(
        self, pcm: np.ndarray, sample_rate: int = 16000, language: str = "zh"
    ) -> ASRResult:
        arr = np.asarray(pcm)
        dur_ms = int(len(arr) / max(1, sample_rate) * 1000)
        if len(arr) < sample_rate * 0.2 or not self.available:
            return ASRResult(text="", language=language, duration_ms=dur_ms)

        data = {
            "model": self.model,
            "response_format": "json",
        }
        if language and language != "auto":
            data["language"] = language
        files = {"file": ("seg.wav", wav_bytes(arr, sample_rate), "audio/wav")}
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/audio/transcriptions",
                    data=data, files=files, headers=headers,
                )
        except httpx.HTTPError as exc:
            return ASRResult(text="", language=language, duration_ms=dur_ms,
                             meta={"error": f"网络错误：{exc}"})

        if resp.status_code >= 400:
            log.warning("云 ASR 返回 %s：%s", resp.status_code, resp.text[:200])
            return ASRResult(text="", language=language, duration_ms=dur_ms,
                             meta={"error": f"HTTP {resp.status_code}"})

        try:
            payload = resp.json()
        except ValueError:
            return ASRResult(text=resp.text.strip(), language=language, duration_ms=dur_ms)

        return ASRResult(
            text=str(payload.get("text") or "").strip(),
            language=str(payload.get("language") or language),
            duration_ms=dur_ms,
            confidence=0.0,
        )
