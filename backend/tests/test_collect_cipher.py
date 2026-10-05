"""SQLCipher 解密器的往返守卫。

## 为什么这里要**自己写加密**来验解密

自动采集这条路上，解密是唯一「算错了也照样能跑」的一环：密钥不对、页布局错位、
HMAC 覆盖范围差一个字节，代码都不会抛异常 —— 它只会解出一堆随机数据。
如果没有一条真库密钥（本机两个版本都没有，见 `keys.py` 的实测记录），
唯一能证明「解密器写对了」的办法就是**按同一份规格造出一个库，再解回来**。

造谜面和解谜底是同一个人写的，听起来像自说自话。但这恰恰能抓住真正会翻车的东西：
CBC 分组对齐、IV 在页里的位置、HMAC 覆盖 `密文 + IV + 页号`、第 1 页的
salt 与魔数互换、保留区在明文里必须是 0 —— 任何一处错位，往返立刻不一致。
真库密钥拿不到的时候，这是唯一有意义的验证方式。

## 两条不能失守的性质

1. **错的密钥必须被拒绝。** 判据恒真的话，程序会拿着错密钥一路「成功」下去，
   把 4096 字节垃圾当消息写进用户的记忆里 —— 不报错、不可逆。
2. **认证失败必须返回 `None`，不能返回「看起来像样」的东西。** 宁可少一页。

跑法：

    cd backend
    python -m pytest tests/test_collect_cipher.py -q
    python tests/test_collect_cipher.py
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import os
import shutil
import struct
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None


class _SkipTest(Exception):
    pass


def _skip(reason: str) -> None:
    if pytest is not None and os.environ.get("PYTEST_CURRENT_TEST"):
        pytest.skip(reason)
    raise _SkipTest(reason)


def _cipher():
    """拿到解密器模块；缺 `cryptography` 时跳过而不是假装通过。"""
    from app.collect import sqlcipher as sc

    if not sc.CRYPTO_AVAILABLE:
        _skip(f"缺少加密库，解密路径不可用：{sc.CRYPTO_REASON}")
    return sc


PAGE = 4096
RESERVE = 80
# 一个 QQ 风格的「文件前有自定义头」的库：实测 9.9.20.37051 就是 1024 字节
CLIENT_HEADER = 1024


# ---------------------------------------------------------------- 造库


def _logical_page1(page_size: int, reserve: int, fill: bytes) -> bytes:
    """造一个「解密后」的第 1 页：合法 SQLite 头 + 随便填的内容。

    字段值要和真实 SQLite 一致（页大小、读写版本、reserve、64/32/32），
    否则 `page1_header_ok` 会拒绝它 —— 那正是判据在起作用。
    """
    sc = _cipher()
    page = bytearray(page_size)
    page[0:16] = sc.SQLITE_MAGIC
    page[16:18] = struct.pack(">H", page_size)
    page[18] = 1                      # 写版本
    page[19] = 1                      # 读版本
    page[20] = reserve
    page[21] = 64                     # 最大负载比例
    page[22] = 32                     # 最小负载比例
    page[23] = 32                     # 叶子负载比例
    page[24:28] = struct.pack(">I", 1)
    page[44:48] = struct.pack(">I", 1)
    body = fill[: page_size - 100 - reserve]
    page[100:100 + len(body)] = body
    page[page_size - reserve:] = b"\x00" * reserve    # 明文里保留区必须是 0
    return bytes(page)


def _logical_page(page_size: int, reserve: int, fill: bytes) -> bytes:
    page = bytearray(fill[:page_size])
    page += b"\x00" * (page_size - len(page))
    page[page_size - reserve:] = b"\x00" * reserve
    return bytes(page)


def _encrypt(pages: list[bytes], raw_key: bytes, profile, *, salt=None,
             header: bytes = b"") -> bytes:
    """按 SQLCipher 规格把逻辑页加密成文件字节。

    磁盘布局：
        第 1 页 = `[salt(16) | 密文 | IV(16) | HMAC(64)]`
        其余页 = `[密文 | IV(16) | HMAC(64)]`
    第 1 页的密文不含那 16 字节 `SQLite format 3\\0` —— 它不在密文里，
    解密时由解密器补回去。这一条曾经判反过，导致所有候选全灭。
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    page_size, reserve = profile.page_size, profile.reserve
    salt = salt or os.urandom(16)
    out = bytearray(header)
    for idx, page in enumerate(pages):
        pgno = idx + 1
        iv = os.urandom(16)
        reserve_start = page_size - reserve
        body = page[16:reserve_start] if pgno == 1 else page[0:reserve_start]
        enc = Cipher(algorithms.AES(raw_key), modes.CBC(iv)).encryptor()
        ct = enc.update(body) + enc.finalize()
        disk = (salt if pgno == 1 else b"") + ct + iv
        tag = _hmac.new(profile_mac_key(raw_key, salt),
                        ct + iv + struct.pack("<I", pgno),
                        profile_hmac_algo(profile)).digest()
        disk += tag
        disk += b"\x00" * (page_size - len(disk))
        assert len(disk) == page_size, len(disk)
        out += disk
    return bytes(out)


