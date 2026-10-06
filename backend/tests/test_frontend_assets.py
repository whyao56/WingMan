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


def test_version_mismatch_can_be_detected_by_the_page_itself(html: str) -> None:
    """界面必须能自己发现「我连的那个进程不是我这个版本」。

    背景是一个真实的误判：升级完之后旧进程还在跑，页面是新的、版本号是旧的，
    用户以为「你把 0.1.4 写成 0.1.3 了」。**版本号没写错，是进程没退干净** ——
    但界面自己不说，用户就只能靠猜。

    三件事缺一不可：一个自带的版本号、一处比对、一个能显示结论的容器。
    少任何一件，这段防呆都是死的（而且死得无声无息）。
    """
    script = html[html.index("<script>"):]
    assert re.search(r'^const BUILD\s*=\s*"[0-9.]+"', html, re.MULTILINE), \
        "前端没有 `const BUILD`，就没法判断自己和服务是不是同一个版本"
    assert "function checkBuildMatches" in script, "没有比对函数"
    assert re.search(r"await checkBuildMatches\(", script), \
        "比对函数定义了却没人调用 —— 那条横幅永远不会出现"
    assert 'id="ver-warn"' in html, "没有放结论的地方"


def test_frontend_collect_clients_stay_within_the_backend_allowlist(html: str) -> None:
    """半自动采集的客户端下拉，不能出现后端不认的值。

    这是一条**跨层**守卫：前端 `<option value="x">` 和后端 `SEMI_CLIENTS` 是
    两处独立维护的名单，漂移之后的表现是「选完点开始监听 → 404 不认识这个客户端」，
    而且只在选到那一项时才炸。加「其他聊天」正好踩在这个接缝上，所以钉住它。
    """
    from app.api.routes_collect import SEMI_CLIENTS

    m = re.search(r'<select id="sc-client">(.*?)</select>', html, re.S)
    assert m, "找不到半自动采集的客户端下拉"
    options = set(re.findall(r'<option value="([^"]+)"', m.group(1)))
    assert options, "这个下拉里一个选项都没解析到，说明匹配规则失效了"
    assert options <= set(SEMI_CLIENTS), (
        f"前端提供了后端不认的客户端：{sorted(options - set(SEMI_CLIENTS))}"
    )
    assert "other" in options, "「其他聊天」没有出现在半自动采集的下拉里"


def test_platform_selects_only_offer_platforms_the_backend_knows(html: str) -> None:
    """界面上所有「选平台/来源」的下拉，取值都要在后端的平台表里。

    又一条跨层守卫，理由和上面那条一样：两处独立维护的名单迟早分叉，
    而分叉的表现是「选中某一项之后才炸」——正常点测很难覆盖到。

    「粘贴文本」那个下拉是这一轮新加的：粘贴过来的内容**看起来**像什么
    （适配器按文本排布猜的）和它**实际是在哪说的**（只有用户知道）不是一回事，
    所以要给用户一个说清楚的机会。它的取值同样不能被后端当成陌生平台。
    """
    from app.store import _CHANNEL_BY_PLATFORM

    known = set(_CHANNEL_BY_PLATFORM)

    # 粘贴框的选项是静态 HTML，直接读；空值表示「自动判断」，不是平台
    m = re.search(r'<select id="paste-platform"[^>]*>(.*?)</select>', html, re.S)
    assert m, "找不到粘贴框的「来源」下拉"
    paste_values = {v for v in re.findall(r'<option value="([^"]*)"', m.group(1)) if v}
    assert paste_values, "这个下拉一个选项都没解析到，匹配规则可能失效了"
    assert not (paste_values - known), (
        f"paste-platform 提供了后端不认的平台：{sorted(paste_values - known)}"
        f"（后端认得：{sorted(known)}）"
    )

    # 渠道设置的下拉是 JS 用常量表拼的（要处理「保持原样」那一项），改从常量表读
    m = re.search(r"const CHAT_PLATFORMS = \[(.*?)\];", html, re.S)
    assert m, "找不到渠道设置的平台常量表 CHAT_PLATFORMS"
    chat_values = set(re.findall(r'\[\s*"([^"]+)"\s*,', m.group(1)))
    assert chat_values, "CHAT_PLATFORMS 一个值都没解析到，匹配规则可能失效了"
    assert not (chat_values - known), (
        f"CHAT_PLATFORMS 提供了后端不认的平台：{sorted(chat_values - known)}"
        f"（后端认得：{sorted(known)}）"
    )


def test_paste_box_offers_a_way_to_say_where_it_came_from(html: str) -> None:
    """粘贴框必须让用户能指定来源。

    粘贴过来的文本只能靠排布猜来源，而别的聊天工具导出的文本常常长成
    「微信的样子」。用户是唯一知道「这话是在哪说的」的人 ——
    不给他这个入口，机器就只能猜，猜错了还会静默贴上微信的标签。
    """
    m = re.search(r'<select id="paste-platform"[^>]*>(.*?)</select>', html, re.S)
    assert m, "粘贴框没有「来源」下拉，用户就没法纠正猜错的来源"
    values = set(re.findall(r'<option value="([^"]+)"', m.group(1)))
    assert "other" in values, "「来源」下拉里没有「其他聊天」"
    assert re.search(r"来源：自动判断", m.group(1)), "缺少「自动判断」这一项（要能啥都不选）"


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
