"""导入这条路的守卫：适配器必须真的能把用户手上的文件读出来。

这个文件是被一次**真实的静默损坏**逼出来的：微信适配器从项目第一个提交起
就引用了一个没导入的名字（`iter_blocks`），于是「导入微信聊天记录」必然
抛 `NameError` —— 编译不报、导入不报，只有真跑到那一行才炸。而恰恰没有
任何测试跑过它，所以它坏了很多个版本都没人发现，连仓库自带的微信示例
文件都读不出来。

所以这里守的是三件事：

1. **仓库自带的示例文件必须能导入**（两份，QQ 与微信各一份）。
   示例文件是产品对外说的「我们支持这个格式」，它读不出来就是产品在说谎。
2. **界面上写着的粘贴格式必须真的能用**。粘贴框旁边写着
   「`2024-01-01 12:00:00 昵称` + 内容」，而当时的默认适配器只认 JSON / CSV ——
   用户照着自己的界面说明操作，得到的是一句「没解析出消息」。
3. **「用到了没定义的名字」这一类错要能被静态拦下**。见文件末尾那条用例。
"""

from __future__ import annotations

import builtins
import json
import pathlib
import shutil
import symtable
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

SAMPLES = PROJECT / "samples"

# 粘贴框旁边写的示范格式。改界面文案时这里要跟着改 —— 两边不一致正是当初的病根。
PASTE_DOC_FORMAT = (
    "2024-01-01 21:00:00 小鹿\n"
    "在吗\n"
    "\n"
    "2024-01-01 21:02:00 我\n"
    "在的\n"
)


def _tmp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="wingman_adapters_"))


def _fresh_store(tmp: Path):
    from app.store import Store

    store = Store(tmp / "wingman.db")
    store.init()
    return store


# ================================================================ 自带示例文件


