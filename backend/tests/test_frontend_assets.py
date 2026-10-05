"""前端静态资源的守卫测试。

这一轮踩到的两个坑都属于「改一处、坏一片」的静默失效，人眼很难发现，
所以钉成测试：

1. ``.sub`` 被同时用作「各页副标题段落」和「通话字幕行」。后者的
   ``display:grid; grid-template-columns:66px 1fr`` 静默覆盖前者，把每个
   页面的副标题都压成 66px 宽的一列 —— 用户看到的就是「字都挤在一起」。
2. 预设里的 DeepSeek 模型名 ``deepseek-chat`` 在 2026-07-24 被官方下线，
   新手点一下按钮就撞上一个看不懂的 400。
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    return FRONTEND.read_text(encoding="utf-8")


def _css(html: str) -> str:
    a = html.index("<style>") + len("<style>")
    b = html.index("</style>", a)
    return html[a:b]


def _presets_block(html: str) -> str:
    """MODEL_PRESETS 定义段（不含退役提示表）。"""
    return html[html.index("const MODEL_PRESETS"):html.index("const RETIRED_MODELS")]


def test_frontend_file_exists() -> None:
    assert FRONTEND.is_file(), f"找不到前端入口：{FRONTEND}"


def test_no_duplicate_top_level_class_rules(html: str) -> None:
    """同一套 CSS 里不允许有两个「顶层同名类」规则。

    要复用样式，请用逗号合并选择器（``.a,.b{...}``）或换个名字；
    分成两条独立规则时，后者会静默覆盖前者 —— 正是 .sub 那次事故的成因。
    """
    names = re.findall(r"(?m)^\.([a-zA-Z0-9_-]+)\s*[,{]", _css(html))
    dup = sorted(n for n, c in Counter(names).items() if c > 1)
    assert not dup, f"CSS 里出现重复定义的顶层类：{dup}"


def test_caption_uses_its_own_class(html: str) -> None:
    """通话字幕行必须用 .cap，不能再抢占 .sub（副标题）。"""
    css = _css(html)
    assert ".cap{" in css
    assert ".sub{display:grid" not in css.replace(" ", "")


def test_presets_do_not_use_retired_deepseek_models(html: str) -> None:
    """预设里不能出现已下线的模型名。

    ``deepseek-chat`` / ``deepseek-reasoner`` 只允许出现在 RETIRED_MODELS
    退役提示表里（那是用来帮老用户一键改掉的），不能作为「推荐预设」。
    """
    presets = _presets_block(html)
    assert "deepseek-chat" not in presets
    assert "deepseek-reasoner" not in presets
    assert "deepseek-flash" in presets, "预设里应给出当前可用的 DeepSeek 模型名"


def test_retired_models_have_replacements(html: str) -> None:
    """退役表里每条都要给出替换目标，否则提示弹出来用户也不知道该改什么。"""
    block = html[html.index("const RETIRED_MODELS"):html.index("// 见到退役的模型名")]
    pairs = re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', block)
    assert pairs, "RETIRED_MODELS 解析为空"
    for old, new in pairs:
        assert old.strip() and new.strip(), f"退役项 {old} 缺少替换目标"
        assert old != new