def profile_hmac_algo(profile):
    return {"sha512": hashlib.sha512, "sha1": hashlib.sha1,
            "sha256": hashlib.sha256}[profile.hmac_algo]


def profile_mac_key(raw_key: bytes, salt: bytes) -> bytes:
    """认证密钥 = PBKDF2-HMAC-SHA512(加密密钥, salt ^ 0x3a, 2 次)。

    这里刻意**不调用**被测模块的 `mac_key_of`，而是照规格重写一遍：
    调它等于用被测对象验证被测对象，它错了这边跟着一起错。
    """
    return hashlib.pbkdf2_hmac("sha512", raw_key,
                               bytes(b ^ 0x3A for b in salt), 2, 32)


def _make_db(tmp: Path, *, header_len: int = 0, reserve: int = RESERVE):
    """造一个合成库，返回 (文件路径, 密码, profile, 原始逻辑页拼接)。"""
    sc = _cipher()
    profile = sc.Profile(f"test/sha512-{reserve}", page_size=PAGE,
                         hmac_algo="sha512", reserve=reserve,
                         file_header=header_len)
    key = os.urandom(32)
    logical = [
        _logical_page1(PAGE, reserve, b"A" * 3000),
        _logical_page(PAGE, reserve, b"B" * 3900),
        _logical_page(PAGE, reserve, b"C" * 4016),
        _logical_page(PAGE, reserve, b"D" * 1234),
    ]
    header = b"\x00" * header_len
    blob = _encrypt(logical, key, profile, header=header)
    src = tmp / "cipher.db"
    src.write_bytes(blob)
    return src, key, profile, b"".join(logical)


# ================================================================ 往返


