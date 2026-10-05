"""音频管线与引擎状态的单元测试。

这两块都是「出错了也不报错、只是给你错结果」的高危区，
所以必须有独立的测试兜住：

1. ``to_float_mono`` —— 量纲归一化
   历史事故：调用方给了「int16 量级的 float32」（值域 ±32768），
   而代码只按 dtype 判断要不要归一化，于是把放大三万倍的波形喂给 Whisper，
   输出「玩玩玩玩玩玩!」这种乱码，**全程不报错**。
   详见 docs/DESKTOP.md 第 9 节。

2. ``ready`` vs ``available`` —— 引擎「能不能真的干活」
   历史事故：自检面板用 ``available`` 判断，而它只表示「对象构造得出来」。
   本地 whisper 在模型权重一个字节都没下载时 ``available`` 也是 True，
   于是自检把这情况报成绿色「已完成」，用户配完发现没反应又找不到原因。
   详见 docs/DESKTOP.md 第 7 节。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


# ================================================================ 量纲


def test_to_float_mono_handles_all_dimensions():
    """三种输入形式必须归一化到同一量级 —— 这是那个 bug 的核心。"""
    from app.asr.base import to_float_mono

    rng = np.random.default_rng(20261005)
    # 造一段「真实」的 int16 音频
    raw = (rng.standard_normal(3200) * 8000).clip(-32768, 32767).astype(np.int16)
    reference = raw.astype(np.float32) / 32768.0

    cases = {
        "int16 原样": raw,
        "float32 已归一化": reference,
        "float32 未归一化（旧 bug 路径）": raw.astype(np.float32),
    }

    for label, given in cases.items():
        got = to_float_mono(given)
        assert got.dtype == np.float32, f"{label}：输出 dtype 应为 float32，实际 {got.dtype}"
        assert np.max(np.abs(got)) <= 1.001, (
            f"{label}：归一化后峰值应 <= 1.0，实际 {np.max(np.abs(got)):.1f}"
            "—— 这就是「波形被放大三万倍」的复发症状"
        )
        # 三种形式归一化后应当逐样本一致
        assert np.allclose(got, reference, atol=1e-4), f"{label}：归一化结果与基准不一致"


def test_to_float_mono_keeps_quiet_audio_quiet():
    """已经归一化过的**小音量**音频不能被二次缩小。

    峰值兜底的阈值是 1.5，正常浮点音频不会超。但边界要测：
    一段最大只有 0.3 的录音，不应该被当成 int16 再除一次。
    """
    from app.asr.base import to_float_mono

    quiet = np.array([0.0, 0.1, -0.3, 0.05], dtype=np.float32)
    out = to_float_mono(quiet)
    assert np.allclose(out, quiet), f"小音量音频被误改了：{out}"


def test_to_float_mono_downsamples_stereo():
    """多通道要混成单通道，否则 Whisper 收到的长度会翻倍。"""
    from app.asr.base import to_float_mono

    stereo = np.array([[0.5, 0.5], [-0.5, -0.5], [0.25, 0.25]], dtype=np.float32)
    out = to_float_mono(stereo)
    assert out.ndim == 1, f"应输出一维，实际 {out.ndim} 维"
    assert len(out) == 3, f"长度应为 3，实际 {len(out)}"


def test_pcm_roundtrip():
    """float32 → int16 → float32 不该有明显损失。"""
    from app.asr.base import pcm_to_int16, to_float_mono

    rng = np.random.default_rng(7)
    f = (rng.standard_normal(1600) * 0.3).astype(np.float32).clip(-1, 1)
    back = to_float_mono(pcm_to_int16(f))
    assert np.allclose(back, f, atol=1 / 32767 * 2), "float32↔int16 往返丢精度太多"


def test_wav_bytes_header_is_valid():
    """云 ASR 靠这个字节流，格式错了云端会直接拒收。"""
    import io
    import wave

    from app.asr.base import wav_bytes

    rng = np.random.default_rng(1)
    pcm = (rng.standard_normal(16000) * 0.2).astype(np.float32)
    data = wav_bytes(pcm, 16000)

    with wave.open(io.BytesIO(data), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 16000
        assert w.getnframes() == 16000, f"帧数应为 16000，实际 {w.getnframes()}"


# ================================================================ 引擎状态


def test_local_whisper_not_ready_without_model():
    """库在、模型没下载 → available 为真但 ready 必须为假。

    这条是那个「自检报绿但其实不能用」bug 的回归测试。
    """
    from app.asr.local_whisper import LocalWhisperASR

    eng = LocalWhisperASR(model_size="definitely-not-a-real-size-xyz")
    if not eng.available:
        # 环境里没装 faster-whisper，退化为测「没有库时也要 ready=False」
        assert eng.ready is False
        return

    assert eng.ready is False, "模型不存在时 ready 不该为真（这正是那个 bug）"
    assert eng.not_ready_reason, "非 ready 时必须给出一句人能看懂的原因"


def test_mock_asr_is_ready_but_named_mock():
    """Mock 引擎「能跑」但没有识别能力 —— 靠 name 区分，不靠 ready。"""
    from app.asr.mock import MockASR

    eng = MockASR()
    assert eng.name == "mock"
    assert eng.ready is True, "Mock 能出文本，所以 ready 为真；调用方要靠 name 判断真假"


def test_cloud_asr_ready_follows_base_url():
    from app.asr.cloud import CloudASR

    assert CloudASR(base_url="", api_key="x").ready is False
    assert CloudASR(base_url="", api_key="x").not_ready_reason
    ok = CloudASR(base_url="https://api.example.com/v1", api_key="k")
    assert ok.ready is True
    assert ok.not_ready_reason == ""


def test_selfcheck_uses_ready_not_available():
    """自检面板必须看 ready。直接用源码扫一遍，防止有人改回去。"""
    src = (BACKEND / "app" / "selfcheck.py").read_text(encoding="utf-8")
    assert 'getattr(eng, "ready"' in src, (
        "selfcheck 里不再检查 ready 了 —— 这会把「模型没下载」报成绿色的「已完成」"
    )


# ================================================================ 模型目录


def test_model_dir_uses_local_dir_not_hf_cache():
    """模型目录必须是 local_dir 结构。

    HuggingFace 的 cache 目录会同时留 blobs/ 和 snapshots/ 两份，
    Windows 未开开发者模式时是**复制**而不是硬链接，磁盘占用直接翻倍
    （实测 tiny 本应 75MB，占了 149MB）。
    """
    from app.asr import models as M

    d = M.model_dir("tiny")
    assert d.name == "faster-whisper-tiny", f"目录命名不对：{d.name}"
    assert "blobs" not in str(d) and "snapshots" not in str(d), (
        "模型目录落在了 HF cache 结构里 —— 磁盘占用会翻倍"
    )


def test_model_catalog_covers_every_size():
    """界面上能选的规格，下载器必须都认识 —— 否则用户选了就下不了。"""
    from app.asr import models as M

    assert "large-v3" in M.MODEL_CATALOG, "界面下拉里有 large-v3，catalog 里却没有"
    for size, meta in M.MODEL_CATALOG.items():
        assert meta.get("mb", 0) > 0, f"{size} 缺少体积信息，进度条会算不出来"
        assert meta.get("note"), f"{size} 缺少一句人话说明"


def test_apply_endpoint_disables_xet():
    """必须关掉 Xet。

    国内镜像不代理 Xet 的 CAS 服务器（cas-server.xethub.hf.co），
    只换主站域名会导致 401 —— 看起来很像是鉴权问题，其实只是走错了通道。
    """
    import os

    from app.asr import models as M

    old = os.environ.get("HF_HUB_DISABLE_XET")
    try:
        os.environ.pop("HF_HUB_DISABLE_XET", None)
        M.apply_endpoint(M.MIRROR_ENDPOINT)
        assert os.environ.get("HF_HUB_DISABLE_XET") == "1", "apply_endpoint 没有关掉 Xet"
        assert os.environ.get("HF_ENDPOINT") == M.MIRROR_ENDPOINT

        M.apply_endpoint("")
        assert "HF_ENDPOINT" not in os.environ, "空 endpoint 时应该清掉 HF_ENDPOINT"
    finally:
        if old is None:
            os.environ.pop("HF_HUB_DISABLE_XET", None)
        else:
            os.environ["HF_HUB_DISABLE_XET"] = old


def test_download_state_snapshot_shape_is_stable():
    """进度快照的字段集合必须恒定。

    前端直接读这些 key；早期版本在「还没开始」的分支里少返回一个字段，
    导致 `KeyError: 'downloaded_mb'`。所以这里对空状态也校验一遍。
    """
    from app.asr.models import DownloadState

    st = DownloadState()
    snap = st.snapshot()
    for key in ("running", "done", "error", "error_hint", "size",
                "downloaded_mb", "expected_mb", "percent",
                "speed_mbps", "remain_sec", "endpoint"):
        assert key in snap, f"进度快照缺少字段 {key}"

    st2 = DownloadState()
    st2.size = "tiny"
    st2.running = True
    assert set(st2.snapshot()) == set(snap), "有任务时的字段集合和空闲时不一致"


def test_explain_error_translates_known_failures():
    """错误要翻成人话，否则用户看到 CAS/401 完全不知道下一步做什么。"""
    from app.asr.models import _explain_error

    xet = _explain_error(
        "CAS Client Error: HTTP status client error (401 Unauthorized), "
        "domain: https://cas-server.xethub.hf.co/v2/reconstructions/xxx"
    )
    assert "Xet" in xet or "通道" in xet, f"Xet/401 没被识别：{xet}"

    assert "镜像" in _explain_error("HTTPSConnectionPool: Read timed out")
    assert "磁盘" in _explain_error("OSError: [Errno 28] No space left on device")
    # 未知错误也要给出可执行的建议，不能是空字符串
    assert _explain_error("something weird happened")
