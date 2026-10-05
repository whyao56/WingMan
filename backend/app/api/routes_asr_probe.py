"""语音链路探针：「录一段 → 转写 → 把中间结果全摊开」。

为什么值得单独做一组接口：

语音出问题时，用户看到的永远是同一句话 —— **「没反应」**。
但背后的原因可能是：
    ① 没装采集库      ② 找不到麦克风      ③ 麦克风被系统静音
    ④ 环境太吵识别不出  ⑤ 模型没下载       ⑥ 引擎还是 Mock（结果全是假的）

这六种情况的**现象完全一样，解法完全不同**。让人自己猜是不可能的，
所以这里直接把设备列表、电平、耗时、原始文本都返回给界面 ——
用户看到「电平 ≈ 0」就知道是麦克风的问题，看到「引擎=mock」就知道要去切引擎。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter

log = logging.getLogger("wingman.api.probe")

router = APIRouter(prefix="/api/asr", tags=["asr-probe"])

# 录音时长上限。太长会让界面以为卡死，太短又听不到完整句子。
MAX_SECONDS = 15.0
MIN_SECONDS = 0.5


def _probe(seconds: float, kind: str, spec: str) -> dict[str, Any]:
    """同步执行：枚举设备 → 录音 → 转写。放在线程里跑，别阻塞事件循环。"""
    from ..asr.base import rms
    from ..asr.capture import list_audio_devices, record_once
    from ..context import get_ctx

    result: dict[str, Any] = {
        "ok": False,
        "steps": [],
        "devices": [],
        "text": "",
        "level": 0.0,
        "engine": "",
        "engine_note": "",
        "seconds": seconds,
        "kind": kind,
        "elapsed_sec": 0.0,
        "error": "",
    }

    def step(name: str, status: str, detail: str) -> None:
        result["steps"].append({"name": name, "status": status, "detail": detail})

    # ---- ① 设备
    try:
        devices = list_audio_devices()
        result["devices"] = [
            {
                "id": getattr(d, "id", ""),
                "name": getattr(d, "name", ""),
                "kind": getattr(d, "kind", ""),
                "is_default": bool(getattr(d, "is_default", False)),
            }
            for d in devices
        ]
        loops = [d for d in devices if getattr(d, "kind", "") == "loopback"]
        if devices:
            step("设备", "ok" if (kind != "loopback" or loops) else "warn",
                 f"找到 {len(devices)} 个设备（含 {len(loops)} 个回环）")
        else:
            step("设备", "fail",
                 "一个音频设备都没找到。多半是没装采集库 soundcard。")
    except Exception as exc:  # noqa: BLE001
        step("设备", "fail", f"枚举失败：{type(exc).__name__}: {exc}")

    # ---- ② 录音
    pcm = None
    try:
        pcm, sr, dev_name = record_once(seconds=seconds, kind=kind, spec=spec)
        level = rms(pcm.astype("float32") / 32768.0)
        result["level"] = round(level, 4)
        result["device_name"] = dev_name
        if level < 0.002:
            step("录音", "fail",
                 f"电平 RMS={level:.4f}，几乎是静音。设备选对了但没收到声音 —— "
                 f"检查麦克风是否被静音、音量是否为 0。")
        elif level < 0.008:
            step("录音", "warn",
                 f"电平 RMS={level:.4f}，偏小。能识别，但离麦克风近一点会更准。")
        else:
            step("录音", "ok", f"电平 RMS={level:.4f}（正常），设备：{dev_name}")
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        step("录音", "fail", str(exc))

    # ---- ③ 转写
    if pcm is not None:
        try:
            ctx = get_ctx()
            eng = ctx.asr
            result["engine"] = getattr(eng, "name", "?")
            result["engine_note"] = getattr(eng, "note", "")

            if result["engine"] == "mock":
                step("引擎", "fail",
                     "当前是 Mock 引擎 —— 下面的识别结果是**假的**，"
                     "只能证明链路没崩，不能证明识别能力。"
                     "到「设置 → 语音识别」选一条真实路线。")
            else:
                step("引擎", "ok", f"{result['engine']} · {result['engine_note']}")

            t0 = time.time()
            res = asyncio.run(eng.transcribe(pcm, sr, language="zh"))
            result["elapsed_sec"] = round(time.time() - t0, 2)
            result["text"] = res.text
            result["confidence"] = round(float(res.confidence), 3)
            result["language"] = res.language

            if res.meta.get("error"):
                step("转写", "fail", f"引擎报错：{res.meta['error']}")
            elif not res.text.strip():
                step("转写", "warn",
                     f"没识别出任何文字（耗时 {result['elapsed_sec']}s）。"
                     f"可能是没说话、太小声音，或者背景噪声太大。"
                     f"也可以换更大的模型规格再试。")
            else:
                step("转写", "ok",
                     f"识别成功，耗时 {result['elapsed_sec']}s，"
                     f"置信度 {result['confidence']}")
        except Exception as exc:  # noqa: BLE001
            log.exception("语音探针转写失败")
            result["error"] = f"{type(exc).__name__}: {exc}"
            step("转写", "fail", f"{type(exc).__name__}: {exc}")

    # ---- 结论
    worst = "ok"
    for s in result["steps"]:
        if s["status"] == "fail":
            worst = "fail"
            break
        if s["status"] == "warn":
            worst = "warn"
    result["ok"] = worst == "ok" and bool(result["text"].strip())
    result["worst"] = worst
    return result


@router.get("/probe/devices")
async def probe_devices() -> dict[str, Any]:
    """只列设备，不录音。给界面显示「当前用的是哪个麦克风」用。"""
    import asyncio as _asyncio

    def _list() -> dict[str, Any]:
        from ..asr.capture import list_audio_devices

        try:
            devices = list_audio_devices()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "devices": []}
        return {
            "ok": True,
            "devices": [
                {
                    "id": getattr(d, "id", ""),
                    "name": getattr(d, "name", ""),
                    "kind": getattr(d, "kind", ""),
                    "is_default": bool(getattr(d, "is_default", False)),
                }
                for d in devices
            ],
        }

    return await _asyncio.to_thread(_list)


@router.post("/probe")
async def probe(
    seconds: float = 5.0,
    kind: str = "microphone",
    spec: str = "auto",
) -> dict[str, Any]:
    """录一段并转写。

    ``kind``：``microphone`` 听自己，``loopback`` 抓系统声音（听对方）。
    """
    seconds = max(MIN_SECONDS, min(MAX_SECONDS, float(seconds or 5.0)))
    if kind not in ("microphone", "loopback"):
        kind = "microphone"
    log.info("语音探针：录制 %.1fs，来源=%s，设备=%s", seconds, kind, spec)
    return await asyncio.to_thread(_probe, seconds, kind, spec)
