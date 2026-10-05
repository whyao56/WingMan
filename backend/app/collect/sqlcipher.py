"""SQLCipher 4 的解密，以及「这个密钥对不对」的自证校验。

为什么这个模块值得单独存在、并且写得这么啰嗦：读本地聊天库这件事，
最容易出的**不是崩溃，而是「看起来成功了」**。用错密钥去解，得到的也是
4096 字节的随机数据；如果用「解出来非空」当成功标准，程序就会把垃圾写进
用户的记忆里，而且一句话都不报。所以这里的每一个函数都以
「能不能解出 SQLite 自己的头」为判据 —— 这个判据是**自证的**：
不需要事先知道密钥，也没法蒙混过关。

磁盘格式（page 1 特殊，其余页一样）：

    page 1 : [ salt(16) | ciphertext | IV(16) | HMAC(64) ]
    page N : [ ciphertext | IV(16) | HMAC(64) ]

- `salt` 是全文件**唯一**的明文。它占了 SQLite 头的位置，所以解密时要
  把 `SQLite format 3\\0` 这 16 字节**补回**到 page 1 开头。
- 每页末尾 `reserve` 字节是保留区，只放 IV 和 HMAC，明文里这一区是 0。
- 密钥派生：
  * 加密密钥 = 直接给（`PRAGMA key = "x'..'"` 的原始密钥形式，不走 KDF）
  * 认证密钥 = `PBKDF2-HMAC-SHA512(加密密钥, salt ^ 0x3a, 2 次, 32 字节)`
    —— 只有 **2 次**迭代，所以验证一个候选密钥很便宜。
- HMAC 覆盖 `ciphertext ‖ IV ‖ 页号(uint32 小端)`。

程序从内存里捞到的是**已经派生好的**加密密钥，所以这里不做 PBKDF2
（那是应用自己做的，我们复现不了也不需要）。

参考：SQLCipher 4 格式说明（page 1 布局、reserve=80、mac_key 派生）。
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import struct
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------- 可选依赖
#
# `cryptography` 只在**自动采集**（解密库）这条路上需要，半自动采集
# （剪贴板）完全用不到它。所以这里不硬依赖：装了就装上，
# 没装也要让程序正常起来，只是自动采集给出「缺一个库，装一下」的说明。
#
# 为什么不用自带实现绕开这个依赖：AES 这种东西，自己搓一份出来
# 既不安全也不值得 —— 而这个模块要处理的正是加密数据。
try:  # pragma: no cover - 取决于环境
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    CRYPTO_AVAILABLE = True
    CRYPTO_REASON = ""
except ImportError as _exc:  # pragma: no cover - 取决于环境
    Cipher = algorithms = modes = None       # type: ignore[assignment]
    CRYPTO_AVAILABLE = False
    CRYPTO_REASON = (
        f"缺少 cryptography 模块（{_exc}）。自动采集需要它来解密客户端数据库；"
        "半自动采集（复制粘贴）不需要，可以照常使用。"
        "要启用自动采集，请执行：pip install cryptography"
    )

# ---------------------------------------------------------------- 常量

SQLITE_MAGIC = b"SQLite format 3\x00"
SALT_SIZE = 16
IV_SIZE = 16
KEY_SIZE = 32

# SQLite 数据库头里第 16 字节起的固定字段。SQLCipher 把「保留区大小」写在第 20
# 字节，所以只要成功解出第 1 页，就能自己读出真实的 reserve —— 不必靠猜。
#   [16:18] 页大小（大端）  [18] 写版本  [19] 读版本  [20] 保留区大小
#   [21] 最大载荷比 64  [22] 最小载荷比 32  [23] 叶子载荷比 32
HEADER_SIGNATURE_LEN = 8


@dataclass(frozen=True)
class Profile:
    """一套 SQLCipher 参数。

    不同客户端、不同版本用的参数不一样（哈希算法、保留区大小、
    文件前面有没有自定义头），所以参数是数据而不是常量。
    """

    name: str
    page_size: int = 4096
    hmac_algo: str = "sha512"      # sha512 | sha256 | sha1
    reserve: int = 80              # 16(iv) + hmac 长度
    file_header: int = 0           # 有些客户端在 SQLCipher 数据前加了自己的头

    @property
    def hmac_size(self) -> int:
        return _HMAC_SIZES.get(self.hmac_algo, 64)

    @property
    def usable(self) -> int:
        return self.page_size - self.reserve


_HMAC_SIZES = {"sha512": 64, "sha256": 32, "sha1": 20}
_HMAC_ALGOS = {"sha512": hashlib.sha512, "sha256": hashlib.sha256, "sha1": hashlib.sha1}


# 常见组合。顺序即尝试顺序：先试最可能的，命中就停。
QQ_NT_PROFILES: tuple[Profile, ...] = (
    Profile("qq-nt/sha512-80", hmac_algo="sha512", reserve=80, file_header=1024),
    Profile("qq-nt/sha1-36", hmac_algo="sha1", reserve=36, file_header=1024),
    Profile("qq-nt/sha1-48", hmac_algo="sha1", reserve=48, file_header=1024),
    Profile("qq-nt/sha256-48", hmac_algo="sha256", reserve=48, file_header=1024),
)

WECHAT4_PROFILES: tuple[Profile, ...] = (
    Profile("wechat4/sha512-80", hmac_algo="sha512", reserve=80, file_header=0),
    Profile("wechat4/sha1-48", hmac_algo="sha1", reserve=48, file_header=0),
)

GENERIC_PROFILES: tuple[Profile, ...] = (
    Profile("sha512-80"),
    Profile("sha1-48", hmac_algo="sha1", reserve=48),
    Profile("sha1-36", hmac_algo="sha1", reserve=36),
    Profile("sha256-48", hmac_algo="sha256", reserve=48),
)


# ---------------------------------------------------------------- 校验


def page1_header_ok(plain: bytes, reserve: int | None = None) -> bool:
    """判断「第 1 页解密后的开头」是不是一段真的 SQLite 头。

    注意这里校验的是**偏移 16 之后**的内容，不是文件开头：
    `SQLite format 3\\0` 那 16 字节是被解密器补回去的，不在密文里。
    一开始按「密文解出来以魔数开头」去判，结果是所有候选全灭 ——
    判据错了比算得慢更致命，因为慢只是慢，错是永远找不到还在装没事。
    """
    if len(plain) < HEADER_SIGNATURE_LEN:
        return False
    page_size = struct.unpack(">H", plain[0:2])[0]
    if page_size not in (1024, 2048, 4096, 8192, 16384, 32768, 65536):
        return False
    if plain[2] != 1 or plain[3] != 1:
        return False            # 文件格式的读写版本，SQLite 目前只写 1
    if plain[5] != 64 or plain[6] != 32 or plain[7] != 32:
        return False            # 三个载荷比例，SQLite 的固定值
    if reserve is not None and plain[4] != reserve:
        # 第 20 字节就是保留区大小。对不上说明 reserve 猜错了 ——
        # 这一条能顺便确认参数版本，比只看魔数强得多。
        return False
    return True


def _decrypt_block0(raw_key: bytes, page1: bytes, profile: Profile) -> bytes:
    """只解第 1 个 AES 块。

    CBC 的第一个明文块 = AES_ECB 解密(第一个密文块) XOR IV，
    所以判定一个候选密钥只需要解 16 字节，不用解整页。
    这一步把单候选成本从 4 µs 压到 1 µs 以下 ——
    在「要在几百 MB 内存里试上千万个候选」的场景里，这就是能不能跑完的区别。
    """
    if len(page1) < profile.page_size:
        return b""
    if not CRYPTO_AVAILABLE:
        return b""
    iv_off = profile.page_size - profile.reserve
    iv = page1[iv_off:iv_off + IV_SIZE]
    body = page1[SALT_SIZE:SALT_SIZE + 16]
    if len(iv) < IV_SIZE or len(body) < 16:
        return b""
    dec = Cipher(algorithms.AES(raw_key), modes.ECB()).decryptor()
    raw = dec.update(body) + dec.finalize()
    return bytes(a ^ b for a, b in zip(raw, iv))


def check_raw_key(raw_key: bytes, page1: bytes, profile: Profile) -> bool:
    """候选密钥对不对？**只看第 1 页的头**，不做整库解密。"""
    if len(raw_key) != KEY_SIZE or len(page1) < profile.page_size:
        return False
    head = _decrypt_block0(raw_key, page1, profile)
    if not page1_header_ok(head, reserve=profile.reserve):
        return False
    # 头对上了还不够：头是 8 字节常量，理论上可能被撞上；
    # HMAC 用的是派生出来的另一个密钥，撞上的概率可以忽略。
    return verify_page_hmac(raw_key, page1, 1, profile, salt=page1[:SALT_SIZE])


def mac_key_of(raw_key: bytes, salt: bytes) -> bytes:
    """认证密钥 = PBKDF2-HMAC-SHA512(加密密钥, salt ^ 0x3a, 2 次)。

    只有 2 次迭代 —— 这不是我抠出来的优化，是 SQLCipher 的规定。
    """
    mac_salt = bytes(b ^ 0x3A for b in salt)
    return hashlib.pbkdf2_hmac("sha512", raw_key, mac_salt, 2, 32)


def verify_page_hmac(raw_key: bytes, page: bytes, pgno: int, profile: Profile,
                     salt: bytes = b"") -> bool:
    """校验一页的认证标签。覆盖范围：密文 ‖ IV ‖ 页号(小端)。

    `salt` 必须由调用方给：它只明文存在于第 1 页，其它页自己读不出来。
    （写成「从当前页里取 salt」是错的 —— 第 1 页的 salt 在页首 `[0:16]`，
    不是 `[16:32]`，取错偏移会让所有多页校验静默失败。）
    """
    page_size, reserve = profile.page_size, profile.reserve
    if len(page) < page_size:
        return False
    if pgno == 1:
        salt = page[:SALT_SIZE]
    elif not salt:
        return False
    reserve_start = page_size - reserve
    iv = page[reserve_start:reserve_start + IV_SIZE]
    tag_at = reserve_start + IV_SIZE
    tag = page[tag_at:tag_at + profile.hmac_size]
    if len(tag) != profile.hmac_size or len(iv) != IV_SIZE:
        return False
    body = page[SALT_SIZE:reserve_start] if pgno == 1 else page[0:reserve_start]
    digest = _hmac.new(
        mac_key_of(raw_key, salt),
        body + iv + struct.pack("<I", pgno),
        _HMAC_ALGOS[profile.hmac_algo],
    ).digest()
    return _hmac.compare_digest(digest, tag)


def probe_profile(raw_key: bytes, page1: bytes,
                  profiles=GENERIC_PROFILES) -> Profile | None:
    """在参数矩阵里找出这个密钥配哪一套参数。"""
    for prof in profiles:
        if check_raw_key(raw_key, page1, prof):
            return prof
    return None


# ---------------------------------------------------------------- 整库解密


@dataclass
class DecryptReport:
    pages: int = 0
    bytes_written: int = 0
    bad_pages: int = 0
    wal_frames_applied: int = 0
    profile: str = ""
    ok: bool = False
    message: str = ""

    def describe(self) -> str:
        if not self.ok:
            return f"解密失败：{self.message}"
        extra = f"，另外套用了 WAL 里 {self.wal_frames_applied} 帧" if self.wal_frames_applied else ""
        warn = f"（{self.bad_pages} 页认证不通过，已跳过）" if self.bad_pages else ""
        return (f"解出 {self.pages} 页 / {self.bytes_written / 1e6:.2f} MB，"
                f"参数 {self.profile}{extra}{warn}")


def decrypt_page(raw_key: bytes, page: bytes, pgno: int,
                 profile: Profile, salt: bytes = b"") -> bytes | None:
    """解密一页，返回**标准 SQLite 页**（页 1 已补回魔数，保留区补 0）。

    `salt` 必须传：它只明文存在于第 1 页，别的页自己读不出来，而认证标签
    的密钥是用 salt 派生的。早先这里没接 salt、非首页一律拿到空串，
    结果是「首页解得开、从第 2 页起全部认证失败，整库只解出 1 页」。

    认证失败返回 None，绝不返回「看起来像样」的垃圾 ——
    这是 fail-closed：宁可少一页，也不要往用户记忆里塞乱码。
    """
    page_size, reserve = profile.page_size, profile.reserve
    if len(page) < page_size:
        return None
    if not CRYPTO_AVAILABLE:
        return None
    if pgno == 1:
        salt = page[:SALT_SIZE]
    elif not salt:
        return None
    if not verify_page_hmac(raw_key, page, pgno, profile, salt=salt):
        return None
    reserve_start = page_size - reserve
    iv = page[reserve_start:reserve_start + IV_SIZE]
    body = page[SALT_SIZE:reserve_start] if pgno == 1 else page[0:reserve_start]
    if not body or len(body) % 16:
        return None
    dec = Cipher(algorithms.AES(raw_key), modes.CBC(iv)).decryptor()
    plain_body = dec.update(body) + dec.finalize()
    out = bytearray()
    if pgno == 1:
        out += SQLITE_MAGIC
    out += plain_body
    out += b"\x00" * reserve          # 保留区在明文里是 0
    if len(out) != page_size:
        # 页大小与 reserve 的组合不自洽，宁可报错也不写出错位的库
        return None
    return bytes(out)


def decrypt_database(
    src: Path,
    dst: Path,
    raw_key: bytes,
    profile: Profile,
    *,
    apply_wal: bool = True,
    progress=None,
) -> DecryptReport:
    """把一个 SQLCipher 库解密成标准 SQLite 文件。

    `apply_wal=True` 时会顺带把 `-wal` 里的帧盖到结果上：
    客户端在跑的时候，**最新的消息还在 WAL 里**，主库文件只有 4 KB 也是常事
    （实测 QQ 的 nt_msg.db-wal 就有 1.47 MB）。只解主库会漏掉最近的对话，
    而用户第一眼要看的恰恰是最近的对话。
    """
    src, dst = Path(src), Path(dst)
    report = DecryptReport(profile=profile.name)
    raw = src.read_bytes()
    if len(raw) <= profile.file_header:
        report.message = "文件比自定义头还短"
        return report
    body = raw[profile.file_header:]
    if len(body) < profile.page_size:
        report.message = "去头后不足一页，可能不是 SQLCipher 库"
        return report
    page1 = body[:profile.page_size]
    if not check_raw_key(raw_key, page1, profile):
        report.message = "密钥或参数不对（第 1 页认证不通过）"
        return report

    page_count = len(body) // profile.page_size
    salt = page1[:SALT_SIZE]
    pages: dict[int, bytes] = {}
    for i in range(page_count):
        pgno = i + 1
        page = body[i * profile.page_size:(i + 1) * profile.page_size]
        plain = decrypt_page(raw_key, page, pgno, profile, salt=salt)
        if plain is None:
            report.bad_pages += 1
            continue
        pages[pgno] = plain
        if progress and pgno % 200 == 0:
            progress(pgno, page_count)

    if apply_wal:
        applied, strays = _apply_wal(src, raw_key, profile, pages, salt)
        report.wal_frames_applied = applied
        report.bad_pages += strays

    if not pages:
        report.message = "一页都没解出来"
        return report

    highest = max(pages)
    with dst.open("wb") as fh:
        for pgno in range(1, highest + 1):
            fh.write(pages.get(pgno, b"\x00" * profile.page_size))
    report.pages = len(pages)
    report.bytes_written = highest * profile.page_size
    report.ok = True
    return report


def _apply_wal(src: Path, raw_key: bytes, profile: Profile,
               pages: dict[int, bytes], salt: bytes) -> tuple[int, int]:
    """把 `-wal` 的帧盖到已解密的页上。

    WAL 的帧结构：32 字节文件头，之后每帧是 `[24 字节帧头][一整页]`，
    帧头里前 4 字节是本帧写的是第几页。SQLCipher 加密的是页本身，
    所以逐帧按同一个 pgno 解密即可。
    """
    wal = Path(str(src) + "-wal")
    if not wal.is_file():
        return 0, 0
    blob = wal.read_bytes()
    if len(blob) < 32:
        return 0, 0
    page_size = profile.page_size
    frame_size = 24 + page_size
    applied = strays = 0
    off = 32
    while off + frame_size <= len(blob):
        frame = blob[off:off + frame_size]
        pgno = struct.unpack(">I", frame[0:4])[0]
        if pgno == 0:
            break
        plain = decrypt_page(raw_key, frame[24:], pgno, profile, salt=salt)
        if plain is None:
            strays += 1
        else:
            pages[pgno] = plain
            applied += 1
        off += frame_size
    return applied, strays


def salt_of(path: Path, profile: Profile) -> bytes:
    """读出某个库的 salt（它是明文，不需要密钥）。"""
    with Path(path).open("rb") as fh:
        fh.seek(profile.file_header)
        return fh.read(SALT_SIZE)
