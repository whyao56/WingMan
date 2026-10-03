"""双通道音频采集。

## 核心洞察

语音通话时：

    ┌─────────────┐
    │  扬声器输出  │ ──（系统回环捕获）──▶ 这是**对方**的声音
    └─────────────┘
    ┌─────────────┐
    │   麦克风     │ ──────────────────▶ 这是**你**的声音
    └─────────────┘

所以只要同时开两路采集，就天然得到了**说话人分离**的结果 ——
不需要跑说话人聚类模型，不需要声纹识别。
这是整个语音方案能成立的地基。

## 流程

    采集线程（soundcard）
        ↓ 30ms 一块
    VAD 分段（能量阈值 + 静音时长）
        ↓ 一段 1~15 秒的音频
    run_coroutine_threadsafe 投递回事件循环
        ↓
    ASR 转写 → 发布到事件总线（SSE 推给前端）→ 落库

## 依赖

需要 `soundcard`（pip install -r requirements-asr.txt）。
没装的话 `list_audio_devices()` 返回空，`start()` 抛出可读的错误，
前端会看到明确提示，而不是静默失败。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Sequence

import numpy as np

from ..bus import bus
from ..schemas import AudioDevice, TranscriptSegment, VoiceStatus
from .base import rms

log = logging.getLogger("chatwing.asr.capture")


def _soundcard() -> Any:
    try:
        import soundcard as sc  # type: ignore
        return sc
    except Exception as exc:      # ImportError 或后端初始化失败
        log.debug("soundcard 不可用：%s", exc)
        return None


# ================================================================ 设备枚举


def list_audio_devices() -> list[AudioDevice]:
    sc = _soundcard()
    if sc is None:
        return []

    try:
        default_spk = sc.default_speaker()
    except Exception:
        default_spk = None
    try:
        default_mic = sc.default_microphone()
    except Exception:
        default_mic = None

    try:
        mics = sc.all_microphones(include_loopback=True)
    except Exception as exc:
        log.warning("枚举音频设备失败：%s", exc)
        return []

    out: list[AudioDevice] = []
    for m in mics:
        try:
            name = str(getattr(m, "name", "") or "")
            mid = str(getattr(m, "id", None) or name)
            is_loop = bool(getattr(m, "isloopback", False))
        except Exception:
            continue
        if not name:
            continue
        kind = "loopback" if is_loop else "microphone"
        is_default = False
        if is_loop and default_spk is not None:
            is_default = name == getattr(default_spk, "name", None)
        elif not is_loop and default_mic is not None:
            is_default = name == getattr(default_mic, "name", None)
        out.append(AudioDevice(id=mid, name=name, kind=kind, is_default=is_default))

    out.sort(key=lambda d: (d.kind != "loopback", not d.is_default, d.name))
    return out


def _pick_device(spec: str, kind: str) -> Any:
    """按 spec 找一个声音设备对象。spec 支持 auto / 设备 id / 名称子串。"""
    sc = _soundcard()
    if sc is None:
        return None

    if kind == "loopback":
        if not spec or spec == "auto":
            try:
                spk = sc.default_speaker()
                return sc.get_microphone(spk.name, include_loopback=True)
            except Exception as exc:
                log.warning("找不到默认扬声器的回环设备：%s", exc)
                return None
    else:
        if not spec or spec == "auto":
            try:
                return sc.default_microphone()
            except Exception:
                return None

    try:
        pool = sc.all_microphones(include_loopback=True)
    except Exception:
        return None

    for m in pool:
        if str(getattr(m, "id", "")) == spec or str(getattr(m, "name", "")) == spec:
            return m
    for m in pool:
        if spec.lower() in str(getattr(m, "name", "")).lower():
            is_loop = bool(getattr(m, "isloopback", False))
            if (kind == "loopback") == is_loop:
                return m
    log.warning("未找到匹配的设备：kind=%s spec=%r", kind, spec)
    return None


# ================================================================ VAD


class VADSegmenter:
    """基于能量的语音分段。

    比 webrtcvad/silero 粗糙，但零依赖、够用。
    音乐环境或有持续底噪时会误切 —— 那正是 ROADMAP 里要换 silero 的原因。

    `pre_roll` 保留语音开始前的一小段音频，
    否则"喂——"的第一个字经常会被切掉。
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        *,
        threshold: float = 0.012,
        silence_ms: int = 700,
        max_segment_ms: int = 15000,
        min_segment_ms: int = 300,
        pre_roll_ms: int = 240,
    ) -> None:
        self.sr = sample_rate
        self.threshold = threshold
        self.silence_ms = silence_ms
        self.max_segment_ms = max_segment_ms
        self.min_segment_ms = min_segment_ms
        self.pre_roll_ms = pre_roll_ms

        self._pre: deque[np.ndarray] = deque()
        self._pre_ms = 0.0
        self._buf: list[np.ndarray] = []
        self._in_speech = False
        self._silence_acc = 0.0
        self._pos_ms = 0.0
        self._seg_start_ms = 0.0

    def push(self, block: np.ndarray) -> list[tuple[np.ndarray, float, float]]:
        arr = np.asarray(block, dtype=np.float32)
        if arr.ndim > 1:
            arr = arr.mean(axis=1)
        if arr.size == 0:
            return []

        block_ms = arr.size / self.sr * 1000.0
        energy = rms(arr)
        out: list[tuple[np.ndarray, float, float]] = []

        if energy >= self.threshold:
            if not self._in_speech:
                self._in_speech = True
                self._buf = list(self._pre) + [arr]
                self._seg_start_ms = max(0.0, self._pos_ms - self._pre_ms)
                self._silence_acc = 0.0
                self._pre.clear()
                self._pre_ms = 0.0
            else:
                self._buf.append(arr)
                self._silence_acc = 0.0
            if self._buffered_ms() >= self.max_segment_ms:
                seg = self._flush()
                if seg:
                    out.append(seg)
        else:
            if self._in_speech:
                self._buf.append(arr)
                self._silence_acc += block_ms
                if self._silence_acc >= self.silence_ms:
                    seg = self._flush()
                    if seg:
                        out.append(seg)
            else:
                self._pre.append(arr)
                self._pre_ms += block_ms
                while self._pre and self._pre_ms > self.pre_roll_ms:
                    dropped = self._pre.popleft()
                    self._pre_ms -= dropped.size / self.sr * 1000.0

        self._pos_ms += block_ms
        return out

    def _buffered_ms(self) -> float:
        return sum(b.size for b in self._buf) / self.sr * 1000.0

    def _flush(self) -> tuple[np.ndarray, float, float] | None:
        buf, self._buf = self._buf, []
        self._in_speech = False
        self._silence_acc = 0.0
        if not buf:
            return None
        pcm = buf[0] if len(buf) == 1 else np.concatenate(buf)
        start = self._seg_start_ms
        end = start + pcm.size / self.sr * 1000.0
        if (end - start) < self.min_segment_ms:
            return None
        return pcm, start, end

    def reset(self) -> None:
        self._pre.clear()
        self._buf = []
        self._pre_ms = 0.0
        self._in_speech = False
        self._silence_acc = 0.0


