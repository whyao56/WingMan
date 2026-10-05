"""「以人为中心」的接口：人物实体、渠道归属、跨渠道时间线。

为什么单独一个文件而不是塞进 routes_data.py：这里的主语是**人**，
而 routes_data.py 的主语是**会话**。两者读起来是两套心智模型 ——
「她昨天说过什么」和「这条会话有多少条消息」不是同一类问题。
分开之后，将来给「人」加东西（关系分析、阶段目标复盘）也不必动数据的路由。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from ..context import get_ctx
from ..schemas import (
    Fact, Person, PersonDetail, PersonOverview, PersonPersona,
)

log = logging.getLogger("wingman.api.persons")
router = APIRouter(prefix="/api", tags=["persons"])


def _require_person(person_id: str) -> Person:
    person = get_ctx().store.get_person(person_id)
    if person is None:
        raise HTTPException(status_code=404, detail=f"这个人不存在：{person_id}")
    return person


# ================================================================ 人


@router.get("/persons", response_model=list[Person])
async def list_persons() -> list[Person]:
    return await asyncio.to_thread(get_ctx().store.list_persons)


@router.post("/persons", response_model=Person)
async def create_person(payload: dict[str, Any] = Body(...)) -> Person:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="新建一个人时至少要填名字")
    store = get_ctx().store

    # 重名先拦一下：两个「小鹿」并存会让「重命名 / 合并」变成猜谜。
    # 但只提示不强制 —— 现实里真的有同名的人，所以带上 force 就能建。
    existing = await asyncio.to_thread(store.find_person_by_alias, name)
    if existing and not payload.get("force"):
        raise HTTPException(
            status_code=409,
            detail=f"已经有一个叫「{existing.name}」的人了。是同一个人就打开 Ta 去合并，"
                   f"确实不是就勾选「仍然新建」。",
        )

    aliases = payload.get("aliases") or []
    person_id = await asyncio.to_thread(store.create_person, name, aliases)
    patch = {k: payload[k] for k in ("relation", "desired_relation", "stage_goal", "notes")
             if k in payload}
    if patch:
        await asyncio.to_thread(store.update_person, person_id, patch)

    # 新建时可以直接挂一批会话上来 —— 「新建人 + 选渠道」是同一个动作
    chat_ids = payload.get("chat_ids") or []
    for cid in chat_ids:
        await asyncio.to_thread(store.bind_chat, str(cid), person_id)
    got = await asyncio.to_thread(store.get_person, person_id)
    assert got is not None
    return got


@router.get("/persons/{person_id}", response_model=PersonDetail)
async def get_person_detail(person_id: str) -> PersonDetail:
    detail = await asyncio.to_thread(get_ctx().store.person_detail, person_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"这个人不存在：{person_id}")
    return detail


@router.patch("/persons/{person_id}", response_model=Person)
async def update_person(person_id: str, payload: dict[str, Any] = Body(...)) -> Person:
    """局部更新：只改传进来的字段。

    关系 / 期望关系 / 阶段目标这三项是用户手写的，绝不能因为一次
    「只改了个名字」的保存而被清空。
    """
    try:
        return await asyncio.to_thread(get_ctx().store.update_person, person_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/persons/{person_id}")
async def delete_person(person_id: str) -> dict[str, Any]:
    """删掉这个人本身。

    **不删下属的聊天记录** —— 消息是不可再生资产，删一个人不该顺手毁掉几万条记录。
    只解除绑定，会话会回到「未归属」状态，可以再挂给别人。
    """
    _require_person(person_id)
    store = get_ctx().store
    detail = await asyncio.to_thread(store.person_detail, person_id)
    released = len(detail.channels) if detail else 0
    await asyncio.to_thread(store.delete_person, person_id)
    return {"ok": True, "released_chats": released}


@router.post("/persons/{person_id}/merge", response_model=Person)
async def merge_persons(person_id: str, payload: dict[str, Any] = Body(...)) -> Person:
    """把别的「人」并进这一个（跨平台归并）。

    场景：QQ 上的「小鹿」和微信上的「鹿鹿」其实是同一个人。合并后
    记忆、关系定位、阶段目标都归到一处。
    """
    _require_person(person_id)
    ids = payload.get("merge_ids") or payload.get("ids") or []
    if isinstance(ids, str):
        ids = [x.strip() for x in ids.replace("，", ",").split(",") if x.strip()]
    ids = [str(i) for i in ids if str(i) != person_id]
    if not ids:
        raise HTTPException(status_code=422, detail="要合并哪些人？请给出 merge_ids")
    try:
        return await asyncio.to_thread(get_ctx().store.merge_persons, person_id, ids)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/persons/{person_id}/messages")
async def person_messages(
    person_id: str, limit: int = 200, before_id: int | None = None
) -> dict[str, Any]:
    """跨渠道时间线：这个人名下所有来源的消息，按时间合并。

    每行带 `channel` / `chat_name`，让界面能标出「这条是通话里说的」，
    但排序只看时间 —— 用户回忆一件事的时候，不会按平台分批回忆。
    """
    _require_person(person_id)
    rows = await asyncio.to_thread(
        get_ctx().store.person_messages, person_id, min(limit, 2000), before_id
    )
    return {"person_id": person_id, "messages": rows, "has_more": len(rows) >= min(limit, 2000)}


# ================================================================ 对象详情（需求 1）

_PERSON_FACT_SUBJECTS = ("peer", "me", "relationship")


@router.get("/persons/{person_id}/overview", response_model=PersonOverview)
async def person_overview(person_id: str) -> PersonOverview:
    """对象详情聚合：基础字段 + 渠道 + 计数 + 最近消息/输出/痕迹 + 待办。

    一次请求把对象详情页首屏要的东西给全，省掉前端拼接五六个接口。
    """
    detail = await asyncio.to_thread(get_ctx().store.person_detail, person_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"这个人不存在：{person_id}")
    ctx = get_ctx()
    providers = ctx.provider_summary()

    def build() -> PersonOverview:
        store = ctx.store
        person = detail.person
        facts = store.list_facts_for_person(person_id)
        runs = store.list_runs(person_id, limit=5)
        activity = store.list_activity(person_id=person_id, limit=20)
        todos: list[str] = []
        for p in providers:
            if p["kind"] == "llm" and (not p["available"] or p["name"] == "mock"):
                todos.append("还没配置大模型（当前是演示用的 mock），指挥台给不出真实建议。")
                break
        if not detail.channels:
            todos.append("还没有绑定任何渠道，去「采集」导一段或采一段。")
        if person.message_count == 0:
            todos.append("这个对象名下还没有聊天记录。")
        if not person.relation and not person.stage_goal:
            todos.append("还没填关系 / 阶段目标，指挥台的建议会偏泛。")
        return PersonOverview(
            person=person,
            channels=detail.channels,
            counts={
                "channels": len(detail.channels),
                "messages": person.message_count,
                "peer": person.peer_count,
                "me": person.me_count,
                "facts": len(facts),
                "runs": len(runs),
            },
            recent_messages=store.person_messages(person_id, limit=20),
            recent_runs=runs,
            activity=activity,
            todos=todos,
        )

    return await asyncio.to_thread(build)


# ================================================================ 对象级事实


@router.get("/persons/{person_id}/facts", response_model=list[Fact])
async def list_person_facts(person_id: str, subject: str | None = None) -> list[Fact]:
    """对象级 + 该对象名下渠道级的事实合并视图，逐条标 `scope`（person / chat）。"""
    _require_person(person_id)
    return await asyncio.to_thread(
        get_ctx().store.list_facts_for_person, person_id, subject
    )


@router.post("/persons/{person_id}/facts", response_model=Fact)
async def add_person_fact(person_id: str, payload: dict[str, Any] = Body(...)) -> Fact:
    """新增一条**对象级**事实（不绑具体渠道）。"""
    _require_person(person_id)
    subject = str(payload.get("subject") or "peer")
    if subject not in _PERSON_FACT_SUBJECTS:
        subject = "peer"
    fact = Fact(
        person_id=person_id, scope="person", subject=subject,
        key=str(payload.get("key") or "").strip(),
        value=str(payload.get("value") or "").strip(),
        confidence=float(payload.get("confidence") or 0.9),
        evidence=str(payload.get("evidence") or "手动添加"),
    )
    if not fact.key or not fact.value:
        raise HTTPException(status_code=422, detail="key 和 value 不能为空。")
    saved = await asyncio.to_thread(get_ctx().store.upsert_person_fact, person_id, fact)
    if saved is None:
        raise HTTPException(status_code=404, detail=f"这个人不存在：{person_id}")
    return saved


@router.delete("/persons/{person_id}/facts/{fact_id}")
async def delete_person_fact(person_id: str, fact_id: int) -> dict[str, Any]:
    """删一条属于这个人的事实（对象级或它的渠道级）。

    先确认这条事实确实在这个人的视野里，避免用错 person_id 就删到别人家的记忆。
    """
    _require_person(person_id)
    store = get_ctx().store
    mine = {f.id for f in await asyncio.to_thread(store.list_facts_for_person, person_id)}
    if fact_id not in mine:
        raise HTTPException(status_code=404, detail=f"这条事实不属于这个人：{fact_id}")
    await asyncio.to_thread(store.delete_fact, fact_id)
    return {"ok": True, "deleted": fact_id}


# ================================================================ 对象级人物设定


@router.get("/persons/{person_id}/persona", response_model=PersonPersona)
async def get_person_persona(person_id: str) -> PersonPersona:
    _require_person(person_id)
    return await asyncio.to_thread(get_ctx().store.get_person_persona, person_id)


@router.put("/persons/{person_id}/persona", response_model=PersonPersona)
async def put_person_persona(person_id: str, payload: dict[str, Any] = Body(...)) -> PersonPersona:
    _require_person(person_id)
    allowed = {k: str(v) for k, v in payload.items()
               if k in ("goal", "my_style", "peer_profile", "taboos", "stage")}
    saved = await asyncio.to_thread(
        get_ctx().store.save_person_persona, person_id, allowed
    )
    if saved is None:
        raise HTTPException(status_code=404, detail=f"这个人不存在：{person_id}")
    return saved


# ================================================================ 历史留存（需求 11）


@router.get("/persons/{person_id}/history")
async def person_history(person_id: str, kind: str = "", limit: int = 50) -> dict[str, Any]:
    """这个对象的历史输出（engine_runs）与动作痕迹（activity_log）。

    `kind` 过滤的是动作痕迹的类别（import / collect_auto / collect_semi / edit / profile）。
    """
    _require_person(person_id)
    store = get_ctx().store
    size = max(1, min(int(limit or 50), 200))

    def build() -> dict[str, Any]:
        return {
            "person_id": person_id,
            "runs": [r.model_dump() for r in store.list_runs(person_id, limit=size)],
            "activity": [a.model_dump() for a in
                         store.list_activity(person_id=person_id, kind=kind, limit=size)],
        }

    return await asyncio.to_thread(build)


# ================================================================ 渠道归属


@router.post("/persons/{person_id}/channels")
async def bind_channel(person_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """把一个会话（渠道）挂到这个人名下。"""
    _require_person(person_id)
    chat_id = str(payload.get("chat_id") or "").strip()
    if not chat_id:
        raise HTTPException(status_code=422, detail="缺少 chat_id")
    store = get_ctx().store
    if store.get_chat(chat_id) is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{chat_id}")
    await asyncio.to_thread(
        store.bind_chat, chat_id, person_id, str(payload.get("channel") or "")
    )
    return {"ok": True, "chat_id": chat_id, "person_id": person_id}


@router.delete("/persons/{person_id}/channels/{chat_id}")
async def unbind_channel(person_id: str, chat_id: str) -> dict[str, Any]:
    """解除归属。会话本身和里面的消息都保留。"""
    _require_person(person_id)
    await asyncio.to_thread(get_ctx().store.unbind_chat, chat_id)
    return {"ok": True, "chat_id": chat_id}


# 采集游标（`/api/collect/cursors`）不在这里注册。
# 它属于 `routes_collect.py` —— 曾经两个 router 各注册了一份同样的路径，
# 结果先注册的那个**静默屏蔽**了后一个：改哪一份都不一定生效，
# 排查时看代码完全看不出问题。路径没变，只是归属明确了。
