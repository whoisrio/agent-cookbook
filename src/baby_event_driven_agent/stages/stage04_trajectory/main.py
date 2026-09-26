"""能力升级 + 会话与真相演示（03c / 04 配套）。

需要仓库根 .env 里的 OPENAI_API_KEY / OPENAI_API_BASE / OPENAI_MODEL
（环境变量可覆盖）。离线段用 ScriptedLLM（可断言、可反复跑），
真模型段打真模型（RealLLM，配置读仓库根 .env），需要 API key。

十二段（各自独立，跑哪个都行）：

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
7. **UI 缓冲**（离线）：200 个 token 增量进 CoalescingBuffer——满格 / 帧界才
   刷屏，一条不丢（照搬 stage03b 的“UI 消息缓冲”机制）。
8. **工具审批**（离线）：工具声明判量超限才要人，执行前发 approval_required，
   人批准才执行（照搬 stage03b 的“评审消息处理”，声明升级为判量）。
9. **输入意图**（离线）：turn 在飞时的新消息默认插话（steering），等不了就
   redirect 打断转向（stage03b 的 followup/steering/redirect 意图，收敛到
   agent 的 step 边界）。
10. **工具层直调**（离线）：任务域两个工具、判量审批边界、数据隔离——
   能力升级的交付物，不打模型。
11. **长程任务**（离线）：五轮 19 步跑完整张任务单（补货核查 + 盘点差异），
   审批判量在真实流程里触发；上下文只增不减——04 要接手的现场。
12. **真模型长任务**：缩减版任务端到端，真工具、真审批。
13. **长任务的轨迹解法**（离线）：同一张任务单换轨迹跑法——纠偏 =
   rewind + 遗言；上下文长 = compact_request 在 step 边界换视图；
   崩一刀 = resume 恢复出压缩视图、悬挂调用补占位。

行首标签沿用前几章：

    用户 │ 用户说了什么
    思考 │ assistant 的 thinking（暗色）
    回答 │ assistant 的可见输出（亮蓝）
    工具 │ 工具调用与真实结果（绿色）；被治理拦下用亮红
    系统 │ 生命周期与统计（统一橙色）
    说明 │ 旁白，只解释这一段在演示什么（灰色缩进）

    python -m baby_event_driven_agent.stages.stage04_trajectory
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

from .agent import Agent, UNKNOWN_TOOL_RESULT, build_context
from . import tools as tools_mod
from .transport.bus import EventBus
from .agent import build_context
from .transport.events import Event, Subscription, OBSERVE, STEERING, UserMessage
from .llm import RealLLM
from .transport.outbound import StreamConsumer
from .transport.persistence import EventLog
from .session.compaction import ScriptedSummarizer
from .session.store import SessionStore, session_facts, sweep_hanging_approvals
from .session.trajectory import (
    COMPACTION,
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
ORANGE = "\033[38;5;208m"  # 系统提示统一橙色
BOLD = "\033[1m"
RESET = "\033[0m"

TIMEOUT = 20.0

CALL_QUERY = [
    {
        "type": "tool_call_delta",
        "index": 0,
        "id": "call_1",
        "name": "query_inventory",
        "args_delta": '{"category": "保温杯"}',
    }
]
FINAL_TEXT = [{"type": "text_delta", "text": "保温杯库存 3 件，316L 不锈钢内胆。"}]

CASE_ORDER = (
    "01-trajectory-shape",
    "02-projection",
    "03-rewind",
    "04-recovery",
    "05-fork",
    "06-live-turn",
    "07-ui-buffer",
    "08-approval",
    "09-steering",
    "10-tools",
    "11-long-task",
    "12-live-task",
    "13-long-task-trajectory",
)
CASE_TITLES = {
    "01-trajectory-shape": "第 1 段：轨迹长什么样（离线）",
    "02-projection": "第 2 段：投影 messages = f(轨迹, 参数)（离线）",
    "03-rewind": "第 3 段：rewind——回退是移动指针（离线）",
    "04-recovery": "第 4 段：异常恢复——残尾、悬挂调用、悬挂审批（离线）",
    "05-fork": "第 5 段：session 切换——fork（离线）",
    "06-live-turn": "第 6 段：真跑一轮，两层事实各自记账",
    "07-ui-buffer": "第 7 段：UI 消息缓冲——满格 / 帧界才刷屏（离线）",
    "08-approval": "第 8 段：工具审批——要等人的那一半（离线）",
    "09-steering": "第 9 段：输入意图——插话与打断（离线）",
    "10-tools": "第 10 段：工具层直调——任务域与判量审批（离线）",
    "11-long-task": "第 11 段：长程任务——补货核查与盘点差异（离线）",
    "12-live-task": "第 12 段：真模型跑缩减版长任务",
    "13-long-task-trajectory": "第 13 段：长任务的轨迹解法——rewind、压缩、恢复（离线）",
}
ALL_TITLE = "十三段全跑（离线 1-5 / 7-11 / 13 + 真模型 6 / 12）"
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

class ScriptedLLM:
    """离线脚本化 LLM：按调用次序吐脚本块——离线段可断言、可反复跑。"""

    def __init__(self, script: list[list[dict[str, Any]]]) -> None:
        self.script = script
        self.calls = 0
        self.contexts: list[list[dict[str, Any]]] = []

    async def stream_chat(self, messages: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
        self.contexts.append([dict(m) for m in messages])
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for chunk in self.script[idx]:
            yield chunk


def use_data_copies(workdir: Path) -> dict[str, Path]:
    """把包自带 data/ 拷进工作目录并换掉工具的数据源（写操作落副本）。

    返回原路径快照，用完 restore_data(saved) 还原——demo / 测试共用这一对。
    """
    attrs = ("_INVENTORY", "_RULES", "_TASKS")
    saved = {a: getattr(tools_mod, a) for a in attrs}
    for a in attrs:
        src: Path = saved[a]
        dst = workdir / src.name
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        setattr(tools_mod, a, dst)
    return saved


def restore_data(saved: dict[str, Path]) -> None:
    for attr, path in saved.items():
        setattr(tools_mod, attr, path)


class Harness:
    """demo 台子：bus + EventLog + store + agent，外加一个 turn_end 信号。

    传 script 就是离线段（ScriptedLLM 驱动，确定性，不打模型）；
    live=True 打真模型（RealLLM）。
    """

    def __init__(
        self,
        workdir: Path,
        script: list[list[dict[str, Any]]] | None = None,
        *,
        live: bool = False,
    ) -> None:
        self.log = EventLog(str(workdir / "events"))
        self.bus = EventBus(self.log)
        self.store = SessionStore(workdir / "sessions")
        self.system_prompt = "你是一个通过工具干活的通用 agent。"
        llm = RealLLM() if live else ScriptedLLM(script or [])
        self.llm = llm
        self.agent = Agent(self.bus, llm, store=self.store, system_prompt=self.system_prompt)
        self.traj = self.store.start(
            cwd=str(workdir), model="fake-model", system_prompt=self.system_prompt
        )
        self.agent.attach(self.traj)
        self.sid = self.traj.sid
        self.ended = asyncio.Event()
        self.events: list[Event] = []

        async def on_end(event: Event) -> None:
            self.ended.set()

        async def on_any(event: Event) -> None:
            self.events.append(event)

        self.bus.subscribe(Subscription("rec-end", ("turn_end",), on_end, mode=OBSERVE))
        self.bus.subscribe(Subscription("rec-any", ("*",), on_any, mode=OBSERVE))

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
    h.send("保温杯还有库存吗")
    await h.wait_turn()
    await h.stop()
    note(f"脚本驱动，瞬时跑完（离线，可反复跑）")

    line("系统", ORANGE, f"轨迹文件：{h.store.path_of(h.sid).name}")
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
    line("系统", ORANGE, f"文件头原文：{json.dumps(header, ensure_ascii=False)}")
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
    line("系统", ORANGE, "投影出的 messages：")
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
        user1.id, "试过查保温杯库存（3 件），结论：库存偏紧，建议按目标补货。"
    )
    line("实测", GREEN, f"branch_with_summary：摘要节点 {summary.id} 挂在 {user1.id} 下（{summary.payload['note']}）")
    line("系统", ORANGE, "现在的投影（新分支的 agent 看到的）：")
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
    fixed = build_context(traj)  # 补自描述占位；system prompt 从 header 提取
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

    line("系统", ORANGE, f"store 里的会话：{h.store.list_sessions()}")
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
        line("系统", ORANGE, f"跳过真模型段：{exc}")
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
    line("系统", ORANGE, "轨迹（尾部 6 条）：")
    show_trajectory(traj, tail=6)
    line(
        "统计",
        GREEN,
        f"EventLog：seq 1..{log.last_seq}（传输层事件账）；轨迹：{len(traj.entries())} 条 entry"
        f"（会话结构账）；投影出 {len(projection.messages)} 条消息，model={projection.model}",
    )
    line("系统", ORANGE, "投影出的 messages 尾部：")
    for m in projection.messages[-3:]:
        line("  ", GREY, f"{json.dumps(m, ensure_ascii=False)[:160]}")


# ---------------------------------------------------------------- 第 7 段：UI 消息缓冲（照搬 stage04）


async def case_ui_buffer(workdir: Path) -> None:
    """UI 缓冲消费：add 只做缓冲追加，满格 / 帧界才刷屏，一条不丢。"""
    banner(
        "07-ui-buffer",
        "UI 消息缓冲：满格 / 帧界才刷屏",
        "往总线灌 200 个 agent_delta。UI 是 CoalescingBuffer 消费者：add 只做缓冲追加，"
        "满 96 字立刻刷，到帧界也刷；emit 不等刷屏（token 流可丢、可合并，不挡 loop）。",
    )

    class TextUI(StreamConsumer):
        def on_flush(self, text: str) -> None:
            line("UI", GREEN, f"刷一帧（{len(text)} 字）")

    bus = EventBus(EventLog(str(workdir / "events")), stream_size=1024)
    ui = TextUI()
    await ui.start()
    bus.subscribe(Subscription("ui", ("agent_delta",), ui))

    t0 = time.perf_counter()
    for _ in range(200):
        await bus.emit(Event("agent_delta", "A", {"text": "字"}))
    cost = time.perf_counter() - t0
    await asyncio.sleep(0.12)  # 等帧界把尾巴刷完
    await ui.stop()
    line(
        "实测",
        GREEN,
        f"200 个 delta 全部送达（merged={ui.merged}），emit 总耗时 {cost:.3f}s"
        "——emit 返回时只是进了缓冲（offer 微秒级，绝不挡 loop）",
    )
    line("实测", GREEN, f"200 次渲染合并成 {ui.flushes} 帧——刷屏节奏归消费者，不归总线")
    note("满了立刻刷（洪峰不攒着），到帧界也刷（尾巴不饿着），二者先到先刷。")
    await bus.drain(timeout=TIMEOUT)


# ---------------------------------------------------------------- 第 8 段：工具审批（照搬 stage04）


async def case_approval(workdir: Path) -> None:
    """工具审批：声明判量超限 → approval_required → 人批准 → 工具执行。"""
    banner(
        "08-approval",
        "工具审批：要等人的那一半",
        "update_inventory 的声明判量发现大额补货（补 97 件，超 50 件上限）要问人。"
        "agent 发 approval_required（带 request_id），答复由人给——这里用应答通道"
        "模拟人点了一下批准。答复走旁路直接 resolve，工具拿到授权才执行。"
        "一问必有一答；小补货则直接放行，不用问。",
    )
    saved = use_data_copies(workdir)
    demo_inventory = workdir / "inventory.txt"

    call_update = [
        {
            "type": "tool_call_delta",
            "index": 0,
            "id": "call_1",
            "name": "update_inventory",
            "args_delta": '{"category": "保温杯", "stock": 100}',
        }
    ]
    h = Harness(workdir / "approval", [call_update, FINAL_TEXT])
    arrived = asyncio.Event()

    async def on_required(event: Event) -> None:
        p = event.payload
        arrived.set()
        line(
            "系统",
            ORANGE,
            f"？ {p['name']} 要执行：{brief(p['arguments'])}"
            f"（request_id={p['request_id']}，超时 {p['timeout']:g}s）",
        )
        await asyncio.sleep(0.2)  # 人去点了一下
        line("用户", YELLOW, "→ 批准")
        h.bus.publish(
            Event(
                "user_approval",
                h.sid,
                {
                    "request_id": p["request_id"],
                    "approve": True,
                    "reason": "demo 里代替人点了一下",
                },
            ),
            to=h.agent.agent_id,
        )

    async def on_decided(event: Event) -> None:
        p = event.payload
        line("系统", ORANGE, f"确认结果：{p['action']} by {p['by']}")

    h.bus.subscribe(Subscription("d.required", ("approval_required",), on_required))
    h.bus.subscribe(Subscription("d.decided", ("approval_decided",), on_decided))
    try:
        line("用户", YELLOW, "把保温杯库存改成 100 件（小补货放行，这次是大额，要问人）")
        h.send("把保温杯库存改成 100 件")
        try:
            await asyncio.wait_for(arrived.wait(), TIMEOUT)
        except asyncio.TimeoutError:
            line("系统", ORANGE, "没等到 approval_required：模型这轮没发起工具调用")
            return
        await h.wait_turn()
        written = [
            ln
            for ln in demo_inventory.read_text(encoding="utf-8").splitlines()
            if ln.startswith("保温杯")
        ]
        line(
            "实测",
            GREEN,
            f"approval_required → 人批准 → approval_decided → 工具执行；"
            f"副本上现在是：{written[0] if written else '（没找到）'}",
        )
        note(
            "等不到答复时按拒绝处理（fail-closed）：不会是“没人管就放行”。"
            "问人的判量声明在工具自己身上（tools.py 的 approval_check），"
            "总线与订阅者不参与审批；小补货直接放行，问人只留给大额。"
            "残尾恢复时悬挂的审批也会被 resume 闭合（见第 4 段）。"
        )
    finally:
        restore_data(saved)
        await h.stop()


# ---------------------------------------------------------------- 第 9 段：输入意图（stage04 的 followup/steering/redirect）


async def case_steering(workdir: Path) -> None:
    """turn 在飞时的新消息：默认 steering 拼进本轮；等不了就 redirect 打断。"""
    banner(
        "09-steering",
        "输入意图：插话（steering）与打断（redirect）",
        "turn 在飞时用户又发来一条：默认在下一个 step 边界拼进当前轮（steering），"
        "本轮不断；若用户等不了，发 user_interrupt（redirect）让纠正先落地，本轮转向。",
    )
    h = Harness(workdir / "steering", [CALL_QUERY, FINAL_TEXT])

    async def on_steer(event: Event) -> None:
        line("系统", ORANGE, "插话拼进本轮：" + "、".join(event.payload["texts"]))

    async def on_redirect(event: Event) -> None:
        line("系统", ORANGE, f"打断转向（intent={event.payload.get('intent')}）")

    h.bus.subscribe(Subscription("d.steer", ("steering_consumed",), on_steer))
    h.bus.subscribe(Subscription("d.redirect", ("turn_interrupted",), on_redirect))

    line("用户", YELLOW, "保温杯还有库存吗")
    h.send("保温杯还有库存吗")
    # 模型在跑工具时，用户又发来一条带 STEERING 意图——直接进 steering 暂存，
    # 下一个 step 边界被拼进本轮（本轮不断）
    line("用户", YELLOW, "顺便查一下规则（STEERING 意图）")
    h.bus.publish(
        UserMessage("user_input", h.sid, {"text": "顺便查一下规则"}, intent=STEERING),
        to=h.agent.agent_id,
    )
    await h.wait_turn()
    line(
        "实测",
        GREEN,
        "在飞期间的新消息：默认作为 steering 拼进当前轮（不排队等下一轮、也不打断）",
    )
    note(
        "stage04 的意图（followup 排队 / steering 插话 / redirect 旁路）在这里收敛为"
        "“step 边界 drain 即 steering + user_interrupt 旁路”：输入消息的意图语义不变，"
        "只是路由落到了 agent 的 step 边界，而不是 UserMessage 的 intent 字段——"
        "机制照搬、落点不同。"
    )
    await h.stop()


# ---------------------------------------------------------------- 第 10 段：工具层直调


async def case_tools(workdir: Path) -> None:
    """工具层直调（不打模型）：任务域、判量审批边界、数据隔离、设值语义。"""
    banner(
        "10-tools",
        "工具层直调：任务域与判量审批（离线）",
        "能力升级的交付物直接看：任务域两个工具的返回形状；判量审批的边界"
        "（补 50 放行、补 51 问人、负差放行）；写操作设值语义，重放不叠加；"
        "数据隔离——写落在工作目录副本，包自带 data/ 与共享 knowledge-base "
        "一个字节不动。",
    )
    attrs = ("_INVENTORY", "_RULES", "_TASKS")
    saved = {a: getattr(tools_mod, a) for a in attrs}
    kb_dir = saved["_INVENTORY"].parents[2] / "knowledge-base"
    kb_before = {p.name: p.read_bytes() for p in kb_dir.glob("*.txt")}
    data_before = {a: p.read_bytes() for a, p in saved.items()}
    try:
        use_data_copies(workdir)

        line("实测", GREEN, f"list_tasks →\n{await tools_mod.list_tasks({})}")
        line("实测", GREEN, f"get_task(T-101) →\n{await tools_mod.get_task({'task_id': 'T-101'})}")
        line(
            "实测",
            GREEN,
            f"get_task(T-404) → {brief(await tools_mod.get_task({'task_id': 'T-404'}))}",
        )

        over = tools_mod._restock_approval({"category": "马克杯", "stock": 60})
        edge = tools_mod._restock_approval({"category": "保温杯", "stock": 53})
        under = tools_mod._restock_approval({"category": "保温杯", "stock": 50})
        shrink = tools_mod._restock_approval({"category": "雨伞", "stock": 13})
        line("实测", GREEN, f"判量：马克杯 3→60（补 52 件）→ {over}")
        line("实测", GREEN, f"判量：保温杯 3→53（补 50 件，贴线）→ {edge or '放行'}")
        line("实测", GREEN, f"判量：保温杯 3→50（补 47 件）→ {under or '放行'}")
        line("实测", GREEN, f"判量：雨伞 15→13（负差，盘点调整）→ {shrink or '放行'}")

        await tools_mod.update_inventory({"category": "保温杯", "stock": 50})
        await tools_mod.update_inventory({"category": "保温杯", "stock": 50})
        again = await tools_mod.query_inventory({"category": "保温杯"})
        line("实测", GREEN, f"设值语义重放：补到 50 两次，仍是一行 → {brief(again)}")

        kb_after = {p.name: p.read_bytes() for p in kb_dir.glob("*.txt")}
        data_after = {a: p.read_bytes() for a, p in saved.items()}
        line(
            "实测",
            GREEN,
            f"数据隔离：包 data/ 未变：{data_after == data_before}；"
            f"共享 knowledge-base 未变：{kb_after == kb_before}",
        )
        note(
            "判量的线和 rules.txt 里的补货规则是同一条：业务规则告诉模型"
            "“超 50 要报备”，工具声明告诉 harness“超 50 要问人”。"
            "任务单是只读输入：没有状态字段，“做到哪了”住在会话里——"
            "这正是 04 压缩要保的东西。"
        )
    finally:
        restore_data(saved)


# ---------------------------------------------------------------- 第 11 段：长程任务


def _tool_step(cid: str, name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_call_delta",
            "index": 0,
            "id": cid,
            "name": name,
            "args_delta": json.dumps(args, ensure_ascii=False),
        }
    ]


def _text_step(text: str) -> list[dict[str, Any]]:
    return [{"type": "text_delta", "text": text}]


# 长程任务脚本（五轮 19 步，离线确定性）：T1 领任务+先干起来 → T2 纠偏先查规则
# → T3 规则路线（马克杯报备被拒）→ T4 换单（雨伞按实物调）→ T5 收尾（帆布包
# 不动、围巾挂起）。幕 3 / 6 / 8 的轨迹解法（branch / compact / resume）归 04。
LONG_TASK_SCRIPT = [
    _tool_step("c01", "list_tasks", {}),
    _tool_step("c02", "get_task", {"task_id": "T-101"}),
    _tool_step("c03", "query_inventory", {"category": "保温杯"}),
    _tool_step("c04", "update_inventory", {"category": "保温杯", "stock": 50}),
    _tool_step("c05", "search_rules", {"query": "补货"}),
    _tool_step("c06", "query_inventory", {"category": "玻璃杯"}),
    _tool_step("c07", "update_inventory", {"category": "玻璃杯", "stock": 20}),
    _tool_step("c08", "query_inventory", {"category": "马克杯"}),
    _tool_step("c09", "update_inventory", {"category": "马克杯", "stock": 60}),
    _tool_step("c10", "query_inventory", {"category": "保温壶"}),
    _tool_step("c11", "update_inventory", {"category": "保温壶", "stock": 20}),
    _text_step(
        "补货核查处理完：保温杯、玻璃杯、保温壶已按目标补足；"
        "马克杯需补 52 件超过 50 件上限，报备未获批准，未补。"
    ),
    _tool_step("c12", "get_task", {"task_id": "T-102"}),
    _tool_step("c13", "query_inventory", {"category": "雨伞"}),
    _tool_step("c14", "update_inventory", {"category": "雨伞", "stock": 13}),
    _text_step("雨伞系统 15 件、实物 13 件，差 2 件在 3 件以内，已按实物调整库存。"),
    _tool_step("c15", "query_inventory", {"category": "帆布包"}),
    _tool_step("c16", "query_inventory", {"category": "围巾"}),
    _text_step(
        "帆布包账实相符，不用动；围巾系统 8 件、实物 2 件，差 6 件超过 3 件，"
        "按规则挂起等人工复盘。今天的任务处理完毕。"
    ),
]

LONG_TASK_TURNS = [
    "今天仓库的补货核查和盘点差异，你处理一下。",
    "等等——先查补货规则，按规矩来。",
    "继续",
    "继续，处理盘点差异那张单。",
    "把剩下的处理完。",
]


async def case_long_task(workdir: Path) -> None:
    """长程任务端到端（离线）：工具链叠出长会话、审批判量触发、上下文只增不减。"""
    banner(
        "11-long-task",
        "长程任务：补货核查与盘点差异（离线）",
        "五轮 19 步跑完整张任务单（ScriptedLLM，确定性）。看三样：工具链怎么"
        "一轮轮叠出长会话；审批判量在真实流程里怎么触发（马克杯报备被拒）；"
        "以及无轨迹时代的痛——上下文只增不减。",
    )
    saved = use_data_copies(workdir)
    try:
        h = Harness(workdir, LONG_TASK_SCRIPT)
        approvals: list[Event] = []

        async def on_required(event: Event) -> None:
            approvals.append(event)
            p = event.payload
            line(
                "系统",
                ORANGE,
                f"？ {p['name']} 要执行：{brief(p['arguments'])}"
                f"（request_id={p['request_id']}，超时 {p['timeout']:g}s）",
            )
            await asyncio.sleep(0.05)  # 店长看了一眼
            line("用户", YELLOW, "→ 拒绝（数量过大，本次不补）")
            h.bus.publish(
                Event(
                    "user_approval",
                    h.sid,
                    {
                        "request_id": p["request_id"],
                        "approve": False,
                        "reason": "数量过大，本次不补",
                    },
                ),
                to=h.agent.agent_id,
            )

        h.bus.subscribe(Subscription("d.required", ("approval_required",), on_required))
        for text in LONG_TASK_TURNS:
            line("用户", YELLOW, text)
            h.send(text)
            await h.wait_turn()

        counts = [len(ctx) for ctx in h.llm.contexts]
        monotonic = all(b >= a for a, b in zip(counts, counts[1:]))
        line(
            "实测",
            GREEN,
            f"{len(LONG_TASK_TURNS)} 轮 {h.llm.calls} 步跑完；每步的上下文消息数 "
            f"{counts[0]}→{counts[-1]}，单调只增不减：{monotonic}",
        )
        decided = [e for e in h.events if e.type == "approval_decided"]
        line(
            "实测",
            GREEN,
            f"审批恰好一次：approval_required ×{len(approvals)}，decided ×{len(decided)}"
            f"（action={decided[0].payload.get('action') if decided else '-'}）",
        )
        line("系统", ORANGE, "任务单跑完后的库存副本：")
        for ln in tools_mod._INVENTORY.read_text(encoding="utf-8").splitlines():
            line("  ", GREY, ln)
        await h.stop()
        note(
            "这就是 04 要接手的现场：纠偏只能往前追加，试错的几轮永远留在上下文里；"
            "消息数只增不减压不下；进程一换全丢。同一张任务单的另一种命运，"
            "轨迹解法见 04 的增量段。"
        )
    finally:
        restore_data(saved)


# ---------------------------------------------------------------- 第 12 段：真模型长任务


async def case_live_task(workdir: Path) -> None:
    """真模型跑缩减版长任务：T-101 整单，审批由应答通道扮演店长拒绝。"""
    banner(
        "12-live-task",
        "真模型跑缩减版长任务",
        "真模型 + 真工具，只跑补货核查那张单（T-101）。马克杯的报备由应答通道"
        "扮演店长拒绝。不断言行为序——模型自己决定先查什么后查什么，"
        "只看任务真的能跑完、审批真的进账。",
    )
    try:
        llm = RealLLM()
    except RuntimeError as exc:
        line("系统", ORANGE, f"跳过真模型段：{exc}")
        return

    saved = use_data_copies(workdir)
    try:
        h = Harness(workdir, live=True)

        async def on_required(event: Event) -> None:
            p = event.payload
            line(
                "系统",
                ORANGE,
                f"？ {p['name']} 要执行：{brief(p['arguments'])}"
                f"（request_id={p['request_id']}，超时 {p['timeout']:g}s）",
            )
            line("用户", YELLOW, "→ 拒绝（数量过大，本次不补）")
            h.bus.publish(
                Event(
                    "user_approval",
                    h.sid,
                    {
                        "request_id": p["request_id"],
                        "approve": False,
                        "reason": "数量过大，本次不补",
                    },
                ),
                to=h.agent.agent_id,
            )

        async def ui_tool(event: Event) -> None:
            line("工具", GREEN, f"← {event.payload['name']} 结果：{brief(event.payload['result'])}")

        h.bus.subscribe(Subscription("d.required", ("approval_required",), on_required))
        h.bus.subscribe(Subscription("d.tool", ("tool_result",), ui_tool, mode=OBSERVE))

        async def on_decided(event: Event) -> None:
            p = event.payload
            line("系统", ORANGE, f"确认结果：{p['action']} by {p['by']}")

        h.bus.subscribe(Subscription("d.decided", ("approval_decided",), on_decided))
        line("用户", YELLOW, "处理一下补货核查任务单（T-101）")
        h.send("处理一下补货核查任务单（T-101）")
        for _ in range(4):  # 每轮最多 4 步，续几句“继续”让单子跑完
            await asyncio.wait_for(h.ended.wait(), 180.0)
            h.ended.clear()
            await h.bus.drain(timeout=30.0)
            line("用户", YELLOW, "继续")
            h.send("继续")
        await h.stop()
        line("系统", ORANGE, "库存副本终态：")
        for ln in tools_mod._INVENTORY.read_text(encoding="utf-8").splitlines():
            line("  ", GREY, ln)
        note(
            "真模型的任务单实测：能不能按单逐项、报备被拒不重试，"
            "取决于模型本身；轨迹与审批的账目是断言的基准，行为序不是。"
        )
    finally:
        restore_data(saved)


# ---------------------------------------------------------------- 第 13 段：长任务的轨迹解法


# 同一张任务单（第 11 段的命运），换轨迹跑法。幕与轨迹特性：
# 幕 1-2 领任务、先干起来（保温杯 50 落库 = 副作用）；幕 3 纠偏 = rewind + 遗言；
# 幕 4-5 规则路线 + 盘点差异（雨伞调 13）；幕 6 compact_request 边界压缩；
# 幕 7 摘要刻意遗漏雨伞 → 模型重查一次（压缩的代价）；幕 8 崩一刀 → resume。
LONG_TASK_TRAJ_SCRIPT = [
    _tool_step("c01", "list_tasks", {}),
    _tool_step("c02", "get_task", {"task_id": "T-101"}),
    _tool_step("c03", "query_inventory", {"category": "保温杯"}),
    _tool_step("c04", "update_inventory", {"category": "保温杯", "stock": 50}),
    _tool_step("c05", "search_rules", {"query": "补货"}),
    _tool_step("c06", "get_task", {"task_id": "T-101"}),
    _tool_step("c07", "query_inventory", {"category": "保温杯"}),
    _tool_step("c08", "query_inventory", {"category": "玻璃杯"}),
    _tool_step("c09", "update_inventory", {"category": "玻璃杯", "stock": 20}),
    _tool_step("c10", "query_inventory", {"category": "马克杯"}),
    _tool_step("c11", "update_inventory", {"category": "马克杯", "stock": 60}),
    _tool_step("c12", "query_inventory", {"category": "保温壶"}),
    _tool_step("c13", "update_inventory", {"category": "保温壶", "stock": 20}),
    _tool_step("c14", "get_task", {"task_id": "T-102"}),
    _tool_step("c15", "query_inventory", {"category": "雨伞"}),
    _tool_step("c16", "update_inventory", {"category": "雨伞", "stock": 13}),
    _text_step(
        "T-101 处理完：保温杯确认已补到 50 无需重做，玻璃杯补到 20，"
        "马克杯报备被拒未补，保温壶补到 20。"
    ),
    _tool_step("c18", "get_task", {"task_id": "T-102"}),
    _tool_step("c19", "query_inventory", {"category": "雨伞"}),
    _tool_step("c20", "query_inventory", {"category": "帆布包"}),
    _tool_step("c21", "query_inventory", {"category": "围巾"}),
]

# resume 后的新 agent 接着跑：重查围巾（悬挂修复后模型自己决定重调）→ 收尾汇报
LONG_TASK_TRAJ_AFTER = [
    _tool_step("c22", "query_inventory", {"category": "围巾"}),
    _text_step(
        "T-102 核对完：雨伞已是实物数 13，无需调整；帆布包 30 与实物一致，不动；"
        "围巾系统 8 件、实物 2 件，差 6 件超过 3 件，按规则挂起等人工复盘。"
        "两张单处理完毕。"
    ),
]

# 手动压缩的摘要剧本：摘要该保住的四样（工具结论 / 规则 / 副作用 / 待办）。
# 刻意遗漏：雨伞的“已处理”不提，保留窗里也没有——模型对不上账，重查一次。
# 幕 3 遗言里的保温杯副作用账本被吞进来——摘要吞摘要第一次自然发生。
COMPACT_SUMMARY = (
    "【已完成】T-101 补货核查处理完：保温杯此前已直接补到 50（副作用，已确认无需重做）；"
    "玻璃杯补到 20；马克杯需补 52 件超上限、报备被店长拒绝、未补；保温壶补到 20。"
    "【关键规则】补货：单品超 50 件需先报备店长审批；"
    "盘点差异：差 3 件以内按实物调整，超过挂起等人工复盘。"
    "【副作用】上述库存写操作均已落库，勿重复执行。"
    "【待办】T-102 盘点差异单处理中。"
)


TRAJ_TURNS = [
    "今天仓库的补货核查和盘点差异，你处理一下。",   # T1
    "按规矩重来：先查补货规则，再逐项处理两张单。",   # T2（rewind 后新分支）
    "继续",                                     # T3
    "继续",                                     # T4
    "继续",                                     # T5
    "继续，把盘点差异核对完。",                     # T6（边界先压缩）
    "继续，汇报核对结果。",                         # T7（resume 后）
]


async def case_long_task_trajectory(workdir: Path) -> None:
    """长任务的轨迹解法（离线）：rewind + 遗言、边界压缩、resume 恢复压缩视图。"""
    banner(
        "13-long-task-trajectory",
        "长任务的轨迹解法：rewind、压缩、恢复（离线）",
        "同一张任务单（第 11 段的命运），换轨迹跑法：纠偏 = rewind + 遗言；"
        "上下文长 = compact_request 在 step 边界换视图；崩一刀 = resume 恢复"
        "压缩视图、悬挂调用补占位。压缩只追加视图标记，原文一个字节不删。",
    )
    saved = use_data_copies(workdir)
    try:
        log = EventLog(str(workdir / "events"))
        bus = EventBus(log)
        store = SessionStore(workdir / "sessions")
        summarizer = ScriptedSummarizer([COMPACT_SUMMARY])
        system_prompt = "你是一个通过工具干活的通用 agent。"
        traj = store.start(cwd=str(workdir), model="fake-model", system_prompt=system_prompt)
        agent = Agent(
            bus,
            ScriptedLLM(LONG_TASK_TRAJ_SCRIPT),
            store=store,
            summarizer=summarizer,
            system_prompt=system_prompt,
        )
        agent.attach(traj)
        ended = asyncio.Event()

        async def on_end(event: Event) -> None:
            ended.set()

        async def ui_tool(event: Event) -> None:
            line("工具", GREEN, f"← {event.payload['name']} 结果：{brief(event.payload['result'])}")

        async def on_required(event: Event) -> None:
            p = event.payload
            line(
                "系统",
                ORANGE,
                f"？ {p['name']} 要执行：{brief(p['arguments'])}（request_id={p['request_id']}）",
            )
            line("用户", YELLOW, "→ 拒绝（数量过大，本次不补）")
            bus.publish(
                Event(
                    "user_approval",
                    traj.sid,
                    {
                        "request_id": p["request_id"],
                        "approve": False,
                        "reason": "数量过大，本次不补",
                    },
                ),
                to=agent.agent_id,
            )

        bus.subscribe(Subscription("end", ("turn_end",), on_end, mode=OBSERVE))
        bus.subscribe(Subscription("tool", ("tool_result",), ui_tool, mode=OBSERVE))
        bus.subscribe(Subscription("req", ("approval_required",), on_required))

        async def send_and_wait(text: str) -> None:
            line("用户", YELLOW, text)
            bus.publish(Event("user_input", traj.sid, {"text": text}), to=agent.agent_id)
            await asyncio.wait_for(ended.wait(), TIMEOUT)
            ended.clear()
            await bus.drain(timeout=TIMEOUT)

        # —— 幕 1-2：领任务、先干起来（保温杯 50 落库）——
        await send_and_wait(TRAJ_TURNS[0])

        # —— 幕 3：纠偏 = rewind + 遗言 ——
        line("用户", YELLOW, "停——你怎么不看补货规则就动手？回到最开始重来（rewind + 遗言）")
        user1 = next(
            e
            for e in traj.entries()
            if e.type == MESSAGE and e.payload["message"]["role"] == "user"
        )
        path_before = traj.path()
        abandoned = path_before[path_before.index(user1) + 1 :]
        node = traj.branch_with_summary(
            user1.id,
            "试过直接补货：保温杯已从 3 补到 50（副作用已发生，勿重复处理）；"
            "未查补货规则，被用户叫停。",
        )
        line(
            "实测",
            GREEN,
            f"branch_with_summary：移指针 + 追加遗言节点 {node.id}（挂在 {user1.id} 下）；"
            f"抛弃 {len(abandoned)} 条 entry（{abandoned[0].id}..{abandoned[-1].id}），"
            f"原样躺在文件里——副作用（保温杯 50）在遗言里带账",
        )

        # —— 幕 4-5：规则路线跑到 T-102 雨伞（T4 步）——
        for text in TRAJ_TURNS[1:5]:
            await send_and_wait(text)

        # —— 幕 6：compact_request → step 边界压缩 ——
        line("用户", YELLOW, "上下文有点长了，压一下（compact_request，下一轮边界生效）")
        p_before = len(build_context(traj).messages)
        bus.publish(Event("compact_request", traj.sid, {"reason": "manual"}), to=agent.agent_id)
        await send_and_wait(TRAJ_TURNS[5])
        p_after = len(build_context(traj).messages)
        comp = next(e for e in traj.entries() if e.type == COMPACTION)
        line(
            "实测",
            GREEN,
            f"边界压缩：投影 {p_before} → {p_after} 条消息；"
            f"compaction entry {comp.id}（keep_from={comp.payload['keep_from_id']}，"
            f"reason={comp.payload['reason']}）",
        )
        line("系统", ORANGE, f"摘要全文：{comp.payload['summary']}")
        note(
            "摘要刻意漏了雨伞的“已处理”且不带待办清单——幕 7 里模型对不上账，"
            "重列任务单、重查雨伞（c19 那次 query 就是）：压缩的代价 = 冗余查询，"
            "收益 = 前四轮整段窗口。幕 3 遗言里的副作用账本被这一刀吞进来——"
            "摘要吞摘要第一次自然发生。摘要调用是裸 chat（不带工具、封顶），"
            "离线段由 ScriptedSummarizer 保确定性。"
        )

        # —— 幕 8：崩一刀 + resume ——
        await agent.stop()  # 进程收尾；崩溃本身不留痕——没写完的不算已发生
        traj.append(
            MESSAGE,
            message_payload(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_d1",
                            "type": "function",
                            "function": {
                                "name": "query_inventory",
                                "arguments": '{"category": "围巾"}',
                            },
                        }
                    ],
                }
            ),
        )
        line(
            "系统",
            ORANGE,
            "进程在工具执行前被杀：assistant 要了围巾的数据，结果永远没来（悬挂调用留在轨迹里）",
        )
        bytes_before = traj.log.raw_bytes()
        store2 = SessionStore(workdir / "sessions")
        traj2 = store2.resume(traj.sid, note="crash 恢复演练")
        agent2 = Agent(
            bus,
            ScriptedLLM(LONG_TASK_TRAJ_AFTER),
            store=store2,
            agent_id="agent-2",
            summarizer=summarizer,
            system_prompt=system_prompt,
        )
        agent2.attach(traj2)
        ended2 = asyncio.Event()

        async def on_end2(event: Event) -> None:
            ended2.set()

        bus.subscribe(Subscription("end2", ("turn_end",), on_end2, mode=OBSERVE))
        proj = build_context(traj2)
        n_unknown = sum(
            1
            for m in proj.messages
            if m.get("role") == "tool" and "UNKNOWN" in str(m.get("content", ""))
        )
        line(
            "实测",
            GREEN,
            f"resume 后投影 {len(proj.messages)} 条 = 压缩视图（[system, <摘要>, 保留窗…]），"
            f"不是全量原文；悬挂调用补了 {n_unknown} 条占位：{brief(UNKNOWN_TOOL_RESULT)}",
        )
        line(
            "实测",
            GREEN,
            f"append-only：resume 前字节是 resume 后的前缀：{traj2.log.raw_bytes().startswith(bytes_before)}",
        )
        await send_and_wait2(bus, traj2, agent2, ended2, TRAJ_TURNS[6])
        await agent2.stop()
        store2.close(traj2, reason="demo 结束")

        line("系统", ORANGE, "库存副本终态：")
        for ln in tools_mod._INVENTORY.read_text(encoding="utf-8").splitlines():
            line("  ", GREY, ln)
        total = len(traj2.entries())
        on_path = len(traj2.path())
        line(
            "统计",
            GREEN,
            f"全树 {total} 条 entry（被抛弃分支 {total - on_path} 条 + compaction 原文都在）；"
            f"当前路径 {on_path} 条；EventLog seq 1..{log.last_seq}",
        )
        line("系统", ORANGE, "轨迹树（尾部 10 条，分支与压缩节点都在树上）：")
        show_trajectory(traj2, tail=10)
        note(
            "这就是本章的答案：纠偏不删历史（分支 + 遗言），压缩不删历史"
            "（视图标记 + 摘要），崩溃不丢历史（resume + 投影收口）。"
            "自动触发（水位、折叠、毒丸防御）归 05。"
        )
    finally:
        restore_data(saved)


async def send_and_wait2(
    bus: EventBus, traj: Trajectory, agent: Agent, ended: asyncio.Event, text: str
) -> None:
    """resume 后新 agent 的发送：走同一台 bus，等新 agent 的 turn_end。"""
    line("用户", YELLOW, text)
    bus.publish(Event("user_input", traj.sid, {"text": text}), to=agent.agent_id)
    await asyncio.wait_for(ended.wait(), TIMEOUT)
    await bus.drain(timeout=TIMEOUT)


CASES = {
    "01-trajectory-shape": case_trajectory_shape,
    "02-projection": case_projection,
    "03-rewind": case_rewind,
    "04-recovery": case_recovery,
    "05-fork": case_fork,
    "06-live-turn": case_live_turn,
    "07-ui-buffer": case_ui_buffer,
    "08-approval": case_approval,
    "09-steering": case_steering,
    "10-tools": case_tools,
    "11-long-task": case_long_task,
    "12-live-task": case_live_task,
    "13-long-task-trajectory": case_long_task_trajectory,
}


async def main(case_ids: list[str] | None = None, sessions_dir: Path | None = None) -> None:
    root = sessions_dir or (Path(__file__).resolve().parents[2] / "sessions" / "stage04")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    picked = [c for c in CASE_ORDER if not case_ids or "all" in case_ids or c in case_ids]
    if not picked:
        print(f"{GREY}没有匹配的 case：{case_ids}；可选：{', '.join(CASE_IDS)}{RESET}")
        return

    print(f"{BOLD}能力升级 + 会话与真相 —— 03c / 04 配套演示{RESET}")
    if case_ids and "all" not in case_ids:
        print(f"{GREY}  （只跑：{', '.join(picked)}）{RESET}")

    for i, cid in enumerate(picked):
        workdir = root / cid.split("-", 1)[1]
        workdir.mkdir(parents=True, exist_ok=True)
        await CASES[cid](workdir)
        if i < len(picked) - 1:
            print()

    print(f"\n{BOLD}── 收尾 ──{RESET}")
    line("系统", ORANGE, "demo 结束")
    print(f"{GREY}  session log: {root}{RESET}")


def cli() -> None:
    """[project.scripts] 入口：stage04-demo。

        stage04-demo                      # 全部（默认；最后一段打真模型）
        stage04-demo 03-rewind            # 只跑指定段（离线段可当基准反复跑）
        stage04-demo --list               # 列 case 及其说明（不加载模型配置）
    """
    parser = argparse.ArgumentParser(
        prog="stage04-demo", description="能力升级 + 会话与真相演示（03c / 04 配套）。"
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
