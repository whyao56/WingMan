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


def test_no_dead_view_links(html: str) -> None:
    """每个字面量的 goView("x") 都要有对应的 ``#view-x`` 容器。

    「记忆 / 导入 / 自检 / 通话」四页并进对象页、采集页与设置页之后，
    任何一个漏改的跳转都会变成一个**点了没反应**的按钮 —— 不报错、不提示，
    用户只会以为程序卡了。挪页面的时候最容易漏的就是这种角落里的跳转。
    """
    views = set(re.findall(r'id="view-([a-z]+)"', html))
    # 变量形式的调用（goView(b.dataset.x)）静态查不了，这里只钉字面量
    targets = set(re.findall(r'goView\("([a-z]+)"\)', html))
    assert views, "一个页面容器都没解析到，说明这个测试的匹配规则已经失效了"
    assert targets <= views, f"这些 goView 目标没有对应页面：{sorted(targets - views)}"


def test_legacy_deep_links_are_redirected(html: str) -> None:
    """旧的 ?view=memory / import / check / voice 深链接不能变成死链。

    老书签、别人发来的链接、以及自动化截图脚本都会带着这些参数，
    点了没反应等于「你这个软件坏了」。
    """
    for legacy in ("memory", "import", "check", "voice"):
        assert re.search(rf'"?{legacy}"?\s*:\s*"[a-z]+"', html), \
            f"旧深链接 ?view={legacy} 没有重定向映射"


def test_js_only_references_dom_ids_that_exist(html: str) -> None:
    """JS 里 ``$("#xxx")`` 提到的 id，最终必须有人创建它。

    「引用了没建的容器」是这套单文件前端最典型的静默失效：`$(...)` 返回 null，
    后面 `.innerHTML` / `.classList` 直接抛异常，整段初始化的后半部分不再执行 ——
    用户看到的是「某个按钮点了没反应」，控制台里才有一行错。

    创建点算两处：静态 HTML，以及脚本里用模板串拼出来的节点
    （弹窗、动态提示里 `id="xxx"` 的那些）。
    """
    script = html[html.index("<script>"):]
    # 静态 DOM + 脚本里拼出来的 DOM，都算「有人创建」
    created = set(re.findall(r'\bid="([^"]+)"', html))
    used = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', script))
    missing = sorted(used - created)
    assert not missing, f"JS 引用了谁都没创建的 id：{missing}"


# 「被调用但没定义」检查用的三张白名单表。
#
# 加进来之前先问自己：这个名字真的不需要在本文件里定义吗？
# 语言关键字与浏览器内置对象是天然如此的；CSS_FUNCS 是另一回事 ——
# 模板串里的内联样式（``style="width:min(680px,100%)"``）会带出 ``min()``
# 这类 CSS 函数，它们长得像 JS 调用但根本不是，只能显式排除。
_JS_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "typeof", "function", "new",
    "await", "do", "else", "throw", "delete", "void", "in", "of", "case", "yield",
    "instanceof", "async", "var", "let", "const", "class", "super", "this", "with",
    "try", "finally",
}

_BROWSER_GLOBALS = {
    "alert", "confirm", "prompt", "console", "fetch", "setTimeout", "setInterval",
    "clearTimeout", "clearInterval", "requestAnimationFrame", "queueMicrotask",
    "btoa", "atob", "structuredClone",
    "Number", "String", "Boolean", "Array", "Object", "JSON", "Math", "Date", "RegExp",
    "Error", "TypeError", "Promise", "Set", "Map", "WeakMap", "WeakSet", "Symbol", "Intl",
    "parseInt", "parseFloat", "isNaN", "isFinite",
    "encodeURIComponent", "decodeURIComponent",
    "Blob", "File", "FileReader", "FormData", "URL", "URLSearchParams",
    "TextEncoder", "TextDecoder", "CustomEvent", "Event", "AbortController", "Function",
    "Image", "Audio", "MutationObserver", "IntersectionObserver", "ResizeObserver",
}

_CSS_FUNCS = {
    "var", "min", "max", "clamp", "calc", "rgb", "rgba", "hsl", "hsla", "url", "env",
    "attr", "translate", "translateX", "translateY", "scale", "rotate", "repeat",
    "cubic-bezier", "linear-gradient",
}


def _defined_function_names(js: str) -> set[str]:
    """脚本里**被定义过**的标识符。

    宁可宽一点（宁可多算一个定义，也不要漏），这样守卫只会漏报、不会误报 ——
    一条会自己误报的守卫，最后一定会被人删掉。
    """
    names: set[str] = set()
    names |= set(re.findall(r"function\s+([A-Za-z_$][\w$]*)", js))       # 声明式 + 具名函数表达式
    names |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", js))
    names |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s+(?:of|in)\b", js))
    # 函数/箭头函数的参数表（含解构出来的名字，如 { onConfirm }）
    for m in re.finditer(r"(?:function\s*[A-Za-z_$]*\s*)?\(([^()]*)\)\s*(?:=>|\{)", js):
        names |= set(re.findall(r"[A-Za-z_$][\w$]*", m.group(1)))
    return names


def _called_function_names(js: str) -> dict[str, int]:
    """脚本里形如 ``name(`` 的调用。前面的字符不能是 ``.``（那是方法调用）或中文。"""
    out: dict[str, int] = {}
    for m in re.finditer(r"(^|[^\w$.\u4e00-\u9fa5])([A-Za-z_$][\w$]*)\s*\(", js):
        name = m.group(2)
        out[name] = out.get(name, 0) + 1
    return out


def test_no_calls_to_undefined_functions(html: str) -> None:
    """脚本里调用的每个名字，最终都要有人定义它。

    重构把 ``loadChats`` 并进 ``loadObjects`` 时漏改了 5 个调用点，而这条链子
    藏得很深：``init()`` 里那个 ``await loadChats()`` 一抛 ReferenceError，
    后面的读设置、自检、深链接就全都不执行了；指挥台那次更隐蔽 ——
    它在 ``try`` 里，于是「分析成功」的结果会被 ``catch`` 覆盖成一句
    ``loadChats is not defined``，看起来就是主功能坏了。

    单文件、无构建步骤的前端没有编译器帮忙，这条守卫补上那一环。
    """
    script = html[html.index("<script>"):]
    called = _called_function_names(script)
    assert called, "一个调用都没解析到，说明这个测试的匹配规则已经失效了"
    defined = _defined_function_names(script)
    unknown = sorted(
        n for n in called
        if n not in defined and n not in _JS_KEYWORDS
        and n not in _BROWSER_GLOBALS and n not in _CSS_FUNCS
    )
    assert not unknown, f"这些函数被调用了但没人定义：{unknown}"


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
