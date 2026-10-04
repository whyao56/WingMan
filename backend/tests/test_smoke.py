"""端到端冒烟测试：导入 → 建索引 → 画像 → 分析 → 建议 → 推演。

直接用 Mock Provider 跑，所以**不需要任何 API Key**。
pytest 和 `python tests/test_smoke.py` 两种方式都能执行。

    cd backend
    python -m pytest tests/ -q
    # 或
    python tests/test_smoke.py

中文 Windows 控制台默认是 GBK(cp936)：脚本入口会自己把 stdout/stderr 切成
UTF-8（errors="replace" 兜底），所以 `python tests\test_smoke.py` 在 936 控制台
或输出重定向到文件时都不会再抛 UnicodeEncodeError；如果目标流连中文都表示不了
（纯 ASCII 终端 / C locale），则自动降级成纯 ASCII 文案，不产生乱码。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

SAMPLE = PROJECT / "samples" / "qq_sample_小鹿.txt"
PEER_LINE = "哈哈哈今天好累啊"


# ---------------------------------------------------------------- 控制台编码

_ASCII_MODE = False


def _configure_console() -> None:
    """让本脚本在任何控制台/重定向下都能打印，且不退化成乱码。

    - 流的编码能表示中文（cp936、utf-8…）：把 stdout/stderr 重配置为 UTF-8
      （errors="replace" 兜底）。cp936 下 print("✓ …") 会抛 UnicodeEncodeError
      （实测：'gbk' codec can't encode character '\u2713'），切到 UTF-8 之后
      中文与 ✓ → 都能正常输出。
    - 流的编码连中文都表示不了（ascii / latin-1 / C locale）：不硬塞 UTF-8，
      改用纯 ASCII 文案（见 t() 与 safe()）。这是显式降级，不是乱码。
    - 不依赖调用方设置 PYTHONIOENCODING / PYTHONUTF8。
    """
    global _ASCII_MODE
    renderable = True
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        encoding = getattr(stream, "encoding", None)
        if encoding:
            try:
                "中文".encode(encoding)
            except (UnicodeEncodeError, LookupError):
                renderable = False
                continue
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, LookupError, OSError):
                pass
    _ASCII_MODE = not renderable


def t(zh: str, en: str) -> str:
    """按控制台能力选文案：能用中文就用中文，纯 ASCII 环境用英文。"""
    return en if _ASCII_MODE else zh


def safe(value: object) -> str:
    """动态值（可能来自模型/规则引擎的中文文本）在 ASCII 模式下先降级。

    纯 ASCII 环境里只保留可打印 ASCII 并压掉多余空格：数字与英文标识（imported
    62、chat_id 等）都在，中文文案整段省略，不会出现乱码或半截字节。
    """
    text = str(value)
    if not _ASCII_MODE:
        return text
    kept = "".join(ch for ch in text if " " <= ch <= "~")
    return " ".join(kept.split())


_configure_console()


def _fresh_ctx():
    """用临时数据库，避免污染真实的 backend/data/wingman.db。"""
    from app import config

    tmpdir = Path(tempfile.mkdtemp(prefix="wingman_test_"))
    config.DATA_DIR = tmpdir
    config.get_settings.cache_clear()

    import app.context as context_module

    context_module._CTX = None
    ctx = context_module.get_ctx()
    ctx.update_cfg({"llm_provider": "mock", "embedder": "hash", "asr_engine": "mock"})
    return ctx


async def run_pipeline() -> dict:
    from app.adapters import registry
    from app.engine import pipeline
    from app.engine.context import build_context
    from app.engine import simulator
    from app.memory import profiler

    ctx = _fresh_ctx()
    assert SAMPLE.exists(), f"示例文件缺失：{SAMPLE}"

    # ---- 1. 嗅探
    probes = registry.detect(SAMPLE)
    assert probes, "没有适配器能识别这个文件"
    assert probes[0].name == "qq", f"应识别为 QQ，实际为 {probes[0].name}（{probes[0].confidence}）"

    # ---- 2. 导入
    result = registry.import_file(ctx.store, SAMPLE, chat_name="小鹿")
    assert result.inserted > 40, f"只导入了 {result.inserted} 条，样例应有 60+ 条"
    chat = ctx.store.get_chat(result.chat_id)
    assert chat is not None
    assert chat.me_name == "我", f"「我」的角色识别错了：{chat.me_name}"
    assert chat.peer_name == "小鹿", f"对方昵称识别错了：{chat.peer_name}"

    # ---- 3. 幂等：重复导入不应产生新数据
    again = registry.import_file(ctx.store, SAMPLE, chat_name="小鹿")
    assert again.inserted == 0, f"重复导入竟然新增了 {again.inserted} 条"

    # ---- 4. 建立索引
    indexed = await profiler.build_index(ctx, result.chat_id)
    assert indexed > 0, "向量索引没有建立"

    # ---- 5. 检索
    from app.memory.retriever import HybridRetriever

    retriever = HybridRetriever(ctx.store, ctx.embedder)
    hits = await retriever.search(result.chat_id, "她喜欢什么动物")
    assert hits, "检索没有召回任何结果"
    joined = " ".join(h.text for h in hits[:8])
    assert "猫" in joined, f"检索『喜欢什么动物』没能召回关于猫的记录；实际召回：{joined[:200]}"

    # ---- 6. 画像
    profile = await profiler.build_profile(ctx, result.chat_id)
    assert profile.facts_total > 0, "没有抽取到任何事实"
    keys = {f.value for f in ctx.store.list_facts(result.chat_id)}
    assert any("猫" in v for v in keys), f"没有抽到「猫」相关事实，实际：{keys}"

    # ---- 7. 分析 + 建议
    bundle = await pipeline.run_analysis(ctx, result.chat_id, PEER_LINE)
    assert bundle.analysis.emotion, "分析结果为空"
    assert len(bundle.options) >= 3, f"只生成了 {len(bundle.options)} 条建议"
    assert all(o.scores.naturalness >= 0 for o in bundle.options), "打分异常"
    assert bundle.options and bundle.options[0].total > 0, "排序后的首选分数为 0"
    # 排序必须是从高到低
    totals = [o.total for o in bundle.options]
    assert totals == sorted(totals, reverse=True), f"建议没有按分数降序排列：{totals}"

    # ---- 8. 推演
    pack = await build_context(ctx, result.chat_id, PEER_LINE, use_retrieval=False)
    tree = await simulator.simulate(ctx, pack, bundle.options[0].text, option_id=bundle.options[0].id)
    assert len(tree.branches) >= 2, "推演分支太少"
    assert abs(sum(b.probability for b in tree.branches) - 1.0) < 0.02, "分支概率没有归一化"
    assert tree.disclaimer, "推演缺少免责声明"

    return {
        "chat_id": result.chat_id,
        "imported": result.inserted,
        "indexed": indexed,
        "retrieved": len(hits),
        "facts": profile.facts_total,
        "options": len(bundle.options),
        "top_option": f"{bundle.options[0].style} · {bundle.options[0].total} · {bundle.options[0].text}",
        "branches": len(tree.branches),
    }


async def run_local_score_checks() -> None:
    """本地打分器的边界用例 —— 这块逻辑独立于模型，必须自己扛住。"""
    from app.engine.suggestor import local_score
    from app.schemas import PeerAnalysis, Persona

    analysis = PeerAnalysis(emotion="疲惫", emotion_intensity=8, topics=["加班", "累"])
    persona = Persona(taboos="宝贝、亲爱的", goal="想约她周末看展")

    _, notes_short, low, _ = local_score("嗯", analysis=analysis, persona=persona)
    assert low < 6, f"敷衍回复不该得高分：{low}"

    _, notes_ai, low_ai, _ = local_score(
        "作为一个贴心的朋友，首先我建议你注意休息，总之要照顾好自己。",
        analysis=analysis, persona=persona,
    )
    assert low_ai < 6, f"AI 腔回复不该得高分：{low_ai}"
    assert any("AI 腔" in n for n in notes_ai), f"没有识别出 AI 腔：{notes_ai}"

    _, notes_taboo, score_taboo, _ = local_score(
        "宝贝别难过，我永远陪着你", analysis=analysis, persona=persona
    )
    assert any("雷区" in n for n in notes_taboo), f"没有识别出雷区：{notes_taboo}"

    _, _, high, pred = local_score(
        "听着就累，那周末带你去换个环境，跟加班彻底断联两小时",
        analysis=analysis, persona=persona,
    )
    assert high > 6.5, f"这条好回复得分偏低：{high}"
    assert pred.direction in ("升温", "持平", "降温")


def main() -> int:
    print("=" * 64)
    print(t("WingMan 冒烟测试（Mock Provider，无需 API Key）",
            "WingMan smoke test (Mock Provider, no API key needed)"))
    print("=" * 64)

    print("\n" + t("[1/2] 本地打分器边界用例 …", "[1/2] local scorer edge cases ..."))
    asyncio.run(run_local_score_checks())
    print("      " + t("✓ 通过", "OK"))

    print("\n" + t("[2/2] 完整链路：导入 → 索引 → 画像 → 分析 → 建议 → 推演 …",
                   "[2/2] full pipeline: import -> index -> profile -> analyze -> suggest -> simulate ..."))
    info = asyncio.run(run_pipeline())
    print("      " + t("✓ 通过", "OK") + "\n")
    for k, v in info.items():
        print(f"      {k:14} {safe(v)}")

    print("\n" + "=" * 64)
    print(t("全部通过。", "All checks passed."))
    print("=" * 64)
    return 0


def test_smoke() -> None:
    """pytest 入口。"""
    asyncio.run(run_local_score_checks())
    asyncio.run(run_pipeline())


if __name__ == "__main__":
    raise SystemExit(main())
