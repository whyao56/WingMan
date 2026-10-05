"""仓库里不许出现真人的名字。

**背景**：采集功能的用例原来用的是真人的名字 —— 那是在本机对着真实聊天记录做验证时
留下来的。而这个仓库是公开的，名字一旦留在代码、文档和界面占位符里，就等于把它发布了。
现在统一改成虚构的「小鹿」（`samples/qq_sample_小鹿.txt` 里的示例数据本来就叫这个名字）。

**为什么被禁的字眼要写成 `\\uXXXX` 转义**：这个文件的职责是「别让某几个字出现在仓库里」。
如果它自己把那个名字明明白白写出来，它就成了它要防的那个来源 ——
而且任何人拿 grep 搜仓库时，第一个命中的会是这个守卫文件本身，那就彻底失去意义了。
"""
from __future__ import annotations

import os
from pathlib import Path

REPO = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# 被禁的字眼 → 为什么禁。
# 汉字用 \uXXXX 转义、拼音用 + 拼起来写，都是同一个理由（见模块说明）：
# 这份文件自己也不许出现连续的那串字。
FORBIDDEN = {
    "\u5b59\u68a6": "真人姓名",
    "\u5b59\u68a6\u6d01": "同一姓名的长变体（用例里用来验「长名字不被前缀吃掉」）",
    "\u5c0f\u68a6\u68a6": "由该姓名派生的昵称",
    "sun" + "meng": "该姓名的拼音（测试里的 wxid 标识符用过）",
}

# 只看这些目录/后缀。刻意用白名单：新增一类文件忘了加进来，是漏检；
# 用黑名单则相反 —— 某个一直在变的目录（比如用户数据）会突然让测试变红。
SCAN_DIRS = ("backend/app", "backend/tests", "frontend", "docs", "samples", "scripts", "build")
SCAN_ROOT_FILES = ("README.md", "CHANGELOG.md")
TEXT_SUFFIXES = {".py", ".md", ".html", ".css", ".js", ".txt", ".json", ".yml", ".yaml",
                 ".ps1", ".cmd", ".spec", ".toml", ".ini", ".cfg"}
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "dist", "release",
             "_pyinstaller", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def _text_files() -> list[Path]:
    found: list[Path] = []
    for name in SCAN_ROOT_FILES:
        p = REPO / name
        if p.is_file():
            found.append(p)
    for rel in SCAN_DIRS:
        root = REPO / rel
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix.lower() in TEXT_SUFFIXES:
                    found.append(p)
    return found


def test_no_real_names_anywhere_in_the_tree() -> None:
    """代码 / 文档 / 页面里都不许出现那几个字眼。

    采集功能的用例、`clipboard.py` 的格式示例、界面上的输入提示都改用虚构的「小鹿」——
    这些地方本来是随便一个名字都行，那就用虚构的那个。
    """
    hits: list[str] = []
    for path in _text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for bad, why in FORBIDDEN.items():
            if bad in text:
                hits.append(f"{path.relative_to(REPO)} 含「{bad}」（{why}）")
    assert not hits, (
        "仓库里出现了不该出现的真实姓名：\n  " + "\n  ".join(hits)
        + "\n改用虚构的「小鹿」，或把示例里的人物换成别的虚构名字。"
    )


def test_the_guard_does_not_spell_the_name_out_itself() -> None:
    """守卫自己也不许把名字写出来 —— 否则它既是防线，也是泄漏点。

    上面那条已经覆盖了本文件，这里单列一条是为了报错时说得更直白：
    有人为了让禁词「看得清楚」把转义改回明文时，它会立刻变红。
    """
    own = Path(__file__).read_text(encoding="utf-8")
    leaked = [f"{why}" for bad, why in FORBIDDEN.items() if bad in own]
    assert not leaked, (
        "守卫文件自己把名字写成了明文：" + "、".join(leaked)
        + "。请保持 \\uXXXX 转义写法（理由见本文件开头的说明）。"
    )
