"""回归：Mock 的「潜台词」不许混入 prompt 的指令词。

缺陷本体（修复前实测）：

    extract_peer_message('【对方最新消息】\\n哈哈哈今天好累啊\\n\\n请分析这条消息。')
      → '哈哈哈今天好累啊\\n\\n请分析这条消息。'      # 指令行被当成对方的话
    keywords(那个结果, 5)
      → ['今天好累', '分析这条', '析这条消', '请分析', '条消息']

而这几个词会经 mock.py 的 `kws[:3]` 直接进用户可见文案：

    「从用词看，她此刻的关注点集中在「今天好累、分析这条、析这条消」。」

根因：prompts.py 的 user 模板在最后一个小节后面跟了一句给模型的指令，
而小节提取正则的终止符只有 `\\n【` / `\\n<<<` / 文本结尾 —— 只有该小节
正好是模板最后一段时才会吞（单条分析的默认路径正是如此）。

覆盖点：真实 prompt 形状 / keywords 无垃圾词 / 后面还有别的【小节时不回归 /
退化分支仍工作 / 用户自己以「请」开头的正常中文不被误删（最容易误伤处）/
真实模板的末尾指令行都被覆盖 / 走完整 pipeline 的潜台词干净。

临时数据目录；绝不碰 backend/data/wingman.db。pytest 与 python 直跑双通。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parent
SAMPLE = PROJECT / "samples" / "qq_sample_小鹿.txt"
REAL_DB = BACKEND / "data" / "wingman.db"
PEER_LINE = "哈哈哈今天好累啊"

sys.path.insert(0, str(BACKEND))

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None

JUNK_WORDS = ("分析这条", "析这条消", "请分析", "条消息", "请给出", "请推演", "本轮策略")


class _SkipTest(Exception):
    pass


def _skip(reason: str) -> None:
    if pytest is not None and os.environ.get("PYTEST_CURRENT_TEST"):
        pytest.skip(reason)
    raise _SkipTest(reason)


# ---------------------------------------------------------------- 工具


def _real_analyze_prompt(peer_message: str = PEER_LINE, *, extra_section: str = "") -> str:
    """用真实的 prompts.analyze_user 造 prompt（不手抄模板，避免与实现漂移）。"""
    from app.engine import prompts

    return prompts.analyze_user(
        profile="小鹿，24 岁，做设计",
        stage="熟悉期",
        taboos="宝贝",
        facts="[对方] 喜欢猫",
        summaries="（无）",
        retrieved="（无）",
        recent="#1|我|在忙吗\n#2|小鹿|刚下班",
        peer_message=peer_message + extra_section,
    )


def _file_hash(path: Path) -> str:
    if not path.is_file():
        return "<missing>"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fresh_ctx():
    """临时数据目录 + Mock provider 的 ctx。"""
    from app import config

    tmpdir = Path(tempfile.mkdtemp(prefix="wingman_test_mock_extract_"))
    config.DATA_DIR = tmpdir
    config.get_settings.cache_clear()

    import app.context as context_module

    context_module.DATA_DIR = tmpdir
    context_module._CTX = None
    ctx = context_module.get_ctx()
    ctx.update_cfg({"llm_provider": "mock", "embedder": "hash", "asr_engine": "mock"})
    return ctx, tmpdir


# ---------------------------------------------------------------- 缺陷本体


def test_real_prompt_shape_extracts_clean_peer_message() -> None:
    from app.llm.mock import extract_peer_message

    literal = "【对方最新消息】\n哈哈哈今天好累啊\n\n请分析这条消息。"
    assert extract_peer_message(literal) == PEER_LINE

    real = _real_analyze_prompt()
    assert "请分析这条消息。" in real, "prompt 模板变了，这个用例的前提需要复核"
    assert extract_peer_message(real) == PEER_LINE
    assert "请分析" not in extract_peer_message(real)


def test_keywords_of_extracted_message_have_no_prompt_junk() -> None:
    from app.llm.mock import extract_peer_message, keywords

    literal = "【对方最新消息】\n哈哈哈今天好累啊\n\n请分析这条消息。"
    words = keywords(extract_peer_message(literal), 5)
    assert words == ["今天好累"], words
    for junk in ("分析这条", "析这条消", "请分析", "条消息"):
        assert junk not in words, f"关键词里仍有 prompt 垃圾词：{words}"

    # 真实模板同样干净
    real_words = keywords(extract_peer_message(_real_analyze_prompt()), 5)
    assert "今天好累" in real_words
    assert not any(junk in "".join(real_words) for junk in JUNK_WORDS), real_words


def test_section_followed_by_another_section_still_clean() -> None:
    """【对方最新消息】后面还有别的【段落】时，行为不许回归。"""
    from app.llm.mock import extract_peer_message

    text = (
        "【最近的对话】\n#1|小鹿|在吗\n\n"
        "【对方最新消息】\n哈哈哈今天好累啊\n\n"
        "【分析结论】\n情绪：疲惫\n\n请给出 4 条候选回复。"
    )
    assert extract_peer_message(text) == PEER_LINE


def test_degraded_branch_still_takes_last_meaningful_line() -> None:
    """退化分支（没有【对方最新消息】段落）不许退化。"""
    from app.llm.mock import extract_peer_message

    assert extract_peer_message("随便一段话\n她最后说了一句晚安") == "她最后说了一句晚安"
    assert extract_peer_message("【人物卡】\n小鹿\n\n她最后说了一句晚安") == "她最后说了一句晚安"
    # 退化分支里，纯指令行不是对方的话，应继续往前找
    assert extract_peer_message("她最后说了一句晚安\n\n请分析这条消息。") == "她最后说了一句晚安"
    assert extract_peer_message("") == ""


def test_peer_message_starting_with_qing_is_not_stripped() -> None:
    """最容易误伤的地方：用户自己的话以「请」开头（或含「请」）不能被删掉。"""
    from app.llm.mock import extract_peer_message, keywords

    samples = [
        "请我吃饭吧",
        "请我吃饭吧。",
        "明天请你吃饭。",
        "请你不要生气。",
        "请别乱说。",
        "她请了半天假",
    ]
    for message in samples:
        literal = f"【对方最新消息】\n{message}\n\n请分析这条消息。"
        assert extract_peer_message(literal) == message, message
        # 消息本身没有「请分析」这类词，关键词里也不该出现
        joined = "".join(keywords(extract_peer_message(literal), 5))
        assert "分析这条" not in joined and "析这条消" not in joined, (message, joined)

    # 多行消息：只剥末尾指令行，正文里的「请」保留
    multiline = "【对方最新消息】\n第一行\n请我吃饭吧\n\n请分析这条消息。"
    assert extract_peer_message(multiline) == "第一行\n请我吃饭吧"


def test_strip_only_touches_trailing_lines() -> None:
    from app.llm.mock import strip_prompt_tail

    # 正文中间出现同样的句子：不是末尾，不动
    middle = "请分析这条消息。\n哈哈哈今天好累啊"
    assert strip_prompt_tail(middle) == middle
    # 干净输入原样返回（不重排、不吞行）
    clean = "哈哈哈今天好累啊"
    assert strip_prompt_tail(clean) == clean
    # 末尾指令行 + 空行都剥掉
    assert strip_prompt_tail("哈哈哈今天好累啊\n\n请分析这条消息。\n") == PEER_LINE
    # 只有指令行时返回空串（而不是把指令当内容）
    assert strip_prompt_tail("请分析这条消息。") == ""


def test_read_section_strips_tail_when_section_is_last() -> None:
    from app.llm.mock import read_section

    text = (
        "【关系阶段】\n暧昧期\n\n"
        "【待推演回复】\n周末带你去换个环境\n\n"
        "请推演发出这句话之后，对话会怎么走。"
    )
    assert read_section(text, "待推演回复") == "周末带你去换个环境"
    assert read_section(text, "关系阶段") == "暧昧期"
    assert read_section(text, "不存在的段落") == ""

    # 真实 simulate 模板的最后一节同样不带指令行
    from app.engine import prompts

    user = prompts.simulate_user(
        profile="小鹿", stage="熟悉期", taboos="宝贝", style_samples="（无）",
        recent="（无）", peer_message=PEER_LINE, option_text="周末带你去换个环境",
    )
    section = read_section(user, "待推演回复")
    assert "请推演" not in section, section
    assert "周末带你去换个环境" in section


def test_prompt_tail_list_covers_real_templates() -> None:
    """防漂移：真实模板末尾的指令行必须能被剥离，不能静默失效。"""
    from app.engine import prompts
    from app.llm.mock import PROMPT_TAIL_LINES, strip_prompt_tail

    built = [
        _real_analyze_prompt(),
        prompts.strategy_user(profile="小鹿", stage="熟悉期", goal="想约她看展",
                              taboos="宝贝", peer_message=PEER_LINE, analysis="情绪：疲惫"),
        prompts.suggest_user(profile="小鹿", stage="熟悉期", goal="想约她看展", taboos="宝贝",
                             style_samples="（无）", recent="（无）", peer_message=PEER_LINE,
                             analysis="情绪：疲惫", strategy="阶段：熟悉期"),
        prompts.simulate_user(profile="小鹿", stage="熟悉期", taboos="宝贝", style_samples="（无）",
                              recent="（无）", peer_message=PEER_LINE, option_text="周末带你去换个环境"),
    ]
    for text in built:
        last_line = strip_prompt_tail(text).splitlines()[-1].strip()
        assert not re.fullmatch(r"请[^\n]{0,40}。", last_line), (
            f"模板末尾的指令行没被剥离：{last_line!r}；"
            f"请把它加进 PROMPT_TAIL_LINES（当前 {PROMPT_TAIL_LINES}）"
        )

    # 源码扫描：prompts.py 里独立成行的静态指令行都要在剥离表里。
    # 带 {占位符} 的那句（抽事实模板的尾巴，格式化后形如「…说话人是「我」的…」）
    # 无法用精确匹配覆盖；它所在的小节也不走 extract_peer_message / read_section，
    # 所以这里显式排除，避免留下一条永远修不掉的假报警。
    source = Path(prompts.__file__).read_text(encoding="utf-8")
    static_lines = set()
    for line in source.splitlines():
        candidate = line.strip().rstrip('"').strip()
        if re.fullmatch(r"请[^\n{]{0,30}。", candidate):
            static_lines.add(candidate)
    assert static_lines, "没在 prompts.py 里扫到静态指令行，这条防漂移断言需要复核"
    missing = sorted(static_lines - set(PROMPT_TAIL_LINES))
    assert not missing, f"prompts.py 里的静态指令行没进 PROMPT_TAIL_LINES：{missing}"


# ---------------------------------------------------------------- 下游可见效果


def test_mock_analyze_subtext_is_clean() -> None:
    """直接看用户能看到的字段：analysis.subtext 里不能再出现指令词。"""
    from app.engine import prompts
    from app.llm.mock import MockProvider

    payload = asyncio.run(MockProvider().chat_raw([
        {"role": "system", "content": prompts.ANALYZE_SYSTEM},
        {"role": "user", "content": _real_analyze_prompt()},
    ]))
    data = json.loads(payload)
    subtext = data["subtext"]
    assert "今天好累" in subtext, subtext
    for junk in JUNK_WORDS:
        assert junk not in subtext, f"潜台词里混入了 prompt 指令词 {junk!r}：{subtext}"
    assert data["topics"], "话题词不应为空"
    assert not any("分析" in topic for topic in data["topics"]), data["topics"]


def test_pipeline_subtext_is_clean_and_options_unchanged() -> None:
    """走一遍完整「分析 + 建议」（临时库）：潜台词干净、建议仍 4 条降序带依据。"""
    from app.adapters import registry
    from app.engine import pipeline
    from app.memory import profiler

    real_before = _file_hash(REAL_DB)
    ctx, tmpdir = _fresh_ctx()
    try:
        assert SAMPLE.exists(), f"示例文件缺失：{SAMPLE}"
        imported = registry.import_file(ctx.store, SAMPLE, chat_name="小鹿")
        assert imported.inserted > 40, f"只导入了 {imported.inserted} 条"
        assert asyncio.run(profiler.build_index(ctx, imported.chat_id)) > 0

        bundle = asyncio.run(pipeline.run_analysis(ctx, imported.chat_id, PEER_LINE))
        assert ctx.llm.name == "mock"
        assert bundle.trace.get("llm") == "mock"

        subtext = bundle.analysis.subtext
        assert "今天好累" in subtext, subtext
        for junk in JUNK_WORDS:
            assert junk not in subtext, f"潜台词里混入了 prompt 指令词 {junk!r}：{subtext}"

        # 其它行为不许变：4 条建议、按总分降序、每条都有打分依据
        assert len(bundle.options) == 4, [o.id for o in bundle.options]
        totals = [option.total for option in bundle.options]
        assert totals == sorted(totals, reverse=True), totals
        assert all(option.score_notes for option in bundle.options)
        assert all(option.text.strip() for option in bundle.options)
        assert "没有产出可用的回复建议" not in " ".join(bundle.warnings)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    assert _file_hash(REAL_DB) == real_before, "测试污染了真实的 backend/data/wingman.db"


# ---------------------------------------------------------------- 直跑入口


def _collect_tests() -> list:
    return [
        (name, obj)
        for name, obj in list(globals().items())
        if name.startswith("test_") and callable(obj)
    ]


def main() -> int:
    print("=" * 68, flush=True)
    print("Mock 潜台词回归：prompt 指令行不得混入对方消息（临时库）", flush=True)
    print("=" * 68, flush=True)
    failures: list[tuple[str, str]] = []
    skipped = 0
    tests = _collect_tests()
    for index, (name, function) in enumerate(tests, start=1):
        try:
            function()
        except _SkipTest as exc:
            skipped += 1
            print(f"[{index:>2}/{len(tests)}] SKIP {name} —— {exc}", flush=True)
            continue
        except Exception as exc:  # noqa: BLE001
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"[{index:>2}/{len(tests)}] FAIL {name} —— {type(exc).__name__}: {exc}", flush=True)
            continue
        print(f"[{index:>2}/{len(tests)}] OK   {name}", flush=True)
    print("-" * 68, flush=True)
    print(f"通过 {len(tests) - len(failures) - skipped} / 跳过 {skipped} / 失败 {len(failures)}", flush=True)
    if failures:
        for name, error in failures:
            print(f"  FAILED {name}: {error}", flush=True)
        print("结果：失败", flush=True)
        return 1
    print("结果：全部通过。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
