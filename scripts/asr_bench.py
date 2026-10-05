"""本地 Whisper 模型对比 / 量纲回归测试。

两个用途：

**一、选模型规格**（最常用）
    想知道 tiny / base / small 在中文上到底差多少、CPU 上慢多少，
    跑一遍就有数了。结果是「该选哪个」的直接依据，不用凭感觉。

        python scripts/asr_bench.py
        python scripts/asr_bench.py --models tiny,small,medium
        python scripts/asr_bench.py --text "周末一起去看展吗" --repeat 3

**二、量纲回归**（防止那个「不报错的坑」复发）
    同一个音频用三种形式喂进去，结果必须**逐字一致**：

        ① 已归一化的 float32（±1.0）  ← 生产路径，capture.py 给的就是这个
        ② 未归一化的 float32（±32768）← 历史上的 bug 路径
        ③ 原始 int16

    三种不一致，就说明 ``to_float_mono()`` 的兜底失效了，
    而症状是「识别出乱码但不报错」—— 见 docs/DESKTOP.md 第 9 节。

依赖：Windows（用 SAPI 合成测试语音）+ 已下载的本地模型。
不带 ``--wav`` 时会现场合成语音，所以不需要准备素材。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.asr.local_whisper import LocalWhisperASR  # noqa: E402
from app.asr.models import MODEL_CATALOG, is_installed  # noqa: E402

# 默认对比这两档：tiny 是最快的，small 是推荐的起步档
DEFAULT_MODELS = "tiny,small"
DEFAULT_TEXT = "今天加班到十点，好累啊，你周末有空吗"

# 不能用 "/tmp/..."：Windows 上会解析成 C:\tmp，而不是 Git Bash 的临时目录
TMP = Path(tempfile.gettempdir())


# ---------------------------------------------------------------- 造音频


def make_tts(text: str, out: Path, rate: int = 0) -> None:
    """用 Windows SAPI 把文字念到 WAV 文件。

    关键：显式把格式设成 16kHz/16bit/单声道（SAFT16kHz16BitMono = 18）。
    SAPI 默认吐 22050Hz，而 Whisper 只认 16kHz —— 直接喂会明显掉准确率。
    这个坑同样不报错，只会让结果变差，很容易误判成「模型不行」。
    """
    import comtypes.client

    stream = comtypes.client.CreateObject("SAPI.SpFileStream")
    stream.Format.Type = 18  # SAFT16kHz16BitMono
    stream.Open(str(out), 3, False)  # 3 = SSFMCreateForWrite
    voice = comtypes.client.CreateObject("SAPI.SpVoice")
    voice.AudioOutputStream = stream
    voice.Rate = rate
    voice.Volume = 100
    voice.Speak(text)
    stream.Close()


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """读成 (int16 数组, 采样率)。多声道会混成单声道。"""
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    arr = np.frombuffer(raw, dtype=np.int16)
    if ch > 1:
        arr = arr.reshape(-1, ch).mean(axis=1).astype(np.int16)
    return arr, sr


def resample_to_16k(arr: np.ndarray, sr: int) -> np.ndarray:
    """线性插值重采样到 16kHz。语音够用，且不引入 scipy 依赖。"""
    if sr == 16000 or len(arr) == 0:
        return arr
    n = int(round(len(arr) * 16000 / sr))
    src = np.linspace(0.0, 1.0, num=len(arr), endpoint=False)
    dst = np.linspace(0.0, 1.0, num=n, endpoint=False)
    return np.interp(dst, src, arr.astype(np.float64)).astype(np.int16)


# ---------------------------------------------------------------- 跑一个模型


async def bench_model(
    size: str, forms: dict[str, np.ndarray], primary: np.ndarray, sr: int, repeat: int
) -> dict:
    """跑一个模型。

    ``primary`` 单独传进来，**不要**用 ``forms[某个标签]`` 取 ——
    标签是给人看的文案，随时会改；上次就是因为改了标签但没改取值，
    直接 KeyError。基准用的那一份要显式传。
    """
    print(f"\n{'=' * 66}")
    print(f"  模型 {size}   （{MODEL_CATALOG.get(size, {}).get('note', '')}）")
    print("=" * 66)

    engine = LocalWhisperASR(model_size=size, device="cpu", compute_type="int8")

    # 第一次调用把模型加载进来，单独计时（否则会被算进识别耗时里）
    t0 = time.time()
    first = await engine.transcribe(primary, sr)
    load_s = time.time() - t0

    print(f"  加载+首次：{load_s:5.2f}s   文本={first.text!r}")
    print(f"             置信度={first.confidence:.3f}  语种={first.language}")
    print()

    # 量纲回归：三种输入形式必须完全一致
    texts: set[str] = set()
    for label, pcm in forms.items():
        t0 = time.time()
        res = await engine.transcribe(pcm, sr)
        cost = time.time() - t0
        texts.add(res.text)
        peak = float(np.max(np.abs(np.asarray(pcm, dtype=np.float64)))) if len(pcm) else 0.0
        print(f"    {cost:5.2f}s  峰值{peak:>10.1f}  {label:<26} {res.text!r}")

    consistent = len(texts) == 1
    print()
    print(f"  量纲一致性：{'一致 ✓' if consistent else '不一致 ✗ —— to_float_mono 的兜底可能失效了'}")

    # 稳态速度（重复跑，取最快的一次 —— 最能代表真实体验）
    best = None
    if repeat > 1:
        for _ in range(repeat - 1):
            t0 = time.time()
            await engine.transcribe(primary, sr)
            dt = time.time() - t0
            best = dt if best is None else min(best, dt)
        print(f"  稳态耗时：{best:.2f}s / {len(primary) / sr:.1f}s 音频"
              f"（{len(primary) / sr / max(best, 1e-6):.1f}x 实时）")

    return {
        "size": size,
        "text": first.text,
        "confidence": first.confidence,
        "load_sec": load_s,
        "best_sec": best,
        "consistent": consistent,
        "audio_sec": len(primary) / sr,
    }


# ---------------------------------------------------------------- 入口


async def main() -> int:
    ap = argparse.ArgumentParser(description="本地 Whisper 模型对比 / 量纲回归")
    ap.add_argument("--models", default=DEFAULT_MODELS,
                    help=f"要对比的规格，逗号分隔（默认 {DEFAULT_MODELS}）")
    ap.add_argument("--text", default=DEFAULT_TEXT, help="要合成的测试文本")
    ap.add_argument("--wav", default="", help="直接用现成的 WAV，不做语音合成")
    ap.add_argument("--repeat", type=int, default=3, help="每个模型跑几次取最快（默认 3）")
    ap.add_argument("--lang", default="zh", help="语言（默认 zh；auto 表示自动判断）")
    args = ap.parse_args()

    sizes = [s.strip() for s in args.models.split(",") if s.strip()]
    unknown = [s for s in sizes if s not in MODEL_CATALOG]
    if unknown:
        print(f"未知的模型规格：{unknown}；可选：{list(MODEL_CATALOG)}")
        return 2

    # ---- 准备音频
    if args.wav:
        src = Path(args.wav)
        if not src.is_file():
            print(f"找不到文件：{src}")
            return 2
        pcm16, sr = read_wav(src)
        print(f"音频来源：{src}")
    else:
        src = TMP / "wingman_bench.wav"
        src.unlink(missing_ok=True)
        print(f"合成测试语音 -> {src}")
        make_tts(args.text, src)
        pcm16, sr = read_wav(src)

    if sr != 16000:
        print(f"  采样率 {sr}Hz != 16000，重采样（Whisper 只认 16k）")
        pcm16 = resample_to_16k(pcm16, sr)
        sr = 16000

    audio_sec = len(pcm16) / sr
    print(f"音频：{audio_sec:.1f}s @ {sr}Hz  峰值={int(np.max(np.abs(pcm16)))}")
    print(f"参考文本：{args.text if not args.wav else '(来自文件)'}")

    # ---- 三种量纲形式
    forms = {
        "已归一化 float32（生产路径）": pcm16.astype(np.float32) / 32768.0,
        "未归一化 float32（旧 bug）": pcm16.astype(np.float32),
        "原始 int16": pcm16,
    }

    # ---- 没下的模型跳过，别浪费时间
    runnable = []
    for s in sizes:
        if is_installed(s):
            runnable.append(s)
        else:
            print(f"\n跳过 {s}：模型还没下载。到界面「设置 → 语音」下载，"
                  f"或 python -c \"from app.asr import models as M; M.download('{s}', M.MIRROR_ENDPOINT)\"")
    if not runnable:
        print("\n一个可用模型都没有，先下载一个再跑。")
        return 1

    results = []
    for s in runnable:
        try:
            results.append(await bench_model(s, forms, pcm16, sr, args.repeat))
        except Exception:  # noqa: BLE001
            import traceback

            print(f"  !! {s} 跑失败：")
            traceback.print_exc()

    # ---- 汇总
    if results:
        print(f"\n{'=' * 66}")
        print("  汇总")
        print("=" * 66)
        print(f"  {'规格':<10}{'置信度':>8}{'稳态耗时':>10}{'实时倍率':>10}  识别结果")
        for r in results:
            if r["best_sec"]:
                speed = r["audio_sec"] / r["best_sec"]
                timing = f"{r['best_sec']:.2f}s"
                ratio = f"{speed:.1f}x"
            else:
                timing, ratio = f"{r['load_sec']:.2f}s*", "-"
            print(f"  {r['size']:<10}{r['confidence']:>8.3f}{timing:>10}{ratio:>10}  {r['text']}")
        print("  （* 只跑了一次，耗时里含模型加载）")

        if not all(r["consistent"] for r in results):
            print("\n  ⚠ 有模型的三种量纲结果不一致 —— 见 docs/DESKTOP.md 第 9 节")
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