# ================================================================ 采集线程


class ChannelWorker(threading.Thread):
    def __init__(
        self,
        session: "VoiceSession",
        channel: str,
        mic: Any,
        segmenter: VADSegmenter,
        blocksize_ms: float = 30.0,
    ) -> None:
        super().__init__(daemon=True, name=f"chatwing-capture-{channel}")
        self.session = session
        self.channel = channel
        self.mic = mic
        self.segmenter = segmenter
        self.sr = segmenter.sr
        self.blocksize = max(160, int(self.sr * blocksize_ms / 1000.0))
        self._stop_evt = threading.Event()
        self._last_level = 0.0

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        try:
            with self.mic.recorder(
                samplerate=self.sr, channels=1, blocksize=self.blocksize
            ) as rec:
                while not self._stop_evt.is_set():
                    data = rec.record(numframes=self.blocksize)
                    if data is None:
                        continue
                    for pcm, start_ms, end_ms in self.segmenter.push(data):
                        self.session.on_segment(self.channel, pcm, start_ms, end_ms)

                    now = time.monotonic()
                    if now - self._last_level >= 0.2:
                        self._last_level = now
                        arr = np.asarray(data, dtype=np.float32)
                        self.session.on_level(self.channel, rms(arr))
        except Exception as exc:
            log.exception("通道 %s 采集异常", self.channel)
            self.session.record_error(f"{self.channel} 通道采集中断：{exc}")


# ================================================================ 会话


