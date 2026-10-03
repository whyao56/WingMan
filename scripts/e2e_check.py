"""对着正在运行的服务跑一遍 HTTP 端到端检查。

用法（先启动服务，再另开一个终端跑）：

    cd chatwing/backend
    python -m uvicorn app.main:app --port 8787

    # 另开终端
    cd chatwing/backend
    python ../scripts/e2e_check.py
    # 也可以指定地址：python ../scripts/e2e_check.py http://127.0.0.1:8787

它会依次验证：健康检查 → 导入预览 → 导入 → 画像 → 建议 → 推演 → 语音注入 → SSE，
每步打印关键结果，任何一步失败都会指出是哪里。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "qq_sample_小鹿.txt"
BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787").rstrip("/")
PEER_LINE = "哈哈哈今天好累啊"

PASS = 0
FAIL = 0


def U(path: str) -> str:
    """拼成绝对 URL。

    不要用 httpx 的 base_url —— base_url 不带尾斜杠时，
    它会把 "/api/x" 当相对路径处理，拼出畸形的请求目标，服务端直接 404。
    这个坑排查起来很费时间，所以这里显式拼接。
    """
    return BASE + path


def step(title: str) -> None:
    print(f"\n\033[36m▸ {title}\033[0m")


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"  \033[32m✓\033[0m {msg}")


def bad(msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  \033[31m✗\033[0m {msg}")


def check(cond: bool, good: str, evil: str) -> bool:
    if cond:
        ok(good)
    else:
        bad(evil)
    return bool(cond)


def main() -> int:
    # trust_env=False 很重要：设了 HTTP_PROXY / http_proxy 环境变量时，
    # httpx 默认把请求发给代理，代理会用「绝对地址」形式转发，
    # 本地 uvicorn 收到 http%3A//127.0.0.1%3A8787/api/... 这种路径，直接 404。
    # 检查本机服务时应该绕开代理。
    c = httpx.Client(timeout=180.0, trust_env=False)

    # ---------------------------------------------------- 0 健康检查
    step("健康检查")
    try:
        h = c.get(U("/api/health")).json()
    except Exception as exc:
        bad(f"连不上 {BASE} —— 服务启动了吗？({exc})")
        return 1
    ok(f"版本 {h['version']}，数据库 {Path(h['db']).name}")
    for p in h["providers"]:
        ok(f"{p['kind']:<9} → {p['name']:<14} {p['note'][:46]}")

    # ---------------------------------------------------- 1 导入预览
    step("导入预览（自动嗅探适配器）")
    if not SAMPLE.exists():
        bad(f"示例文件不存在：{SAMPLE}")
        return 1
    with SAMPLE.open("rb") as fh:
        r = c.post(U("/api/import/preview"),
                   files={"file": (SAMPLE.name, fh, "text/plain")})
    if r.status_code != 200:
        bad(f"HTTP {r.status_code}: {r.text[:200]}")
        return 1
    pv = r.json()
    check(pv["adapter"] == "qq", f"识别为 {pv['adapter']}",
          f"适配器识别错误，得到 {pv['adapter']}")
    top = pv["probes"][0]
    check(top["confidence"] >= 0.9, f"匹配度 {top['confidence']}",
          f"匹配度过低：{top['confidence']}")
    check(pv["total_parsed"] > 40, f"解析出 {pv['total_parsed']} 条消息",
          f"只解析出 {pv['total_parsed']} 条")

    # ---------------------------------------------------- 2 导入
    step("正式导入")
    with SAMPLE.open("rb") as fh:
        r = c.post(U("/api/import"),
                   files={"file": (SAMPLE.name, fh, "text/plain")},
                   data={"chat_name": "小鹿"})
    if r.status_code != 200:
        bad(f"HTTP {r.status_code}: {r.text[:200]}")
        return 1
    imp = r.json()
    chat_id = imp["chat_id"]
    ok(f"会话 {chat_id}：新增 {imp['inserted']} 条，跳过重复 {imp['skipped']} 条")
    for w in imp.get("warnings") or []:
        print(f"    \033[33m! {w}\033[0m")

    info = c.get(U(f"/api/chats/{chat_id}")).json()
    check(info["me_name"] == "我", "「我」的角色识别正确",
          f"「我」识别错误：{info['me_name']}")
    check(info["peer_name"] == "小鹿", "对方昵称识别正确",
          f"对方昵称识别错误：{info['peer_name']}")
    check(info["indexed"] > 0, f"自动建索引 {info['indexed']} 条",
          "导入后没有自动建立向量索引")

    # ---------------------------------------------------- 3 索引 + 画像
    step("重建索引 + 抽取画像")
    idx = c.post(U(f"/api/chats/{chat_id}/index")).json()
    ok(f"索引：{idx['indexed']} 条，维度 {idx['dim']}，模型 {idx['model']}")

    prof = c.post(U(f"/api/chats/{chat_id}/profile")).json()
    check(prof["facts_total"] > 0, f"抽取到 {prof['facts_total']} 条事实",
          "一条事实都没抽到")
    facts = c.get(U(f"/api/chats/{chat_id}/facts")).json()
    if facts:
        ok("事实样例：" + "；".join(f"{f['key']}={f['value']}" for f in facts[:6]))
    persona = c.get(U(f"/api/chats/{chat_id}/persona")).json()
    ok(f"关系阶段：{persona.get('stage') or '未判定'}")
    if persona.get("peer_profile"):
        ok(f"画像：{persona['peer_profile'][:70]}…")

    # ---------------------------------------------------- 4 设定目标
    step("写入目标与雷区")
    c.put(U(f"/api/chats/{chat_id}/persona"), json={
        "goal": "想约她周末去看展",
        "taboos": "宝贝、问她的前任",
        "stage": "暧昧期",
    })
    ok("已写入目标 / 雷区 / 阶段")

    # ---------------------------------------------------- 5 分析 + 建议
    step("核心：分析 + 建议")
    sug = c.post(U(f"/api/chats/{chat_id}/suggest"),
                 json={"peer_message": PEER_LINE, "persist": True}).json()
    a = sug["analysis"]
    ok(f"情绪：{a['emotion']}（强度 {a['emotion_intensity']}/10，兴趣变化 {a['interest_delta']:+d}）")
    ok(f"意图：{a['intent'][:60]}")
    ok(f"风险：{a['risk'][:60]}")
    check(len(a["signals"]) > 0, f"观测信号 {len(a['signals'])} 条", "没有观测信号")
    ok(f"本轮策略：{sug['strategy']['goal_this_turn'][:60]}")
    print(f"    \033[90m检索命中 {sug['trace'].get('retrieved')} 条历史 · "
          f"耗时 {sug['trace'].get('total_ms')}ms\033[0m")
    for w in sug.get("warnings") or []:
        print(f"    \033[33m! {w}\033[0m")

    check(len(sug["options"]) >= 3, f"生成 {len(sug['options'])} 条候选回复",
          f"只有 {len(sug['options'])} 条候选")
    print()
    for o in sug["options"]:
        print(f"    \033[1m{o['total']:>4.1f}\033[0m  [{o['id']}] {o['style']:<6} "
              f"{o['prediction']['direction']:<4} │ {o['text']}")
    totals = [o["total"] for o in sug["options"]]
    check(totals == sorted(totals, reverse=True), "候选按综合分降序排列",
          f"排序错误：{totals}")
    check(all(o["score_notes"] for o in sug["options"]), "每条都带打分依据",
          "有候选缺少打分依据")

    # ---------------------------------------------------- 6 推演
    step("推演：这条回复之后会怎么走")
    top = sug["options"][0]
    tree = c.post(U(f"/api/chats/{chat_id}/simulate"),
                  json={"option_id": top["id"], "option_text": top["text"],
                        "peer_message": PEER_LINE}).json()
    check(len(tree["branches"]) >= 2, f"推演出 {len(tree['branches'])} 个分支",
          "分支太少")
    for b in tree["branches"]:
        zh = {"likely": "最可能", "possible": "有可能", "unlikely": "不太可能"}.get(
            b["likelihood"], b["likelihood"])
        print(f"    {zh} {b['probability'] * 100:.0f}%  {b['label']}  →  {b['temperature']}")
        for s in b["steps"][:2]:
            who = "对方" if s["speaker"] == "peer" else "  你"
            print(f"        {who}: {s['text'][:52]}")
    check(abs(sum(b["probability"] for b in tree["branches"]) - 1.0) < 0.02,
          "分支概率已归一化", "概率没有归一化")
    check(bool(tree["disclaimer"]), "带免责声明", "缺少免责声明")

    # ---------------------------------------------------- 7 语音注入
    step("语音：手动注入一段转写")
    ing = c.post(U("/api/voice/ingest"),
                 json={"text": "喂你能听到吗", "channel": "peer", "chat_id": chat_id})
    check(ing.status_code == 200, "注入成功，会通过 SSE 推送",
          f"注入失败 HTTP {ing.status_code}")
    segs = c.get(U(f"/api/chats/{chat_id}/voice")).json()
    check(segs["count"] >= 1, f"语音记录已入库（{segs['count']} 条）", "语音记录没有入库")

    # ---------------------------------------------------- 8 SSE
    step("SSE 实时流")
    got = False
    try:
        with c.stream("GET", U("/api/voice/stream"), timeout=6.0) as s:
            for line in s.iter_lines():
                if line.startswith("data:"):
                    evt = json.loads(line[5:])
                    ok(f"收到事件：{evt.get('type')}")
                    got = True
                    break
    except Exception as exc:
        bad(f"SSE 连接异常：{exc}")
    if not got and FAIL == 0:
        bad("SSE 没有推送任何事件")

    # ---------------------------------------------------- 结果
    print("\n" + "=" * 60)
    if FAIL == 0:
        print(f"\033[32m全部通过（{PASS} 项检查）\033[0m")
        print(f"打开控制台看看：{BASE}")
    else:
        print(f"\033[31m{PASS} 项通过，{FAIL} 项失败\033[0m")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
