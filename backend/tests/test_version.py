"""版本号一致性守卫。

为什么需要这个文件：这一轮发版时我撞上了一个很典型的漂移 ——
`backend/app/__init__.py` 里写着 `0.1.0`，而我准备打的 tag 是 `v0.2.0`。
后果不是崩溃，而是**用户报 bug 时说不清自己装的是哪个包**：
exe 自检会打印 `WingMan v0.1.0`，下载页写着 v0.2.0，两边对不上。

这类"到处都要改、漏一处也不报错"的字段，靠人记是靠不住的，得有测试盯着。
"""
from __future__ import annotations

import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(rel: str) -> str:
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return f.read()


def _version() -> str:
    from app import __version__

    return __version__


def test_version_is_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", _version()), (
        f"版本号 {_version()!r} 不是 x.y.z 形式，README/CHANGELOG 的写法都会跟着乱"
    )


def test_changelog_has_entry_for_current_version() -> None:
    """改了版本号就必须在 CHANGELOG 里留下对应章节。

    每次发版都要动这个文件，最容易漏。漏了的直接后果是
    「这个版本改了什么」无从查起 —— 而下载页恰恰要靠它说明变化。
    """
    v = _version()
    text = _read("CHANGELOG.md")
    pattern = rf"^##\s*\[{re.escape(v)}\](?:\s*-\s*(\d{{4}}-\d{{2}}-\d{{2}}))?\s*$"
    m = re.search(pattern, text, re.MULTILINE)
    assert m, f"CHANGELOG.md 里没有 `## [{v}]` 章节 —— 版本号提了但没记变更"
    assert m.group(1), (
        f"CHANGELOG 的 `## [{v}]` 没写发布日期。"
        "`## [Unreleased]` 是没有日期的，说明这一节还没真正发出去。"
    )


def test_readme_advertises_current_version() -> None:
    """README 的「当前版本」要和代码里的版本号一致。"""
    v = _version()
    text = _read("README.md")
    m = re.search(r"\*\*当前版本：v([0-9.]+)\*\*", text)
    assert m, "README 里找不到「**当前版本：vX.Y.Z**」这一行"
    assert m.group(1) == v, (
        f"README 写的是 v{m.group(1)}，代码里是 v{v} —— 两边对不上"
    )


def test_release_notes_exist_for_current_version() -> None:
    """发版说明要在仓库里留档。

    scripts/make_release.py 要求 docs/releases/v<tag>.md 存在才会发版，
    这里再加一道：文件得进了版本库，而不是只躺在某人的临时目录里。
    发行说明是用户判断「该下哪个包」的唯一依据，值得被测试盯住。
    """
    v = _version()
    path = os.path.join(REPO, "docs", "releases", f"v{v}.md")
    assert os.path.isfile(path), f"缺少发行说明 docs/releases/v{v}.md"
    body = _read(f"docs/releases/v{v}.md")
    # 下载页最容易出问题的地方：两个包的区别没说清，用户下错。
    assert "WingMan-full" in body, "发行说明里没提到完整版，用户不知道该不该下它"
    assert "SHA256" in body or "sha256" in body, (
        "发行说明里没有校验和 —— 用户无法确认下载到的包没被替换"
    )


@pytest.mark.parametrize("rel", ["README.md", "docs/QUICKSTART.md"])
def test_no_stale_hardcoded_download_links(rel: str) -> None:
    """下载链接里的版本号不能停在旧版本上。

    指向一个不存在的 tag 的链接不会报错，只会 404 —— 静默失效。
    """
    v = _version()
    text = _read(rel)
    found = set(re.findall(r"releases/download/v([0-9.]+)/", text))
    stale = {f for f in found if f != v}
    assert not stale, f"{rel} 里有过期下载链接：v{', v'.join(sorted(stale))}（当前 v{v}）"