class VoiceSession:
    """一次采集会话：同时开 peer（回环）和 me（麦克风）两路。"""

    def __init__(
        self,
        ctx: Any,
        *,
        chat_id: str | None = None,
        peer_device: str = "auto",
        me_device: str = "auto",
        enable_peer: bool = True,
        enable_me: bool = True,
    ) -> None:
        self.ctx = ctx
        self.chat_id = chat_id
        self.peer_device = peer_device
        self.me_device = me_device
        self.enable_peer = enable_peer
        self.enable_me = enable_me

        self.sample_rate = int(ctx.cfg("audio_sample_rate", 16000) or 16000)
        self.language = str(ctx.cfg("asr_language", "zh") or "zh")
        self.threshold = float(ctx.cfg("vad_threshold", 0.012) or 0.012)
        self.silence_ms = int(ctx.cfg("vad_silence_ms", 700) or 700)
        self.max_segment_ms = int(ctx.cfg("vad_max_segment_ms", 15000) or 15000)

        self._workers: list[ChannelWorker] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._started_at: str | None = None
        self._segments = 0
        self._seq = 0
        self._errors: list[str] = []

    # ------------------------------------------------------ 生命周期

    @property
    def running(self) -> bool:
        return self._running

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._running:
            return
        sc = _soundcard()
        if sc is None:
            raise RuntimeError(
                "未安装 soundcard，无法采集音频。"
                "请执行：pip install -r requirements-asr.txt（或 pip install soundcard）"
            )

        self._loop = loop
        self._errors.clear()
        channels: list[tuple[str, str]] = []
        if self.enable_peer:
            channels.append(("peer", "loopback"))
        if self.enable_me:
            channels.append(("me", "microphone"))

        started: list[str] = []
        for channel, kind in channels:
            spec = self.peer_device if channel == "peer" else self.me_device
            mic = _pick_device(spec, kind)
            if mic is None:
                self._errors.append(
                    f"{channel} 通道找不到可用设备（kind={kind}, spec={spec!r}）"
                )
                continue
            seg = VADSegmenter(
                self.sample_rate, threshold=self.threshold,
                silence_ms=self.silence_ms, max_segment_ms=self.max_segment_ms,
            )
            w = ChannelWorker(self, channel, mic, seg)
            self._workers.append(w)
            started.append(channel)

        if not self._workers:
            raise RuntimeError(
                "没有可用的采集通道。" + ("；".join(self._errors) if self._errors else
                "请检查系统默认扬声器/麦克风设置。")
            )

        for w in self._workers:
            w.start()
        self._running = True
        self._started_at = datetime.now().isoformat(timespec="seconds")
        log.info("语音采集已启动，通道：%s", ",".join(started))
        bus.publish({
            "type": "voice_status", "running": True,
            "channels": started, "engine": self.ctx.asr.name,
        })

    def stop(self) -> None:
        if not self._running and not self._workers:
            return
        for w in self._workers:
            w.stop()
        for w in self._workers:
            w.join(timeout=2.0)
        self._workers.clear()
        self._running = False
        log.info("语音采集已停止，共转写 %d 段", self._segments)
        bus.publish({"type": "voice_status", "running": False, "channels": []})

    # ------------------------------------------------------ 回调（采集线程调用）

    def on_segment(self, channel: str, pcm: np.ndarray, start_ms: float, end_ms: float) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._transcribe(channel, pcm, start_ms, end_ms), loop
            )
        except RuntimeError as exc:
            log.debug("投递转写任务失败：%s", exc)

    def on_level(self, channel: str, value: float) -> None:
        bus.publish({"type": "level", "channel": channel, "rms": round(float(value), 4)})

    def record_error(self, message: str) -> None:
        self._errors.append(message)
        bus.publish({"type": "error", "where": "capture", "message": message})

    # ------------------------------------------------------ 转写（事件循环内）

    async def _transcribe(self, channel: str, pcm: np.ndarray, start_ms: float, end_ms: float) -> None:
        try:
            result = await self.ctx.asr.transcribe(pcm, self.sample_rate, language=self.language)
        except Exception as exc:
            log.exception("转写失败")
            self.record_error(f"转写失败：{exc}")
            return

        text = (result.text or "").strip()
        if not text:
            return

        self._seq += 1
        self._segments += 1
        seg = TranscriptSegment(
            seq=self._seq,
            channel=channel,
            text=text,
            start_ms=int(start_ms),
            end_ms=int(end_ms),
            ts=datetime.now().isoformat(timespec="seconds"),
            duration_ms=int(max(0, end_ms - start_ms)),
            rms=round(rms(pcm), 4),
        )
        bus.publish({"type": "transcript", **seg.model_dump()})

        if self.chat_id:
            try:
                self.ctx.store.add_voice_segment(
                    self.chat_id, channel, seg.ts, text,
                    seg.start_ms, seg.end_ms, seg.duration_ms,
                )
            except Exception as exc:
                log.warning("写入语音记录失败：%s", exc)

    # ------------------------------------------------------ 状态

    def status(self) -> VoiceStatus:
        channels = [w.channel for w in self._workers]
        return VoiceStatus(
            running=self._running,
            channels=channels,
            engine=getattr(self.ctx.asr, "name", "unknown"),
            started_at=self._started_at,
            segments=self._segments,
            errors=self._errors[-10:],
        )


# ================================================================ 单例


_SESSION: VoiceSession | None = None


def get_session() -> VoiceSession | None:
    return _SESSION


def set_session(session: VoiceSession | None) -> None:
    global _SESSION
    _SESSION = session