def test_bundled_samples_import_through_their_own_adapter() -> None:
    """两份示例文件各自被正确的适配器认出来，并且真的导进去了。

    用「插入条数」而不是「函数没抛异常」当断言：`NameError` 这种错确实会抛，
    但将来更可能出的是「识别对了、解析出来 0 条」—— 那同样等于导入失败，
    却一声不响。条数断言两种都能抓住。
    """
    from app.adapters import registry

    cases = [
        ("qq_sample_小鹿.txt", "qq", 40),
        ("wechat_sample_阿哲.txt", "wechat", 20),
    ]
    for filename, expect_adapter, least in cases:
        path = SAMPLES / filename
        assert path.exists(), f"示例文件缺失：{path}"
        tmp = _tmp_dir()
        try:
            store = _fresh_store(tmp)
            probe = registry.detect(path)[0]
            assert probe.name == expect_adapter, (
                f"{filename} 应识别为 {expect_adapter}，实际为 {probe.name}"
            )
            result = registry.import_file(store, path)
            assert result.inserted >= least, (
                f"{filename} 只导入了 {result.inserted} 条（至少应有 {least} 条）；"
                f"警告：{result.warnings}"
            )
            assert "我" in result.speakers, (
                f"{filename} 没认出「我」这个说话人，实际说话人：{result.speakers}"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def test_the_paste_format_documented_in_the_ui_actually_parses() -> None:
    """界面上写的那个粘贴格式，默认适配器必须真的能读。

    不指定适配器（也就是界面的默认行为）粘贴示范格式 —— 这一条如果红了，
    说明用户照着界面上的说明做会得到「一条都没读到」，而原因只是
    默认适配器选错了。**文案与默认值必须一起成立。**
    """
    from app.adapters import registry

    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        result = registry.import_text(store, PASTE_DOC_FORMAT, chat_name="小鹿")
        assert result.inserted >= 2, (
            f"照着界面上的格式粘贴却只导入 {result.inserted} 条；警告：{result.warnings}"
        )
        chat = store.get_chat(result.chat_id)
        assert chat is not None and chat.peer_name == "小鹿", (
            f"对方昵称没认出来：{chat.peer_name if chat else '(没有这条记录)'}"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pasted_json_is_recognised_without_being_declared() -> None:
    """粘贴 JSON 也要认。粘贴框是 `.txt`，而 `generic` 靠扩展名分支。

    一律按 `.txt` 落盘的话，用户粘一段 JSON 进来同样读不出来 ——
    所以后缀得从内容推（见 `registry.import_text`）。
    """
    from app.adapters import registry

    payload = json.dumps([
        {"time": "2024-01-01 21:00:00", "sender": "小鹿", "text": "在吗"},
        {"time": "2024-01-01 21:02:00", "sender": "我", "text": "在的"},
    ], ensure_ascii=False)
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        result = registry.import_text(store, payload, chat_name="小鹿")
        assert result.inserted == 2, (
            f"粘贴的 JSON 只导入 {result.inserted} 条；警告：{result.warnings}"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_column_names_are_matched_against_the_real_columns() -> None:
    """列名要拿**数据里真实的列**去挑，不能拿候选表自己糊弄自己。

    原来的 `_resolve_map` 把「候选列名表」当成「现有列名」传进去了，
    于是永远只会返回候选表的第一个名字（`ts` / `sender` / `text`），
    数据里叫什么完全不影响结果。后果：

    - JSON 的时间字段叫 `time`（候选表里排第二）→ 选不中 → 一条都读不出来；
    - 中文表头 `时间 / 内容 / 发送者` → 一个都不中 → 一条都读不出来。

    而且最坏的是**嗅探与解析说两套话**：`sniff` 看到中文表头给 0.85 的高分，
    用户看到「匹配度很高」，然后导入结果是 0 条。下面把能过的形状都钉住。
    """
    from app.adapters import registry

    # 正文刻意比说话人长得多：万一将来退回「按长度猜内容列」，也能猜对方向
    first, second = "在吗，出来聊聊", "在的，等我一下"
    shapes = {
        "模块文档里写的 who/content": json.dumps([
            {"time": "2024-01-01 21:00:00", "who": "小鹿", "content": first},
            {"time": "2024-01-01 21:02:00", "who": "我", "content": second},
        ], ensure_ascii=False),
        "候选表首项 ts/sender/text": json.dumps([
            {"ts": "2024-01-01 21:00:00", "sender": "小鹿", "text": first},
            {"ts": "2024-01-01 21:02:00", "sender": "我", "text": second},
        ], ensure_ascii=False),
        "中文表头 CSV":
            f"时间,发送者,内容\n2024-01-01 21:00:00,小鹿,{first}\n2024-01-01 21:02:00,我,{second}\n",
        "英文表头 CSV":
            f"datetime,name,message\n2024-01-01 21:00:00,小鹿,{first}\n2024-01-01 21:02:00,我,{second}\n",
        "列名全不认识（按值形状推断）":
            f"A,B,C\n2024-01-01 21:00:00,小鹿,{first}\n2024-01-01 21:02:00,我,{second}\n",
    }
    for label, payload in shapes.items():
        tmp = _tmp_dir()
        try:
            store = _fresh_store(tmp)
            result = registry.import_text(store, payload, chat_name="小鹿")
            assert result.parsed == 2, (
                f"「{label}」这一形状只解析出 {result.parsed} 条（应 2 条）；"
                f"警告：{result.warnings}"
            )
            texts = [m["text"] for m in store.all_messages(result.chat_id)]
            assert texts == [first, second], (
                f"「{label}」的消息正文不对：{texts} —— "
                "多半是把发送者那一列当成内容了"
            )
            senders = [m["sender"] for m in store.all_messages(result.chat_id)]
            assert senders == ["小鹿", "我"], f"「{label}」的说话人不对：{senders}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def test_nothing_matches_falls_back_to_the_generic_parser() -> None:
    """所有适配器都打 0 分时，落到通用的那个，而不是听凭注册顺序。

    原来 `_pick` 直接取 `probes[0]`，而分数并列时排序是稳定的 ——
    于是「谁都不匹配」会静默落到**注册顺序第一个**（QQ）上。
    后果不只是猜错解析器，而是**连提示词一起说错**：用户会看到
    「「QQ 聊天记录」读不了这份内容」，可问题根本不在于它是 QQ。

    兜底选 `generic` 的理由：它本来就是「结构化解析的兜底」，
    而且它的失败提示是最能指路的那句（告诉用户改走剪贴板）。
    """
    from app.adapters import registry

    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        # 一段没有任何时间戳、也不是任何已知结构的纯口语
        result = registry.import_text(
            store, "今天天气不错啊我们出去走走吧顺便吃个饭", chat_name="随便",
        )
        assert result.inserted == 0
        assert result.adapter == "generic", (
            f"谁都不匹配时应落到 generic，实际落到 {result.adapter}"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_an_empty_parse_says_what_to_do_instead_of_just_failing() -> None:
    """一条都没解析出来时，提示必须能指路。

    这是新手最容易卡住的地方：他把内容贴进来了，界面说一条都没读到。
    只说「格式不匹配」等于让他自己猜；得点名这个适配器吃什么，
    并给出另一条确定能走通的路（半自动剪贴板）。
    """
    from app.adapters import registry

    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        # 一段没有任何时间戳的纯口语，谁都不该声称能读
        result = registry.import_text(
            store, "今天天气不错啊我们出去走走吧顺便吃个饭", chat_name="随便",
        )
        assert result.inserted == 0
        assert result.warnings, "解析为空却一条提示都没有"
        joined = " ".join(result.warnings)
        assert "剪贴板" in joined or "半自动" in joined, (
            f"没有告诉用户另一条能走通的路：{result.warnings}"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ 渠道归属


def test_import_result_reports_which_channel_it_landed_in() -> None:
    """导入结果要回报归宿渠道。

    平台有两种来源：用户显式声明的、适配器猜的。**猜的那次用户看不见** ——
    如果不回报，把 Telegram 的内容贴上「微信」标签这件事，只会在之后某次
    分析答得不对时才隐约浮出来，很难回溯到导入这一步。
    """
    from app.adapters import registry

    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        result = registry.import_text(store, PASTE_DOC_FORMAT, chat_name="小鹿")
        assert result.platform, "没回报平台"
        assert result.channel, "没回报渠道"
        chat = store.get_chat(result.chat_id)
        assert chat is not None and chat.channel == result.channel, (
            "回报的渠道和库里存的不一致，界面就会显示一个不存在的渠道"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_declaring_the_source_beats_the_adapter_guess() -> None:
    """用户说了「这段是其他聊天」，就不许再按文本排布猜成微信。

    适配器认的是**文件长什么样**，用户知道的是**话是在哪说的**，两者不等价：
    别的聊天工具导出的文本很可能被识别成微信格式。机器猜不出来的事就别猜。
    """
    from app.adapters import registry

    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)

        # 先确认不声明时确实会被猜成微信（否则这条用例等于没验到东西）
        guessed = registry.import_text(store, PASTE_DOC_FORMAT, chat_name="小鹿")
        assert guessed.channel == "wechat", (
            f"这份文本本应被猜成微信，实际 {guessed.channel} —— 用例前提失效了"
        )

        declared = registry.import_text(
            store, PASTE_DOC_FORMAT, chat_name="小鹿 · Telegram", platform="other",
        )
        assert declared.channel == "other", (
            f"用户声明了 other，实际却归到 {declared.channel}"
        )
        chat = store.get_chat(declared.chat_id)
        assert chat is not None and chat.platform == "other"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_an_unknown_declared_platform_keeps_the_words_but_lands_in_other() -> None:
    """声明的平台后端不认得时：**渠道**归到「其他聊天」，**平台**原样留着。

    这是一个刻意的不对称，两边的理由不同：

    - 渠道名是**给人看的归类**（界面上的彩色标记、对象页的分组），只允许
      有限几个值，认不出来又硬造一个新名字，会让同一个来源在库里长出好几种写法；
      所以兜底到「其他聊天」—— 它本来就是收容所。
    - 平台名是**用户自己打的字**。项目在别处（`openChatSettingsDialog`）已经
      立过同一条规矩：认不出来就原样显示「（保持原样）」，不许悄悄改掉。
      打开设置看一眼就把人家的输入换掉，是那种事后很难查的静默改动。
    """
    from app.adapters import registry

    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        result = registry.import_text(
            store, PASTE_DOC_FORMAT, chat_name="小鹿", platform="telegram",
        )
        assert result.channel == "other", (
            f"不认得的平台应把渠道兜底到 other，实际 {result.channel}"
        )
        assert result.platform == "telegram", (
            f"用户打的平台名被改掉了：{result.platform}"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ 静态守卫


def _undefined_globals(py: Path) -> set[str]:
    """列出这个模块里「被引用、但既没定义也没导入」的全局名。

    用 `symtable` 从字节码层面看符号表：能拿到 Python 自己认定的
    「这个名字属于全局作用域，但本模块没有给它赋值/导入」。这正是
    `wechat.py` 那个 `iter_blocks` 的形状。
    """
    src = py.read_text(encoding="utf-8")
    if "import *" in src:  # 星号导入会让符号表失真，跳过
        return set()
    top = symtable.symtable(src, str(py), "exec")
    known = {s.get_name() for s in top.get_symbols()
             if s.is_assigned() or s.is_imported() or s.is_namespace()}
    builtin_names = set(dir(builtins)) | {
        "__file__", "__name__", "__doc__", "__package__", "__spec__",
        "__loader__", "__builtins__", "__debug__", "__annotations__",
    }
    missing: set[str] = set()

    def visit(table) -> None:
        for sym in table.get_symbols():
            name = sym.get_name()
            if (sym.is_global() and sym.is_referenced() and not sym.is_assigned()
                    and name not in known and name not in builtin_names):
                missing.add(name)
        for child in table.get_children():
            visit(child)

    for sym in top.get_symbols():
        name = sym.get_name()
        if (sym.is_referenced() and not (sym.is_assigned() or sym.is_imported())
                and name not in builtin_names):
            missing.add(name)
    visit(top)
    return missing


def test_no_module_references_a_global_that_was_never_defined() -> None:
    """整个 app/ 里不许出现「用了没定义的名字」。

    这一类错最坏的地方是**它不报错也不崩溃，只是那个分支永远走不通**：
    `import wechat` 成功、`/api/health` 正常、界面能打开，只有用户在
    「导入微信记录」时收到一个内部错误。微信适配器就这样坏了很久。

    与其等用户撞上来，不如让编译器级别的事实替我们盯住：
    Python 自己已经把「这个名字在本模块里没定义」算出来了，我们只要问它。
    """
    offenders: list[str] = []
    for py in sorted((BACKEND / "app").rglob("*.py")):
        for name in sorted(_undefined_globals(py)):
            offenders.append(f"{py.relative_to(BACKEND)}: {name}")
    assert not offenders, (
        "这些模块引用了没定义也没导入的全局名（只会真跑到那一行时才炸）：\n  "
        + "\n  ".join(offenders)
    )
