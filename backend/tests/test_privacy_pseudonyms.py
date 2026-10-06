"""仓库里不许出现能反查到真人的信息。

**背景**：采集功能的用例原来用的是真人的名字 —— 那是在本机对着真实聊天记录做验证时
留下来的。而这个仓库是公开的，名字一旦留在代码、文档和界面占位符里，就等于把它发布了。
现在统一改成虚构的「小鹿」（`samples/qq_sample_小鹿.txt` 里的示例数据本来就叫这个名字）。

**覆盖两条通道**：① 文件内容（代码 / 文档 / 页面里的姓名）；② **提交元数据**
（author / committer 邮箱 —— 它不在文件里，但公开仓库的每个提交页都印着）。
两条都堵上，才算「仓库里没有可反查的信息」。

**为什么被禁的字眼要写成 `\\uXXXX` 转义**：这个文件的职责是「别让某几个字出现在仓库里」。
如果它自己把那个名字明明白白写出来，它就成了它要防的那个来源 ——
而且任何人拿 grep 搜仓库时，第一个命中的会是这个守卫文件本身，那就彻底失去意义了。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

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


def _git(*args: str) -> tuple[int, str]:
    """跑一条 git 命令。拿不到 git（比如只拷了源码目录）就当失败，由调用方决定怎么办。"""
    try:
        proc = subprocess.run(["git", *args], cwd=REPO, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    except OSError:
        return 127, ""
    return proc.returncode, proc.stdout


def test_the_history_does_not_contain_the_name_either() -> None:
    """**历史提交**里也不许有 —— 把当前文件改掉是清不干净的。

    这条是重写历史之后补上的。起因：名字是先落进提交、后来才在工作区里改掉的，
    于是 `git log -S` 一搜就能搜出来。而公开仓库的历史会被完整 clone 走，
    所以「当前文件干净」并不等于「没泄漏」—— 差的那一步就是把历史一起重写。

    浅克隆（CI 默认 `fetch-depth: 1`）里根本没有历史可搜，这时**跳过而不是通过**：
    「搜不到」和「搜过且干净」是两件事，混为一谈会让这条守卫在 CI 上变成摆设。
    """
    if _git("rev-parse", "--is-inside-work-tree")[0] != 0:
        pytest.skip("不在 git 工作区里（只拷了源码目录？），跳过历史扫描")
    shallow = _git("rev-parse", "--is-shallow-repository")
    if shallow[0] == 0 and shallow[1].strip() == "true":
        pytest.skip("浅克隆，历史不完整 —— 跳过（不是「已验证干净」）")

    hits: list[str] = []
    for bad, why in FORBIDDEN.items():
        code, out = _git("log", "--all", "-S", bad, "--oneline")
        if code != 0:
            pytest.skip(f"git log 执行失败（退出码 {code}），跳过历史扫描")
        commits = [line.split(" ", 1)[0] for line in out.strip().splitlines()] if out.strip() else []
        if commits:
            hits.append(f"「{bad}」（{why}）出现在 {len(commits)} 个提交里："
                        + "、".join(commits[:5]) + ("…" if len(commits) > 5 else ""))
    assert not hits, (
        "git 历史里还有不该出现的真实姓名：\n  " + "\n  ".join(hits)
        + "\n只改工作区是不够的 —— 已经提交过的内容要重写历史才清得掉，"
          "而且公开仓库的历史别人 clone 得到。"
    )


# 允许的匿名邮箱形态。GitHub 的 `@users.noreply.github.com` 是官方给提交用的匿名地址，
# 也是「在公开仓库里留下提交、但不暴露真实邮箱」的标准做法。
ANON_EMAIL_SUFFIXES = ("@users.noreply.github.com",)
ANON_EMAIL_EXACT = {"noreply@github.com", "noreply@anthropic.com", "action@github.com"}


def _is_anonymous(email: str) -> bool:
    return email in ANON_EMAIL_EXACT or email.endswith(ANON_EMAIL_SUFFIXES)


def test_commit_metadata_does_not_carry_a_real_email() -> None:
    """提交的 author / committer 邮箱不许是能反查到真人的邮箱。

    **为什么单列一条**：姓名守卫扫的是**文件内容**，而邮箱不在文件里 —— 它在 commit
    对象的元数据里。GitHub 会把每个提交的作者邮箱明明白白印在提交页和 API 上，
    公开仓库里任何人都能翻。所以「文件干净」并不等于「没泄漏」，这是另一条通道。

    本仓库先前正是这样：42 个提交的作者邮箱是一个真实邮箱，而所有文件都是干净的。
    修法不是改文件，而是**重写全部历史**把 author/committer 一起换掉（见 CHANGELOG
    0.1.2 那条记录，同一类问题的先例）。

    **为什么这里不把那个邮箱写出来**：理由同本文件开头 —— 守卫的职责是让某个字符串
    不出现在仓库里，它自己就不能是那个字符串。所以这里只判断「是不是匿名形态」，
    不点名任何一个具体地址。

    浅克隆里 `git log` 仍能拿到邮箱，所以这条在 CI 上**照常生效**；
    只有在拿不到 git 时（比如只拷了源码目录）才跳过。
    """
    code, out = _git("log", "--all", "--format=%ae%n%ce")
    if code != 0:
        pytest.skip(f"拿不到 git log（退出码 {code}），跳过提交元数据扫描")

    emails = {line.strip() for line in out.splitlines() if line.strip()}
    leaked = sorted(e for e in emails if not _is_anonymous(e))
    assert not leaked, (
        "提交元数据里有真实邮箱，公开仓库上任何人可见：\n  " + "\n  ".join(leaked)
        + "\n改 git config 只影响之后的提交；已经提交过的要重写历史才清得掉。"
          "\n推荐用 GitHub 的匿名地址：<用户名>@users.noreply.github.com"
    )
