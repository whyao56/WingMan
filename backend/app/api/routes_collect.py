"""采集接口：探测 / 版本指引 / 取密钥 / 自动采集 / 半自动采集。

## 这个文件要回答的三个问题

用户点进「采集」页面时，心里其实只在问三件事：

1. **我的版本支不支持？** —— `GET /api/collect/clients` 直接给出
   「你的微信 4.1.13.12 支持，但本机还没有消息库，先点开一个会话」
   这样一句能照做的事，而不是一张支持版本对照表。版本矩阵是**软件里的数据**，
   不是 README 里的一段话 —— 写在 README 里的人不会来看，看了也不知道自己是什么版本。
2. **采到的到底是什么？** —— `POST /api/collect/preview` 走完整条链路但不写库。
   采集是不可逆地把东西塞进记忆，所以必须有一个「先看看」的入口。
3. **采不动怎么办？** —— 半自动采集（剪贴板）是**当下唯一确定能跑通的通道**，
   所以它不是一个隐藏的降级选项，而是与自动采集并列的一等公民。

## 为什么所有耗时动作都走 `asyncio.to_thread`

探测要枚举进程、扫数据目录、读文件版本；采集要解密几十 MB 的库。
这些全是阻塞调用，直接在事件循环里跑会把整个界面卡住 ——
表现是「点了采集，界面死了十秒」，用户会以为程序崩了。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from ..context import get_ctx
from ..collect import detect as detect_mod
from ..collect import keys as keys_mod
from ..collect import pipeline as pipe
from ..collect import reader as reader_mod
from ..collect.keys import normalise_key
from ..collect.matrix import SUPPORT_MATRIX, supported_versions_table
from ..collect.semi import get_semi
from ..collect.sqlcipher import (
    GENERIC_PROFILES, QQ_NT_PROFILES, WECHAT4_PROFILES,
)

log = logging.getLogger("wingman.api.collect")
router = APIRouter(prefix="/api/collect", tags=["collect"])


def _profiles_for(key: str):
    if key == "qq":
        return QQ_NT_PROFILES
    if key == "wechat":
        return WECHAT4_PROFILES
    return GENERIC_PROFILES


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise HTTPException(status_code=422,
                            detail=f"时间格式不对：{value}（要 ISO 格式）") from exc


# ================================================================ 探测与指引


@router.get("/clients")
async def list_clients(deep: bool = True) -> dict[str, Any]:
    """本机装了什么、什么版本、支不支持、下一步做什么。"""
    dets = await asyncio.to_thread(detect_mod.detect_all, deep=deep)
    return {
        "clients": [detect_mod.describe(d) for d in dets],
        "summary": detect_mod.summarise(dets),
        "matrix": supported_versions_table(),
    }


@router.get("/clients/{key}")
async def get_client(key: str, deep: bool = True) -> dict[str, Any]:
    det = await asyncio.to_thread(detect_mod.detect_client, key, deep=deep)
    if det is None:
        raise HTTPException(status_code=404, detail=f"不认识这个客户端：{key}")
    return detect_mod.describe(det)


@router.get("/matrix")
async def get_matrix() -> list[dict[str, Any]]:
    """支持矩阵。前端「配置页」用它渲染「你的版本 → 该做什么」。

    返回类型必须写成 `list[...]`：FastAPI 会**按注解校验响应体**，
    注解写成 `dict` 而实际返回列表时，接口是 500 而不是「大概能用」。
    """
    return supported_versions_table()


# ================================================================ 密钥


@router.post("/key/check")
async def check_key(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """校验一个粘贴进来的密钥，不落任何东西。"""
    client = str(payload.get("client") or "")
    db = str(payload.get("db") or "")
    text = str(payload.get("key") or "")
    if client not in SUPPORT_MATRIX:
        raise HTTPException(status_code=404, detail=f"不认识这个客户端：{client}")
    raw = normalise_key(text)
    if raw is None:
        return {"ok": False,
                "detail": "这段内容不是 32 字节密钥。它应该是 64 个十六进制字符，"
                          "形如 `x'1a2b3c…'` 或直接 64 位十六进制。"}

    from pathlib import Path
    target = Path(db) if db else None
    if target is None or not target.is_file():
        # 没给库就用这个客户端的第一条消息库来验
        det = await asyncio.to_thread(detect_mod.detect_client, client)
        cands = [Path(p) for a in (det.accounts if det else []) for p in a.message_dbs]
        target = cands[0] if cands else None
    if target is None:
        return {"ok": True, "verified": False,
                "detail": "密钥格式没问题，但本机没有可用来验证的库。"}

    prof = await asyncio.to_thread(
        keys_mod.verify_key, raw, target, _profiles_for(client))
    if prof is None:
        return {"ok": False, "verified": True, "db": str(target),
                "detail": "密钥能解析，但通不过校验（解不出 SQLite 头）。"
                          "可能对应的是另一个库，或者这个版本的参数不同。"}
    return {"ok": True, "verified": True, "db": str(target), "profile": prof.name,
            "detail": f"校验通过（参数 {prof.name}），可以开始采集。"}


@router.post("/key/scan")
async def scan_key(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """在客户端进程内存里找密钥。有预算、有结论，不假装一定能成。"""
    from pathlib import Path

    client = str(payload.get("client") or "")
    spec = SUPPORT_MATRIX.get(client)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"不认识这个客户端：{client}")
    det = await asyncio.to_thread(detect_mod.detect_client, client)
    cands = [Path(p) for a in (det.accounts if det else []) for p in a.message_dbs]
    if not cands:
        raise HTTPException(status_code=409,
                            detail="本机没有找到可采的消息库，先按指引打开几个会话。")
    budget = float(payload.get("budget_s") or 60)
    log.info("自动找密钥：client=%s，预算 %.0fs，开始", client, budget)
    attempt = await asyncio.to_thread(
        keys_mod.scan_memory_for_key, cands[0], _profiles_for(client),
        exe_names=spec.exe_names, budget_s=max(5.0, min(budget, 600.0)))
    log.info("自动找密钥：%s，用时 %.1fs%s", "命中" if attempt.ok else "没找到",
             attempt.elapsed_s, "（到点收工，没扫完）" if attempt.budget_hit else "")
    return {"ok": attempt.ok, "key": attempt.key_hex, "method": attempt.method,
            "detail": attempt.detail, "tried": attempt.tried,
            "candidates": attempt.candidates, "elapsed_s": round(attempt.elapsed_s, 1),
            "budget_hit": attempt.budget_hit}


# ================================================================ 自动采集


@router.post("/preview")
async def preview_collect(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """走完整条链路但不写库。回答「这套配置能不能采到东西」。

    走 `pipe.preview` 而不是直接 `pipe.run`：预演会**压缩搜密钥的预算**
    （见 `pipeline.PREVIEW_MEMORY_BUDGET_S`），并把这个前提写进报告的
    `notes` 里 —— 不然用户会以为「预演取不到密钥」等于这条路走不通。
    """
    req = _request_from(payload, force_dry=True)
    rep = await asyncio.to_thread(pipe.preview, None, req)
    return rep.as_dict()


@router.post("/run")
async def run_collect(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    ctx = get_ctx()
    req = _request_from(payload)
    rep = await asyncio.to_thread(pipe.run, None if req.dry_run else ctx.store, req)
    return rep.as_dict()


@router.get("/cursors")
async def list_cursors(client: str = "", person_id: str = "") -> dict[str, Any]:
    """采集游标：上次采到哪、有没有出错。"""
    rows = await asyncio.to_thread(
        lambda: get_ctx().store.list_cursors(client, person_id))
    return {"cursors": [c.model_dump() for c in rows]}


@router.delete("/cursors")
async def reset_cursor(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """重置游标 = 下次重新全量采。怀疑漏数据时用。

    `account` 可以不传，表示「这个平台上这个人的游标全重置」——
    界面上用户只看得到「小鹿 · QQ」，看不到游标是按三段主键存的。
    返回真删掉的条数：删了 0 条也要如实说，不能让界面显示
    「已重置」而实际上什么都没发生。
    """
    platform = str(payload.get("platform") or "").strip()
    peer_key = str(payload.get("peer_key") or "").strip()
    if not platform or not peer_key:
        raise HTTPException(status_code=422, detail="platform 和 peer_key 都不能为空。")
    removed = await asyncio.to_thread(
        get_ctx().store.reset_cursor, platform,
        str(payload.get("account") or ""), peer_key)
    return {"ok": True, "removed": removed,
            "detail": (f"已重置 {removed} 条采集进度，下次会重新全量采。"
                       if removed else
                       "没有找到对应的采集进度 —— 可能本来就没采过，"
                       "或者你要重置的是别的账号。")}


@router.post("/inspect")
async def inspect_db(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """对**已经解密好的副本**出一份「我看到了什么」的报告。

    用途是排查「解密成功但采不到消息」：把表名、认出来的列、候选列
    一起摆出来，用户或开发者一眼能看出是哪里没对上。
    """
    from pathlib import Path

    db = Path(str(payload.get("db") or ""))
    if not db.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在：{db}")
    me_ids = [str(x) for x in (payload.get("me_ids") or [])]
    rep = await asyncio.to_thread(reader_mod.probe, db, me_ids=me_ids,
                                  allow_carve=bool(payload.get("allow_carve")))
    if not rep.get("ok"):
        raise HTTPException(status_code=422, detail=rep.get("message") or "读不了这个库")
    return rep


def _request_from(payload: dict[str, Any], force_dry: bool = False) -> pipe.CollectRequest:
    client = str(payload.get("client") or "").strip()
    if client not in SUPPORT_MATRIX:
        raise HTTPException(status_code=404, detail=f"不认识这个客户端：{client}")
    return pipe.CollectRequest(
        client=client,
        account=str(payload.get("account") or ""),
        keys={str(k): str(v) for k, v in (payload.get("keys") or {}).items()},
        pasted_key=str(payload.get("key") or ""),
        schema_overrides={
            str(k): {str(a): str(b) for a, b in (v or {}).items()}
            for k, v in (payload.get("schema_overrides") or {}).items()
        },
        me_ids=[str(x) for x in (payload.get("me_ids") or [])],
        since=_parse_dt(payload.get("since")),
        until=_parse_dt(payload.get("until")),
        max_messages=int(payload.get("max_messages") or pipe.DEFAULT_MAX_MESSAGES),
        dry_run=bool(payload.get("dry_run")) or force_dry,
        allow_carve=bool(payload.get("allow_carve")),
        assume_peer=bool(payload.get("assume_peer")),
        allow_memory_scan=bool(payload.get("allow_memory_scan", True)),
        memory_budget_s=float(payload.get("memory_budget_s")
                              or pipe.DEFAULT_MEMORY_BUDGET_S),
    )


# ================================================================ 半自动采集


@router.post("/semi/start")
async def semi_start(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """开启剪贴板监听。

    这是**当下唯一确定能跑通**的采集通道：它不需要数据库密钥，
    因为内容是你自己选中并复制的 —— 你已经在客户端里看见它了。
    """
    ctx = get_ctx()
    client = str(payload.get("client") or "").strip()
    if client not in SUPPORT_MATRIX:
        raise HTTPException(status_code=404, detail=f"不认识这个客户端：{client}")
    peer_name = str(payload.get("peer_name") or "").strip()
    person_id = str(payload.get("person_id") or "").strip()
    if person_id and ctx.store.get_person(person_id) is None:
        raise HTTPException(status_code=404, detail=f"这个人不存在：{person_id}")
    if not person_id and not peer_name:
        raise HTTPException(
            status_code=422,
            detail="至少要指定「采谁」：可以选一个已存在的人，或直接填对方的称呼。"
                   "不指定的话，抓到的消息没有归属，只能挂在那里。")
    state = await asyncio.to_thread(
        get_semi().start, ctx.store, client=client, peer_name=peer_name,
        person_id=person_id,
        missing_time=str(payload.get("missing_time") or "inferred"))
    return {"ok": True, "state": state.__dict__}


@router.post("/semi/stop")
async def semi_stop() -> dict[str, Any]:
    state = await asyncio.to_thread(get_semi().stop)
    return {"ok": True, "state": state.__dict__}


@router.get("/semi/status")
async def semi_status() -> dict[str, Any]:
    return get_semi().snapshot()


@router.get("/semi/poll")
async def semi_poll(since: str = "") -> dict[str, Any]:
    """前端轮询：`since` 传上次拿到的最后一个条目 id。"""
    return get_semi().poll(since)


@router.post("/semi/commit")
async def semi_commit(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """把挂起的条目落库。归属或时间没有依据时**不会**替你决定。"""
    capture_id = str(payload.get("capture_id") or "")
    if not capture_id:
        raise HTTPException(status_code=422, detail="要指定 capture_id。")
    cap = await asyncio.to_thread(get_semi().commit, capture_id, {
        "items": payload.get("items") or {},
        "apply_to_rest": payload.get("apply_to_rest") or "",
    })
    if cap is None:
        raise HTTPException(status_code=404, detail=f"没有这条待确认内容：{capture_id}")
    return {"ok": cap.status == "committed", "capture": _capture_out(cap)}


@router.post("/semi/discard")
async def semi_discard(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    capture_id = str(payload.get("capture_id") or "")
    cap = await asyncio.to_thread(get_semi().discard, capture_id,
                                  str(payload.get("reason") or ""))
    if cap is None:
        raise HTTPException(status_code=404, detail=f"没有这条待确认内容：{capture_id}")
    return {"ok": True, "capture": _capture_out(cap)}


@router.post("/semi/clear")
async def semi_clear() -> dict[str, Any]:
    """只清掉已处理的记录，待确认的一条都不动。"""
    removed = await asyncio.to_thread(get_semi().clear)
    return {"ok": True, "removed": removed}


def _capture_out(cap) -> dict[str, Any]:
    from ..collect.semi import _capture_dict

    return _capture_dict(cap)