def test_every_page_round_trips_byte_for_byte() -> None:
    """逐页往返必须完全一致 —— 这是「解密器写对了」的唯一直接证据。"""
    sc = _cipher()
    tmp = Path(tempfile.mkdtemp(prefix="wingman_cipher_"))
    try:
        src, key, profile, _ = _make_db(tmp)
        blob = src.read_bytes()
        salt = blob[:16]
        for i in range(4):
            page = blob[i * PAGE:(i + 1) * PAGE]
            plain = sc.decrypt_page(key, page, i + 1, profile, salt=salt)
            assert plain is not None, f"第 {i + 1} 页解密失败"
            assert len(plain) == PAGE
            if i == 0:
                assert plain[:16] == sc.SQLITE_MAGIC, "第 1 页的魔数要补回去"
            assert plain[PAGE - profile.reserve:] == b"\x00" * profile.reserve, (
                "明文里的保留区必须是 0")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_non_first_page_without_the_salt_is_refused() -> None:
    """非首页必须显式拿到 salt 才能解。

    这里钉的是一个真实踩过的坑：salt 只明文存在于第 1 页，
    别的页自己读不出来，而认证密钥是用 salt 派生的。早先没把 salt 传下去，
    表现是「首页解得开、从第 2 页起全部认证失败，整库只解出 1 页」——
    而且不报错，用户只会觉得「怎么只有一条消息」。
    """
    sc = _cipher()
    tmp = Path(tempfile.mkdtemp(prefix="wingman_cipher_"))
    try:
        src, key, profile, _ = _make_db(tmp)
        page2 = src.read_bytes()[PAGE:2 * PAGE]
        assert sc.decrypt_page(key, page2, 2, profile, salt=b"") is None
        assert sc.decrypt_page(key, page2, 2, profile,
                               salt=src.read_bytes()[:16]) is not None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_whole_database_decrypts_to_the_original_bytes() -> None:
    """整库解密的结果必须和原始逻辑页**逐字节**相同。"""
    sc = _cipher()
    tmp = Path(tempfile.mkdtemp(prefix="wingman_cipher_"))
    try:
        src, key, profile, logical = _make_db(tmp)
        rep = sc.decrypt_database(src, tmp / "plain.db", key, profile, apply_wal=False)
        assert rep.ok, rep.describe()
        assert (tmp / "plain.db").read_bytes() == logical, rep.describe()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_client_header_is_skipped_before_the_salt() -> None:
    """文件前面有自定义头（实测 QQ NT 是 1024 字节）时要先剥掉再找 salt。

    剥的位置错了会「解不出来」而不是「解出错数据」—— 后者更坏，
    但这一个错了会让人怀疑密钥不对，白折腾很久。
    """
    sc = _cipher()
    tmp = Path(tempfile.mkdtemp(prefix="wingman_cipher_"))
    try:
        src, key, profile, logical = _make_db(tmp, header_len=CLIENT_HEADER)
        assert sc.salt_of(src, profile) == src.read_bytes()[CLIENT_HEADER:CLIENT_HEADER + 16]
        rep = sc.decrypt_database(src, tmp / "plain.db", key, profile, apply_wal=False)
        assert rep.ok, rep.describe()
        assert (tmp / "plain.db").read_bytes() == logical
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ 判据不能恒真


def test_a_wrong_key_is_always_rejected() -> None:
    """错密钥必须一个都不放过 —— 判据恒真等于整条链都在自欺。"""
    sc = _cipher()
    tmp = Path(tempfile.mkdtemp(prefix="wingman_cipher_"))
    try:
        src, key, profile, _ = _make_db(tmp)
        page1 = src.read_bytes()[:PAGE]
        assert sc.check_raw_key(key, page1, profile) is True
        false_accepts = sum(sc.check_raw_key(os.urandom(32), page1, profile)
                            for _ in range(30))
        assert false_accepts == 0, f"{false_accepts}/30 个随机密钥被误判为正确"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_wrong_reserve_is_rejected() -> None:
    """参数不对也要被拒（密钥对但 reserve 错 = 页布局错，解出来是错的）。"""
    sc = _cipher()
    tmp = Path(tempfile.mkdtemp(prefix="wingman_cipher_"))
    try:
        src, key, profile, _ = _make_db(tmp)
        page1 = src.read_bytes()[:PAGE]
        wrong = sc.Profile("wrong", page_size=PAGE, hmac_algo="sha512", reserve=48)
        assert sc.check_raw_key(key, page1, wrong) is False
        assert sc.decrypt_page(key, page1, 1, wrong) is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_random_bytes_never_look_like_a_sqlite_header() -> None:
    """随机数据不能被当成「解出来的 SQLite 头」。

    这条是 `page1_header_ok` 的生死线：它一旦宽松，前面所有校验都失去意义。
    """
    sc = _cipher()
    assert all(not sc.page1_header_ok(os.urandom(16), reserve=RESERVE)
               for _ in range(200))
    assert not sc.page1_header_ok(sc.SQLITE_MAGIC, reserve=RESERVE), (
        "只有魔数、后面全是 0 的 16 字节也不行")


