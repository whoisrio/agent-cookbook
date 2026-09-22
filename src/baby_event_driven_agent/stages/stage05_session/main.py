"""Stage 5a 演示：会话与真相——log、投影与异常恢复。

需要仓库根 .env 里的 OPENAI_API_KEY / OPENAI_API_BASE / OPENAI_MODEL
（环境变量可覆盖）。前五段不打模型（ScriptedLLM），可以当基准反复跑。

六段（各自独立，跑哪个都行）：

1. **轨迹长什么样**（离线）：脚本化 LLM 跑一轮，把 entry 树打印出来——
   header 不是节点、认父不认子、一条 assistant 是一个节点、toolCallId 配对。
2. **投影**（离线）：messages = f(轨迹, 参数)。路径遍历 + 按类型分派 +
   sanitize 收口；同一份文件同一组参数两次投影逐字节相同；model_change
   覆盖式提取，元数据跳过。
3. **rewind**（离线）：回退是移动指针，被抛弃分支留在文件里；回退后追加 =
   分支（grep parentId 可见）；带摘要的 rewind（遗言不是对话）。
4. **异常恢复**（离线）：残尾（砍字节 → resume 停在完好处）、悬挂的工具调用
   （投影层两档修复，原文件字节不变）、悬挂审批闭合（一问必有一答）。
5. **session 切换**（离线）：fork 出一份新会话文件，两条轨迹分道扬镳，
   各自独立 resume。
6. **真跑一轮**：真模型 + 真工具，两层事实（EventLog / 轨迹）各自记账。

行首标签沿用前几章：

    用户 │ 用户说了什么
    思考 │ assistant 的 thinking（暗色）
    回答 │ assistant 的可见输出（亮蓝）
    工具 │ 工具调用与真实结果（绿色）；被治理拦下用亮红
    系统 │ 生命周期与统计（绿色 / 亮红）
    说明 │ 旁白，只解释这一段在演示什么（灰色缩进）

    python -m baby_event_driven_agent.stages.stage05_session
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

from .agent import Agent
from .transport.bus import EventBus
from .agent import build_context
from .transport.events import Event, Subscription, OBSERVE
from .llm import RealLLM
from .transport.persistence import EventLog
from .session.store import SessionStore, session_facts, sweep_hanging_approvals
from .session.trajectory import (
    LABEL,
    MESSAGE,
    MODEL_CHANGE,
    Trajectory,
    TrajectoryLog,
    message_payload,
)

# ---------------------------------------------------------------- 屏幕上色
DIM = "\033[2m"
GREY = "\033[90m"
BLUE = "\033[94m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"

TIMEOUT = 20.0

CASE_ORDER = (
    "01-trajectory-shape",
    "02-projection",
    "03-rewind",
    "04-recovery",
    "05-fork",
    "06-live-turn",
)
CASE_TITLES = {
    "01-trajectory-shape": "第 1 段：轨迹长什么样（离线）",
    "02-projection": "第 2 段：投影 messages = f(轨迹, 参数)（离线）",
    "03-rewind": "第 3 段：rewind——回退是移动指针（离线）",
    "04-recovery": "第 4 段：异常恢复——残尾、悬挂调用、悬挂审批（离线）",
    "05-fork": "第 5 段：session 切换——fork（离线）",
    "06-live-turn": "第 6 段：真跑一轮，两层事实各自记账",
}
ALL_TITLE = "六段全跑（离线 1-5 + 真模型 6）"
CASE_IDS = ("all", *CASE_ORDER)


def line(label: str, color: str, text: str) -> None:
    print(f"\n{BOLD}{color}[{label}] {RESET}{color}{text}{RESET}")


def note(text: str) -> None:
    print(f"{GREY}       说明 │ {text}{RESET}")


def banner(name: str, title: str, what: str) -> None:
    print(f"\n{BOLD}── {name} · {title} ──{RESET}")
    note(what)


def brief(text: str, limit: int = 110) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


# ---------------------------------------------------------------- 脚手架

CALL_QUERY = [
    {
        "type": "tool_call_delta",
        "index": 0,
        "id": "call_1",
        "name": "query_inventory",
        "args_delta": '{"category": "保温杯"}',
    }
]
FINAL_TEXT = [{"type": "text_delta", "text": "保温杯库存 42 件，316L 不锈钢内胆。"}]


class ScriptedLLM:
    """脚本化 LLM：每个请求吐一段固定增量（和 stage04 的测试同一套）。"""

    def __init__(self, script: list[list[dict[str, Any]]]) -> None:
        self.script = script
        self.calls = 0

    async def stream_chat(self, messages: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for chunk in self.script[idx]:
            yield chunk


class Harness:
    """离线台子：bus + EventLog + store + agent，外加一个 turn_end 信号。"""

    def __init__(self, workdir: Path, script: list[list[dict[str, Any]]]) -> None:
        self.log = EventLog(str(workdir / "events"))
        self.bus = EventBus(self.log)
        self.store = SessionStore(workdir / "sessions")
        self.system_prompt = "你是一个通过工具干活的通用 agent。"
        self.agent = Agent(self.bus, ScriptedLLM(script), store=self.store, system_prompt=self.system_prompt)
        self.traj = self.store.start(
            cwd=str(workdir), model="fake-model", system_prompt=self.system_prompt
        )
        self.agent.attach(self.traj)
        self.sid = self.traj.sid
        self.ended = asyncio.Event()

        async def on_end(event: Event) -> None:
            self.ended.set()

        self.bus.subscribe(Subscription("rec-end", ("turn_end",), on_end, mode=OBSERVE))

    def send(self, text: str) -> None:
        self.bus.publish(Event("user_input", self.sid, {"text": text}), to=self.agent.agent_id)

    async def wait_turn(self) -> None:
        await asyncio.wait_for(self.ended.wait(), TIMEOUT)
        self.ended.clear()
        await self.bus.drain(timeout=TIMEOUT)

    async def stop(self) -> None:
        await self.agent.stop()
        await self.bus.drain(timeout=TIMEOUT)


def show_trajectory(traj: Trajectory, *, tail: int = 0) -> None:
    """把轨迹打印成链：id ← parentId，type + 消息摘要。"""
    entries = traj.entries()
    for e in entries[-tail:] if tail else entries:
        desc = ""
        if e.type == MESSAGE:
            m = e.payload.get("message", {})
            role = m.get("role", "?")
            if m.get("tool_calls"):
                calls = "、".join(c["function"]["name"] for c in m["tool_calls"])
                desc = f"{role} → toolCall({calls})"
            else:
                desc = f"{role}: {brief(m.get('content') or '')}"
            if e.payload.get("synthetic"):
                desc += f"  [synthetic·{e.payload.get('note')}]"
        elif e.type == MODEL_CHANGE:
            desc = f"→ {e.payload.get('model_id')}"
        elif e.type in ("session_started", "session_resumed", "session_end"):
            desc = brief(json.dumps(e.payload, ensure_ascii=False))
        elif e.type == LABEL:
            desc = str(e.payload.get("text", ""))
        else:
            desc = brief(json.dumps(e.payload, ensure_ascii=False))
        parent = e.parent_id or "∅"
        line("entry", GREY, f"{e.id} ← {parent}  {e.type:<16} {desc}")


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(asyncio.wait_for(coro, TIMEOUT))


# ---------------------------------------------------------------- 第 1 段


async def case_trajectory_shape(workdir: Path) -> None:
    banner(
        "01-trajectory-shape",
        "轨迹长什么样",
        "脚本化 LLM 跑一轮（user → assistant 要调工具 → tool 结果 → assistant 收尾）。"
        "看落盘的 entry 树：header 不是节点；每行只带 parentId（认父不认子）；"
        "一条 assistant 连 thinking 占位带 toolCall 是一个节点。",
    )
    h = Harness(workdir, [CALL_QUERY, FINAL_TEXT])
    line("用户", YELLOW, "保温杯还有库存吗")
    t0 = time.perf_counter()
    h.send("保温杯还有库存吗")
    await h.wait_turn()
    await h.stop()
    note(f"一轮跑完 {time.perf_counter() - t0:.2f}s（ScriptedLLM，无网络）")

    line("系统", GREEN, f"轨迹文件：{h.store.path_of(h.sid).name}")
    show_trajectory(h.traj)

    header, entries, torn = TrajectoryLog(h.store.path_of(h.sid)).read()
    roles = [
        str(e["payload"]["message"].get("role"))
        for e in entries
        if e["type"] == MESSAGE
    ]
    line(
        "统计",
        GREEN,
        f"文件 {len(entries)} 条 entry + 1 条 header（不是节点，type=session）；"
        f"message 里 {roles.count('user')} user / {roles.count('assistant')} assistant / "
        f"{roles.count('tool')} tool；残尾={torn}",
    )
    line("系统", GREEN, f"文件头原文：{json.dumps(header, ensure_ascii=False)}")
    if h.traj.branch_points():
        line("实测", RED, "发现了分叉点（不该有）")
    else:
        line("实测", GREEN, "分叉点：无（220 条一类纯链的轨迹，树是按需要准备的）")
    note(
        "认父不认子：每行只有 parentId，文件里没有一个字提到孩子。想找分叉要靠"
        "按 parentId 反查——这张 log 里一次 rewind 都没有，轨迹就是一条链。"
    )


# ---------------------------------------------------------------- 第 2 段


async def case_projection(workdir: Path) -> None:
    banner(
        "02-projection",
        "投影 messages = f(轨迹, 参数)",
        "路径遍历（leafId 沿 parentId 回根）→ 按类型分派（消息进 messages、"
        "model_change 覆盖变量、元数据跳过）→ sanitize 收口。"
        "同一份文件同一组参数，两次投影逐字节相同——这是 eval 的地基。",
    )
    h = Harness(workdir, [CALL_QUERY, FINAL_TEXT])
    h.send("保温杯还有库存吗")
    await h.wait_turn()
    await h.stop()

    traj = h.traj
    traj.append(LABEL, {"text": "关键节点：首轮问答"})
    traj.append(MODEL_CHANGE, {"model_id": "qwen3.5:4b-32k", "by": "user"})

    p1 = build_context(traj)
    p2 = build_context(traj)
    line("实测", GREEN, f"两次投影逐字节相同：{json.dumps(p1.messages, ensure_ascii=False) == json.dumps(p2.messages, ensure_ascii=False)}")
    line("实测", GREEN, f"覆盖式提取的 model：{p1.model}（store.start 落的 model_change，路径上最后一次生效）")
    line("实测", GREEN, f"投影统计：{p1.stats}")
    line("系统", GREEN, "投影出的 messages：")
    for m in p1.messages:
        body = m.get("content") if m.get("role") != "assistant" else None
        extra = f" toolCall({', '.join(c['function']['name'] for c in m['tool_calls'])})" if m.get("tool_calls") else ""
        line("  ", GREY, f"{m['role']:<9} {brief(body) if body else ''}{extra}")

    note(
        "system 不在轨迹里——它是参数不是事实，由 agent 在投影时统一前置，"
        "原文记在 header（审计用）；"
        "label 是纯元数据，进了轨迹但进不了上下文（stats.skipped 里能数出来）。"
    )


# ---------------------------------------------------------------- 第 3 段


async def case_rewind(workdir: Path) -> None:
    banner(
        "03-rewind",
        "rewind：回退是移动指针，不是删数据",
        "跑一轮之后回退到那条 user 上：没有任何 entry 被删除，只是 leafId 指针移过去。"
        "回退后继续追加，分支就出现了（两个节点共享同一个 parent）。最后来一次"
        "带摘要的 rewind：摘要是被抛弃分支的遗言，不是真实对话。",
    )
    h = Harness(workdir, [CALL_QUERY, FINAL_TEXT])
    h.send("保温杯还有库存吗")
    await h.wait_turn()
    await h.stop()

    traj = h.traj
    entries = traj.entries()
    user1 = next(e for e in entries if e.type == MESSAGE and e.payload["message"]["role"] == "user")
    before = len(traj.entries())
    before_bytes = traj.log.raw_bytes()

    traj.branch(user1.id)  # 核心就这一行
    after_branch = len(traj.entries())
    line("实测", GREEN, f"branch({user1.id})：文件里还是 {after_branch} 条 entry（{before} → {after_branch}，一条没删），字节未变：{traj.log.raw_bytes() == before_bytes}")
    line("实测", GREEN, f"回退后投影只剩 {len(build_context(traj).messages)} 条消息（system + 那条 user）")

    traj.append(MESSAGE, message_payload({"role": "user", "content": "换个思路：查一下玻璃杯"}))
    dup = traj.branch_points()
    at = next(iter(dup))
    line("实测", GREEN, f"回退后追加 = 分支：{at} 现在有两个孩子 {dup[at]}（grep parentId 的程序版）")

    summary = traj.branch_with_summary(
        user1.id, "试过查保温杯库存（42 件），结论：库存充足，无需补货。"
    )
    line("实测", GREEN, f"branch_with_summary：摘要节点 {summary.id} 挂在 {user1.id} 下（{summary.payload['note']}）")
    line("系统", GREEN, "现在的投影（新分支的 agent 看到的）：")
    for m in build_context(traj).messages:
        line("  ", GREY, f"{m['role']:<9} {brief(m.get('content') or '')}")

    note(
        "被抛弃的分支原样躺在文件里——想回头随时能回（branch 回去即可）。摘要把"
        "“之前试过什么、结论是什么”带进新分支，但它是 <summary> 视图，不是对话。"
    )


# ---------------------------------------------------------------- 第 4 段


async def case_recovery(workdir: Path) -> None:
    banner(
        "04-recovery",
        "异常恢复：残尾、悬挂调用、悬挂审批",
        "三个恢复现场，全部以“轨迹是唯一真相”为基准：崩在写一半的字节（CRC 判定）；"
        "崩在工具执行前的语义残尾（投影层两档修复，原文件字节不变）；"
        "进程被硬杀留下的孤立审批请求（resume 时按未授权闭合）。",
    )

    # —— 现场一：字节级残尾 ——
    h1 = Harness(workdir / "torn", [CALL_QUERY, FINAL_TEXT])
    h1.send("保温杯还有库存吗")
    await h1.wait_turn()
    await h1.stop()
    path1 = h1.store.path_of(h1.sid)
    intact = len(TrajectoryLog(path1).read()[1])
    raw = path1.read_bytes()
    path1.write_bytes(raw[:-11])  # 砍掉最后 11 字节 = 崩在写一半
    _, after_cut, torn_before = TrajectoryLog(path1).read()  # resume 之前先记账
    resumed = h1.store.resume(h1.sid, note="crash 恢复演练")
    line(
        "实测",
        GREEN,
        f"残尾：完好 {intact} 条 → 砍 11 字节后读到 {len(after_cut)} 条"
        f"（停在坏记录之前，torn={torn_before}）→ resume 裁掉残尾续写，"
        f"resumed 事件留痕 torn_tail={session_facts(resumed)[-1]['payload']['torn_tail']}",
    )

    # —— 现场二：悬挂的工具调用（崩在工具执行前） ——
    h2 = Harness(workdir / "dangling", [])
    traj = h2.traj
    traj.append(MESSAGE, message_payload({"role": "user", "content": "把库存改成 45 件"}))
    traj.append(
        MESSAGE,
        message_payload(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_x1",
                        "type": "function",
                        "function": {"name": "update_inventory", "arguments": "{}"},
                    }
                ],
            }
        ),
    )
    # 进程到这里被硬杀：assistant 要了工具结果，结果永远没来
    before_bytes = traj.log.raw_bytes()
    fixed = build_context(traj, system_prompt=h2.system_prompt)  # 补自描述占位
    tail = [m for m in fixed.messages if m.get("role") == "tool"]
    line(
        "实测",
        GREEN,
        f"悬挂调用·补占位：投影补了 {len(tail)} 条占位 → {tail[0]['content'] if tail else '-'}；"
        f"原文件字节未变：{traj.log.raw_bytes() == before_bytes}",
    )
    # —— 现场三：孤立的审批请求 ——
    h3 = Harness(workdir / "approval", [FINAL_TEXT])
    h3.bus.record(
        Event(
            "approval_required",
            h3.sid,
            {"request_id": "ap-deadbeef", "name": "update_inventory", "arguments": "{}"},
        )
    )
    closed = sweep_hanging_approvals(h3.bus, h3.sid)
    again = sweep_hanging_approvals(h3.bus, h3.sid)
    line(
        "实测",
        GREEN,
        f"悬挂审批：孤立请求 ap-deadbeef → 闭合 {closed}；再扫一遍：{again}（幂等，闭合过的不再碰）",
    )
    note(
        "三处的共同纪律：没写完的不算已发生（字节级）；修复只作用于喂给模型的"
        "投影（语义级）；补的裁决走 record 留痕，不伪造“当时批过”（审批）。"
    )
    await h2.stop()
    await h3.stop()


# ---------------------------------------------------------------- 第 5 段


async def case_fork(workdir: Path) -> None:
    banner(
        "05-fork",
        "session 切换：fork",
        "把当前路径克隆进一份新会话文件（id 与 parentId 原样保留）。"
        "新文件是完整合法的轨迹，可以独立继续生长；旧文件原封不动。",
    )
    h = Harness(workdir, [CALL_QUERY, FINAL_TEXT])
    h.send("保温杯还有库存吗")
    await h.wait_turn()
    await h.stop()

    forked = h.store.fork(h.traj, note="demo：分叉出轻量副本")
    # 两条轨迹分道扬镳：各追加一轮，互不可见
    h.agent.attach(h.traj)
    h2_sid = h.agent.attach(forked)
    h.traj.append(MESSAGE, message_payload({"role": "user", "content": "旧会话的下一句"}))
    forked.append(MESSAGE, message_payload({"role": "user", "content": "新会话的下一句"}))

    line("系统", GREEN, f"store 里的会话：{h.store.list_sessions()}")
    for name, t in (("原会话", h.traj), ("分叉", forked)):
        p = build_context(t)
        last_user = [m for m in p.messages if m.get("role") == "user"][-1]["content"]
        line("实测", GREEN, f"{name} {t.sid[:8]}…：{len(t.entries())} 条 entry，最后一条 user = {brief(last_user)}")
    line("实测", GREEN, f"分叉的生命周期事实：{json.dumps(session_facts(forked)[-1], ensure_ascii=False)}")
    note(
        "session 切换不修改历史，只创造新的“当前”：模型切换是树上的新节点，"
        "会话切换是新文件——都是追加，都不是改写。"
    )
    _ = h2_sid


# ---------------------------------------------------------------- 第 6 段


async def case_live_turn(workdir: Path) -> None:
    banner(
        "06-live-turn",
        "真跑一轮，两层事实各自记账",
        "真模型 + 真工具。EventLog 记传输层的事件账（token 流、治理、生命周期），"
        "轨迹记会话的结构账（消息树、投影）。两层坐标不同：seq 与 entry id。",
    )
    try:
        llm = RealLLM()
    except RuntimeError as exc:
        line("系统", YELLOW, f"跳过真模型段：{exc}")
        return

    log = EventLog(str(workdir / "events"))
    bus = EventBus(log)
    store = SessionStore(workdir / "sessions")
    traj = store.start(cwd=str(workdir), model="live")
    agent = Agent(bus, llm, store=store)
    agent.attach(traj)
    ended = asyncio.Event()

    async def on_end(event: Event) -> None:
        ended.set()

    bus.subscribe(Subscription("rec-end", ("turn_end",), on_end, mode=OBSERVE))

    async def ui_tool(event: Event) -> None:
        if event.session_id == traj.sid:
            line("工具", GREEN, f"← {event.payload['name']} 结果：{brief(event.payload['result'])}")

    bus.subscribe(Subscription("ui-tool", ("tool_result",), ui_tool, mode=OBSERVE))

    line("用户", YELLOW, "保温杯还有库存吗")
    bus.publish(Event("user_input", traj.sid, {"text": "保温杯还有库存吗"}), to=agent.agent_id)
    await asyncio.wait_for(ended.wait(), 120.0)
    await bus.drain(timeout=30.0)
    await agent.stop()
    store.close(traj, reason="demo 结束")

    projection = build_context(traj)
    line("系统", GREEN, "轨迹（尾部 6 条）：")
    show_trajectory(traj, tail=6)
    line(
        "统计",
        GREEN,
        f"EventLog：seq 1..{log.last_seq}（传输层事件账）；轨迹：{len(traj.entries())} 条 entry"
        f"（会话结构账）；投影出 {len(projection.messages)} 条消息，model={projection.model}",
    )
    line("系统", GREEN, "投影出的 messages 尾部：")
    for m in projection.messages[-3:]:
        line("  ", GREY, f"{json.dumps(m, ensure_ascii=False)[:160]}")


CASES = {
    "01-trajectory-shape": case_trajectory_shape,
    "02-projection": case_projection,
    "03-rewind": case_rewind,
    "04-recovery": case_recovery,
    "05-fork": case_fork,
    "06-live-turn": case_live_turn,
}


async def main(case_ids: list[str] | None = None, sessions_dir: Path | None = None) -> None:
    root = sessions_dir or (Path(__file__).resolve().parents[2] / "sessions" / "stage05")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    picked = [c for c in CASE_ORDER if not case_ids or "all" in case_ids or c in case_ids]
    if not picked:
        print(f"{GREY}没有匹配的 case：{case_ids}；可选：{', '.join(CASE_IDS)}{RESET}")
        return

    print(f"{BOLD}Stage 5a：会话与真相 —— log、投影与异常恢复{RESET}")
    if case_ids and "all" not in case_ids:
        print(f"{GREY}  （只跑：{', '.join(picked)}）{RESET}")

    for i, cid in enumerate(picked):
        workdir = root / cid.split("-", 1)[1]
        workdir.mkdir(parents=True, exist_ok=True)
        await CASES[cid](workdir)
        if i < len(picked) - 1:
            print()

    print(f"\n{BOLD}── 收尾 ──{RESET}")
    line("系统", GREEN, "demo 结束")
    print(f"{GREY}  session log: {root}{RESET}")


def cli() -> None:
    """[project.scripts] 入口：stage05-demo。

        stage05-demo                      # 全部（默认；最后一段打真模型）
        stage05-demo 03-rewind            # 只跑指定段（离线段可当基准反复跑）
        stage05-demo --list               # 列 case 及其说明（不加载模型配置）
    """
    parser = argparse.ArgumentParser(
        prog="stage05-demo", description="Stage 5a 演示：会话与真相——log、投影与异常恢复。"
    )
    parser.add_argument("cases", nargs="*", metavar="CASE", help="要跑的 case（默认全部）")
    parser.add_argument("--list", action="store_true", help="列出所有 case 后退出")
    parser.add_argument("--sessions-dir", default=None, help="轨迹落点")
    args = parser.parse_args()
    if args.list:
        print(f"all\t{ALL_TITLE}")
        for cid in CASE_ORDER:
            print(f"{cid}\t{CASE_TITLES[cid]}")
        return
    asyncio.run(
        main(args.cases or None, Path(args.sessions_dir) if args.sessions_dir else None)
    )


if __name__ == "__main__":
    cli()
