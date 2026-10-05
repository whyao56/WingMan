"""把打好包的 exe 发布成 GitHub Release —— 一条命令发版。

为什么需要这个脚本，而不是手动点网页：
  发布涉及 4 步（打包 → 算校验和 → 建 Release → 传附件），手点容易漏掉校验和、
  或者传了个旧包上去。脚本把「包和校验和必须同源」这件事变成默认行为。

为什么 exe 走 Release 而不是提交进仓库：
  1. dist/ 在 .gitignore 里，二进制产物不该进版本历史（仓库会膨胀到几百 MB，
     而且每次改代码都多一份副本，历史再也瘦不回去）。
  2. GitHub 单文件硬限制 100MB —— 完整版解压后 237MB，根本推不上去。
  Release 附件就是官方为此设计的出口：可下载、有稳定 URL、可校验、不污染历史。

用法：
    python scripts/make_release.py --check       # 只打包、打印校验和，不上传
    python scripts/make_release.py --dry-run     # 再加一步模拟上传
    python scripts/make_release.py               # 真发版（建 tag、传附件、发布）

版本号默认从 backend/app/__init__.py 里读 —— 这个脚本本身就是为「防止版本号漂移」
而写的，自己的默认值再硬编码一个版本就自相矛盾了。要覆盖用 --version 0.2.1 --tag v0.2.1。

前置条件：先跑过 `python scripts/build_exe.py`，产物归位成 dist/WingMan
（只发一个包，见 docs/DESKTOP.md 的发布检查清单）。

凭据从 git 的凭据管理器读取（git credential fill），不落盘、不进环境变量。
需要 token 具备 repo 权限。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://api.github.com"
UPLOAD = "https://uploads.github.com"

# 只剩一个包了：语音能力撤下后，「标准版 / 完整版」曾经唯一的差异
# （是否内置 faster-whisper）不复存在，那个 237MB 的包随之消失。
PACKAGES = [
    (os.path.join("dist", "WingMan"), "WingMan-{version}-win64", "桌面版"),
]

# 写在压缩包里的使用说明。新手最容易踩的坑写在最前面。
NOTE = """WingMan · 聊天僚机 {version}
=================================

怎么用
------
1. 把整个文件夹解压到任意位置（桌面、D 盘都行，路径有中文也没关系）
2. 双击 WingMan.exe

【重要】不要只把 WingMan.exe 单独拖出来运行。
    它旁边的 _internal 文件夹是程序的一部分，少一个文件都起不来。
    要挪位置就整个文件夹一起挪。

关于语音识别
------------
这个版本暂不提供「通话实时转写」，程序里的「通话」页保留了设计思路。
聊天记录采集、人物画像、回复建议这些功能不受影响，也不需要额外的语音依赖。

数据存在哪
----------
聊天记录、设置都在：
    C:\\Users\\<你的用户名>\\AppData\\Local\\WingMan
卸载 = 删掉程序文件夹 + 删掉上面这个目录。
换个位置解压程序，数据不会丢。

起不来怎么办
------------
先在命令行里跑一次自检，它会直接告诉你缺什么：
    WingMan.exe --check
结果同时写在数据目录的 logs\\selfcheck.txt（就是上面「数据存在哪」那个目录）。