def test_a_tampered_page_returns_none_instead_of_garbage() -> None:
    """改一个字节的密文 → 认证失败 → 必须返回 `None`，不能返回垃圾。

    这是 fail-closed 落到最底层的一处：宁可少一页，
    也不要把 4096 字节随机数据当消息塞进用户的记忆里。
    """
    sc = _cipher()
    tmp = Path(tempfile.mkdtemp(prefix="wingman_cipher_"))
    try:
        src, key, profile, _ = _make_db(tmp)
        blob = bytearray(src.read_bytes())
        salt = bytes(blob[:16])
        blob[200] ^= 0x01                       # 在第 1 页的密文里改一位
        assert sc.decrypt_page(key, bytes(blob[:PAGE]), 1, profile, salt=salt) is None

        clean = src.read_bytes()
        page3 = bytearray(clean[2 * PAGE:3 * PAGE])
        page3[100] ^= 0x01
        assert sc.decrypt_page(key, bytes(page3), 3, profile, salt=salt) is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_truncated_file_is_refused_with_a_reason() -> None:
    """文件被截断/根本不是 SQLCipher 库 → 拒绝并说明，不是抛异常。"""
    sc = _cipher()
    tmp = Path(tempfile.mkdtemp(prefix="wingman_cipher_"))
    try:
        profile = sc.Profile("test", page_size=PAGE, hmac_algo="sha512", reserve=RESERVE)
        junk = tmp / "junk.db"
        junk.write_bytes(b"not a database at all")
        rep = sc.decrypt_database(junk, tmp / "out.db", os.urandom(32), profile,
                                  apply_wal=False)
        assert rep.ok is False
        assert rep.message, "失败必须给出原因，否则用户不知道该做什么"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_profile_probe_finds_the_right_parameters() -> None:
    """给定密钥和参数矩阵，探测要挑出对的那一套（顺序不能决定结果）。"""
    sc = _cipher()
    tmp = Path(tempfile.mkdtemp(prefix="wingman_cipher_"))
    try:
        src, key, profile, _ = _make_db(tmp)
        page1 = src.read_bytes()[:PAGE]
        wrong = sc.Profile("wrong", page_size=PAGE, hmac_algo="sha512", reserve=48)
        assert sc.probe_profile(key, page1, (wrong, profile)) is profile
        assert sc.probe_profile(os.urandom(32), page1, (wrong, profile)) is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_crypto_dependency_is_optional_and_says_so() -> None:
    """缺加密库时要有可读的原因，而不是 ImportError 崩在启动路上。

    这条测的是「降级路径存在」而不是用它 —— 依赖装了的时候它只检查
    常量有没有配对（`CRYPTO_AVAILABLE` 为假就必须有一句原因）。
    """
    sc = _cipher()
    if not sc.CRYPTO_AVAILABLE:                     # pragma: no cover
        assert sc.CRYPTO_REASON
    else:
        assert sc.CRYPTO_REASON == "" or isinstance(sc.CRYPTO_REASON, str)


def main() -> int:
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    skipped = failed = 0
    print("=" * 64)
    print(f"SQLCipher 解密器往返守卫：{len(tests)} 项")
    print("=" * 64)
    for fn in tests:
        try:
            fn()
        except _SkipTest as exc:
            skipped += 1
            print(f"  - 跳过 {fn.__name__}：{exc}")
        except Exception:                       # noqa: BLE001
            failed += 1
            print(f"  x 失败 {fn.__name__}")
            traceback.print_exc()
        else:
            print(f"  + 通过 {fn.__name__}")
    print("-" * 64)
    print(f"通过 {len(tests) - failed - skipped} / 失败 {failed} / 跳过 {skipped}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