开源地址：https://github.com/{repo}
"""


def human(n: int) -> str:
    return f"{n / 1048576:.1f} MB"


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def current_version() -> str:
    """从代码里读当前版本号。

    为什么不直接写个默认值：这个脚本的存在意义就是「让版本号别再漂移」，
    它自己的默认值再硬编码一个版本，下次升版本时就会第一个过期 ——
    而且过期得不明显（不加 --version 时会打出一个版本号与代码不符的包）。
    """
    path = os.path.join(ROOT, "backend", "app", "__init__.py")
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r'__version__\s*=\s*"([^"]+)"', f.read())
    except OSError as e:  # noqa: BLE001
        raise SystemExit(f"读不出版本号：{e}")
    if not m:
        raise SystemExit(f'{path} 里找不到 __version__ = "x.y.z"')
    return m.group(1)


def token() -> str:
    """从 git 凭据管理器取 token。不落盘、不打印。"""
    try:
        out = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, cwd=ROOT, timeout=60,
        ).stdout
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"调用 git credential fill 失败：{e}")
    for line in out.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    raise SystemExit(
        "取不到 GitHub 凭据。确认这台机器能 git push（git credential fill 应返回 password）。"
    )


def call(method: str, url: str, tok: str, *, data=None, raw: bytes | None = None,
         ctype: str = "application/json"):
    body = raw if raw is not None else (json.dumps(data).encode() if data else None)
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"token {tok}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "wingman-make-release")
    if body:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            text = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(text) if text.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", "replace")}


def pack_one(src: str, top: str, version: str, repo: str, out_dir: str):
    """打一个带顶层目录的 zip，返回 (路径, 大小, sha256, 源文件数)。

    刻意做成**可复现**的：同样输入两次跑出**字节相同**的 zip。
    为什么值得多写这几行：zip 条目默认记录文件 mtime，而「使用说明.txt」是当场生成的，
    时间就是打包那一刻 —— 于是同样的代码重打一次，字节就变了、SHA256 也变了。
    后果是发行说明里印的校验和**永远复现不出来**，用户想核对反而以为下到了坏包。
    这里把时间戳钉死、遍历排序，让校验和成为可验证的事实而不是一次性的快照。
    """
    zpath = os.path.join(out_dir, top + ".zip")
    raw = n = 0
    files = []
    for r, _, fs in os.walk(src):
        for f in fs:
            p = os.path.join(r, f)
            if os.path.exists(p):
                raw += os.path.getsize(p)
                n += 1
                files.append(p)
    files.sort()  # os.walk 的顺序不保证跨调用一致，排序才是可复现的前提

    os.makedirs(out_dir, exist_ok=True)
    # DEFLATED：包里大量 DLL 能压掉一半以上，用户下载量直接少几十 MB。
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        info = zipfile.ZipInfo(f"{top}/使用说明.txt", date_time=(1980, 1, 1, 0, 0, 0))
        info.external_attr = 0o644 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, NOTE.format(version=version, repo=repo))
        for full in files:
            inner = os.path.relpath(full, src).replace(os.sep, "/")
            zf.write(full, f"{top}/{inner}")
    return zpath, os.path.getsize(zpath), sha256(zpath), n


def main() -> int:
    ap = argparse.ArgumentParser(description="把打好包的 exe 发布成 GitHub Release")
    ap.add_argument("--version", default=None,
                    help="版本号，默认取 backend/app/__init__.py 里的 __version__")
    ap.add_argument("--tag", default=None, help="tag 名，默认 v<version>")
    ap.add_argument("--repo", default=None, help="owner/name，默认取 origin 的地址")
    ap.add_argument("--notes", default=None, help="发行说明文件，默认 docs/releases/<tag>.md")
    ap.add_argument("--target", default="main", help="tag 指向的分支（默认 main）")
    ap.add_argument("--draft", action="store_true", help="只建草稿，不发布")
    ap.add_argument("--check", action="store_true", help="只打包并打印校验和，不上传")
    ap.add_argument("--dry-run", action="store_true", help="打包 + 模拟上传，不真的调 API")
    args = ap.parse_args()

    version = args.version or current_version()
    tag = args.tag or f"v{version}"
    repo = args.repo
    if not repo:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT,
                             capture_output=True, text=True).stdout.strip()
        repo = url.split("github.com")[-1].strip("/:").removesuffix(".git")
    notes_path = args.notes or os.path.join(ROOT, "docs", "releases", f"{tag}.md")
    out_dir = os.path.join(ROOT, "release")

    print(f"仓库 {repo} · tag {tag} · 版本 {version}\n")

    if not os.path.isfile(notes_path):
        print(f"!! 找不到发行说明 {notes_path}")
        print("   先写一份再发布 —— 空白说明会让下载的人不知道选哪个包。")
        return 2

    built = []
    for rel, tpl, label in PACKAGES:
        src = os.path.join(ROOT, rel)
        if not os.path.isdir(src):
            print(f"!! 缺产物 {rel}，先跑 scripts/build_exe.py")
            return 2
        top = tpl.format(version=version)
        print(f"== 打包 {label}：{rel}")
        zpath, size, digest, n = pack_one(src, top, version, repo, out_dir)
        print(f"   {n} 个文件 -> {os.path.basename(zpath)}  {human(size)}")
        print(f"   SHA256 {digest}\n")
        built.append((os.path.basename(zpath), zpath, size, digest))

    print("=== 校验和汇总（请贴进发行说明）===")
    for name, _, size, digest in built:
        print(f"{digest}  {name}")

    if args.check:
        print("\n--check：到此为止，未上传。")
        return 0

    body = open(notes_path, encoding="utf-8").read()
    if args.dry_run:
        print(f"\n--dry-run：将使用说明 {notes_path}（{len(body)} 字符），"
              f"上传 {len(built)} 个附件，tag={tag} -> {args.target}。未真的调用 API。")
        return 0

    tok = token()
    print(f"\n== 检查 {tag} 是否已存在")
    st, existing = call("GET", f"{API}/repos/{repo}/releases/tags/{tag}", tok)
    if st == 200:
        print(f"!! 已存在：{existing.get('html_url')}")
        print("   本脚本刻意不覆盖已发布的 Release（避免误删别人的下载记录）。")
        print("   要重发请先到网页上删掉，或换一个 tag。")
        return 1

    print(f"== 创建{'草稿' if args.draft else ''} Release")
    st, rel = call("POST", f"{API}/repos/{repo}/releases", tok, data={
        "tag_name": tag,
        "target_commitish": args.target,
        "name": f"WingMan {tag}",
        "body": body,
        "draft": bool(args.draft),
        "prerelease": False,
    })
    if st not in (200, 201):
        print(f"!! 创建失败 ({st})：{str(rel)[:400]}")
        return 1
    rid = rel["id"]
    print(f"   id={rid}  {rel['html_url']}\n")

    for name, path, size, digest in built:
        print(f"== 上传 {name}（{human(size)}）")
        with open(path, "rb") as f:
            payload = f.read()
        st, asset = call("POST",
                         f"{UPLOAD}/repos/{repo}/releases/{rid}/assets?name={name}",
                         tok, raw=payload, ctype="application/zip")
        if st not in (200, 201):
            print(f"!! 上传失败 ({st})：{str(asset)[:400]}")
            print("   草稿仍保留，可重试或到网页手动补传。")
            return 1
        print(f"   OK {asset['size']} 字节\n")

    if args.draft:
        print(f"草稿已就绪，去网页确认后点发布：{rel['html_url']}")
        return 0

    print("== 发布（草稿 -> 公开）")
    st, rel2 = call("PATCH", f"{API}/repos/{repo}/releases/{rid}", tok, data={"draft": False})
    if st != 200:
        print(f"!! 发布失败 ({st})：{str(rel2)[:400]}")
        return 1
    print(f"   完成：{rel2['html_url']}")
    for a in rel2.get("assets", []):
        print(f"   附件：{a['name']}  {human(a['size'])}")
    print("\n别忘了把 README 里的下载链接更新到新 tag。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
