"""Stage 04 轨迹层演示——与 04 章（trajectory）的六个 demo 一一对应。

需要仓库根 .env 里的 OPENAI_API_KEY / OPENAI_API_BASE / OPENAI_MODEL
（环境变量可覆盖）。六段全部打真模型（RealLLM）；没配 key 的段整段跳过。
03b / 03c 的演示（UI 缓冲、审批、插话、工具层、长任务）不在这个包里。

六段（各自独立，跑哪个都行），按"写 → 恢复 → 压缩 → 回退 → 分叉 → 崩溃恢复"
的顺序把轨迹层的每个能力过一遍：

1. **轨迹长什么样**（写轨迹）：跑一轮对话，把落盘的 entry 原样打印出来——
   header 不是节点、认父不认子、一条 assistant 连 toolCall 是一个节点。
2. **从正常轨迹恢复**：跑完一轮关会话，当进程重启过——resume 重建树、
   attach 登记、投影逐字节还原，继续对话带全历史。
3. **触发压缩**：compact_request 在 step 边界追加 compaction entry
   （LiveSummarizer 生成摘要 + 刀口），投影换成 [system, <摘要>, 保留窗…]。
4. **压缩之后 rewind**：rewind 到压缩之前旧消息逐字回来（压缩是视图）；
   继续对话 = 开分支；branch 到 compaction 节点 = 回到压缩刚做完那一刻。
5. **session 切换（fork）**：fork 出一份新会话文件，两条轨迹分道扬镳，
   各自独立生长。
6. **从有问题的轨迹 resume**：主动构造两份不完整的轨迹——残尾（砍字节 →
   resume 停在完好处）、悬挂的工具调用（投影层补占位，原文件字节不变）。

行首标签沿用前几章：

    用户 │ 用户说了什么
    entry │ 落盘的 entry 原样呈现（id ← parentId、type、payload 原样 JSON）
    系统 │ 生命周期与统计（统一橙色）
    说明 │ 旁白，只解释这一段在演示什么（灰色缩进）

    python -m baby_event_driven_agent.stages.stage04_trajectory
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any

from . import tools as tools_mod
from .agent import Agent, build_context
from .llm import RealLLM
from .transport.bus import EventBus
from .transport.events import Event, Subscription, OBSERVE
from .transport.persistence import EventLog
from .session.compaction import Summarizer, maybe_compact
from .session.store import SessionStore, session_facts
from .session.trajectory import (
    COMPACTION,
    MESSAGE,
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

CASE_ORDER = (
    "01-trajectory-shape",
    "02-resume-normal",
    "03-compact",
    "04-rewind-after-compact",
    "05-fork",
    "06-resume-broken",
)
CASE_TITLES = {
    "01-trajectory-shape": "demo 1：写轨迹——写好的 entry 长什么样",
    "02-resume-normal": "demo 2：从正常轨迹恢复——resume + attach + 投影重建",
    "03-compact": "demo 3：触发压缩——compaction entry + 投影换视图",
    "04-rewind-after-compact": "demo 4：压缩之后 rewind，然后继续对话",
    "05-fork": "demo 5：session 切换——fork",
    "06-resume-broken": "demo 6：从有问题的轨迹 resume——残尾、悬挂调用",
}
ALL_TITLE = "六段全跑（全部真模型）"
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

    全部真模型（RealLLM，配置读仓库根 .env）。isolated=True 时写操作落
    工作目录副本，包自带 data/ 一个字节不动（真模型自主决策，可能写）。
    """

    def __init__(
        self,
        workdir: Path,
        *,
        summarizer: Summarizer | None = None,
        keep_turns: int = 2,
        isolated: bool = False,
    ) -> None:
        self.log = EventLog(str(workdir / "events"))
        self.bus = EventBus(self.log)
        self.store = SessionStore(workdir / "sessions")
        self.system_prompt = "你是一个通过工具干活的通用 agent。"
        self.llm = RealLLM()
        self.timeout = 180.0  # 真模型段放宽等待
        self.agent = Agent(
            self.bus,
            self.llm,
            store=self.store,
            system_prompt=self.system_prompt,
            summarizer=summarizer,
            keep_turns=keep_turns,
        )
        # 数据隔离：写操作落工作目录副本，包自带 data/ 一个字节不动（真模型自主决策，可能写）
        self._saved_data: dict[str, Path] | None = use_data_copies(workdir) if isolated else None
        # 轨迹里的 model_change 记真实模型名
        self.traj = self.store.start(
            cwd=str(workdir), model=str(getattr(self.llm, "model", "")), system_prompt=self.system_prompt
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
        await asyncio.wait_for(self.ended.wait(), self.timeout)
        self.ended.clear()
        await self.bus.drain(timeout=self.timeout)

    async def stop(self) -> None:
        await self.agent.stop()
        await self.bus.drain(timeout=self.timeout)
        await self.bus.close()
        if self._saved_data is not None:
            restore_data(self._saved_data)
            self._saved_data = None


def show_trajectory(traj: Trajectory, *, tail: int = 0) -> None:
    """把轨迹打印成链：id ← parentId，type + 消息摘要。"""
    entries = traj.entries()
    for e in entries[-tail:] if tail else entries:
        # 原样呈现落盘的真实 entry（与文件记录同构，少了长度前缀与 CRC）：
        # 五件套固定顺序，payload 按 type 原样 JSON，不做任何美化
        parent = e.parent_id or "∅"
        payload = json.dumps(e.payload, ensure_ascii=False)
        line("entry", GREY, f"{e.id} ← {parent}  {e.type:<16} {payload}")


# ---------------------------------------------------------------- demo 1：写轨迹


async def case_trajectory_shape(workdir: Path) -> None:
    banner(
        "01-trajectory-shape",
        "轨迹长什么样",
        "真模型跑一轮对话。看落盘的 entry 树：header 不是节点；每行只带 parentId"
        "（认父不认子）；一条 assistant 连 toolCall 是一个节点。",
    )
    try:
        h = Harness(workdir, isolated=True)
    except RuntimeError as exc:
        line("系统", ORANGE, f"跳过真模型段：{exc}")
        return
    line("用户", YELLOW, "保温杯还有库存吗")
    h.send("保温杯还有库存吗")
    await h.wait_turn()
    await h.stop()

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
    note(
        "这一轮的 agent loop：用户问库存 → LLM 发起工具调用 → agent 执行工具 → "
        "LLM 拿到结果回复，四步各记一条 message entry；加上开头的 "
        "session_started（会话开始）与 model_change（模型选择），"
        f"就是上面的 {len(entries)} 条 entry。"
    )
    line("系统", ORANGE, f"文件头原文：{json.dumps(header, ensure_ascii=False)}")
    note(
        "header 是文件的第一行（type=session）：sid、工作目录、创建时间、"
        "system_prompt 原文——回答“这个会话是谁、用什么开的”。"
        "它不是树节点，不占 entry、不进投影，审计/回放时才用。"
    )
    if h.traj.branch_points():
        line("实测", RED, "发现了分叉点（不该有）")
    else:
        line("实测", GREEN, "分叉点：无（纯链的轨迹，树是按需要准备的）")
    note(
        "认父不认子：每行只有 parentId，文件里没有一个字提到孩子。想找分叉要靠"
        "按 parentId 反查——这张 log 里一次 rewind 都没有，轨迹就是一条链。"
    )


# ---------------------------------------------------------------- demo 2：从正常轨迹恢复


async def case_resume_normal(workdir: Path) -> None:
    """resume + attach + 投影重建：内存丢了，对话丢不了。"""
    banner(
        "02-resume-normal",
        "从正常轨迹恢复：resume + attach + 投影重建（真模型）",
        "跑完一轮、关掉会话，然后当进程重启过：新 store / 新 agent，手里只有 sid。"
        "resume 读文件重建树，attach 登记会话，上下文从轨迹投影现算——"
        "不依赖任何内存状态。",
    )
    try:
        h = Harness(workdir, isolated=True)
    except RuntimeError as exc:
        line("系统", ORANGE, f"跳过真模型段：{exc}")
        return
    line("用户", YELLOW, "保温杯还有库存吗")
    h.send("保温杯还有库存吗")
    await h.wait_turn()

    # 第一幕：重演 demo 1 的同一轮对话——轨迹应与 demo 1 同构
    n_entries = len(h.traj.entries())
    line("系统", ORANGE, "写下的轨迹（应与 demo 1 的同一轮对话同构）：")
    show_trajectory(h.traj)
    line(
        "实测",
        GREEN,
        f"{n_entries} 条 entry + 1 条 header——和 demo 1 的那张轨迹对得上",
    )

    sid = h.sid
    h.store.close(h.traj, reason="第一段对话结束")
    before = build_context(h.traj)
    line(
        "实测",
        GREEN,
        f"关闭时：投影 {len(before.messages)} 条消息；进程到此结束，内存里什么都可以扔了",
    )

    # —— 进程重启：新 store / 新 agent，手里只有 sid ——
    store2 = SessionStore(workdir / "sessions")
    traj2 = store2.resume(sid, note="进程重启后恢复")
    agent2 = Agent(h.bus, h.llm, agent_id="agent-2", store=store2, system_prompt=h.system_prompt)
    agent2.attach(traj2)
    after = build_context(traj2)
    line(
        "实测",
        GREEN,
        f"resume 重建树：{len(traj2.entries())} 条 entry（原 {n_entries} 条 + resumed 留痕）；"
        f"投影与关闭前逐字节相同："
        f"{json.dumps(after.messages, ensure_ascii=False) == json.dumps(before.messages, ensure_ascii=False)}",
    )
    line("系统", ORANGE, "重建出的轨迹（尾部 2 条：原轨迹末尾 + resumed 留痕）：")
    show_trajectory(traj2, tail=2)
    line(
        "实测",
        GREEN,
        f"生命周期留痕：{json.dumps(session_facts(traj2)[-1], ensure_ascii=False)}",
    )

    # —— 继续对话：模型带着恢复的历史接着答 ——
    ended2 = asyncio.Event()

    async def on_end2(event: Event) -> None:
        ended2.set()

    h.bus.subscribe(Subscription("end2", ("turn_end",), on_end2, mode=OBSERVE))
    line("用户", YELLOW, "刚才查的是哪个品类？")
    h.bus.publish(
        Event("user_input", traj2.sid, {"text": "刚才查的是哪个品类？"}), to=agent2.agent_id
    )
    await asyncio.wait_for(ended2.wait(), h.timeout)
    await h.bus.drain(timeout=h.timeout)
    await agent2.stop()
    ctx = build_context(traj2)
    reply = ctx.messages[-1]
    line(
        "实测",
        GREEN,
        f"继续对话：上下文 {len(ctx.messages)} 条（system + 恢复的历史 + 新一轮），"
        f"回答：{brief(reply.get('content'))}",
    )
    await h.stop()
    note(
        "恢复没有秘密：读文件重建树 + attach 登记。上下文不是从内存拿的——"
        "每次 LLM 调用前从轨迹投影现算，同一份文件谁来做投影结果都一样。"
    )


# ---------------------------------------------------------------- demo 3：触发压缩


async def case_compact(workdir: Path) -> None:
    """compact_request → step 边界压缩：compaction entry + 投影换视图。"""
    banner(
        "03-compact",
        "触发压缩：compaction entry + 投影换视图（真模型）",
        "真模型跑三轮把上下文堆长，然后 compact_request——下一个 step 边界追加一个 "
        "compaction entry（摘要由 LiveSummarizer 裸 chat 生成 + 刀口 keep_from_id），"
        "原文一个字节不删；之后的投影自动变成 [system, <摘要>, 保留窗…]。",
    )
    try:
        h = Harness(workdir, keep_turns=1, isolated=True)
    except RuntimeError as exc:
        line("系统", ORANGE, f"跳过真模型段：{exc}")
        return
    for text in ("保温杯还有库存吗", "玻璃杯呢", "帮我汇总一下"):
        line("用户", YELLOW, text)
        h.send(text)
        await h.wait_turn()

    p_before = len(build_context(h.traj).messages)
    line("用户", YELLOW, "上下文有点长了，压一下（compact_request，下一个边界生效）")
    h.bus.publish(Event("compact_request", h.sid, {"reason": "manual"}), to=h.agent.agent_id)
    line("用户", YELLOW, "继续")
    h.send("继续")
    await h.wait_turn()

    p_after = len(build_context(h.traj).messages)
    comp = next(e for e in h.traj.entries() if e.type == COMPACTION)
    line(
        "实测",
        GREEN,
        f"边界压缩：投影 {p_before} → {p_after} 条消息；compaction entry {comp.id}"
        f"（keep_from={comp.payload['keep_from_id']}，reason={comp.payload['reason']}）——"
        f"被压的原文一个字节没动：全树 {len(h.traj.entries())} 条 entry 都在",
    )
    line("系统", ORANGE, f"摘要全文：{comp.payload['summary']}")
    line("entry", GREY, json.dumps(comp.to_dict(), ensure_ascii=False))
    line("系统", ORANGE, "被压进摘要的消息（keep_from 之前，原文仍在轨迹里）：")
    for m in Agent._prefix_view(h.traj, str(comp.payload["keep_from_id"])):
        calls = m.get("tool_calls")
        extra = f" → toolCall({', '.join(c['function']['name'] for c in calls)})" if calls else ""
        line("  ", GREY, f"{m['role']:<9} {brief(m.get('content') or '')}{extra}")
    line("系统", ORANGE, "压缩后的投影（模型实际看到的）：")
    for m in build_context(h.traj).messages:
        line("  ", GREY, f"{m['role']:<9} {brief(m.get('content') or '')}")
    await h.stop()
    note(
        "压缩只追加视图标记：compaction 的 payload = summary + keep_from_id"
        "（从哪条起原样保留）。投影遇到它：刀口之前跳过、摘要插在 system 之后、"
        "只认当前路径上第一条（折叠语义归 05）。"
    )


# ---------------------------------------------------------------- demo 4：压缩之后 rewind


async def case_rewind_after_compact(workdir: Path) -> None:
    """压缩过的轨迹上 rewind：两个落点，文件一个字节不动。"""
    banner(
        "04-rewind-after-compact",
        "压缩之后 rewind，然后继续对话（真模型）",
        "真模型跑三轮、边界压缩后，rewind 到压缩之前的 entry：compaction 不在路径上 = "
        "压缩没发生过，旧消息逐字回来（压缩是视图，不是对数据的手术）。"
        "之后继续对话 = 开分支；也可以 branch 到 compaction 节点本身——"
        "回到压缩刚做完那一刻的视图。",
    )
    try:
        h = Harness(workdir, keep_turns=1, isolated=True)
    except RuntimeError as exc:
        line("系统", ORANGE, f"跳过真模型段：{exc}")
        return
    for text in ("开始处理 T-101，先定个方案", "继续", "继续"):
        line("用户", YELLOW, text)
        h.send(text)
        await h.wait_turn()

    # —— 边界压缩（触发机制见 demo 3，这里直接在边界做一刀，聚焦 rewind）——
    comp = await maybe_compact(
        h.traj,
        h.agent._summarizer_for(),
        keep_turns=1,
        reason="manual",
        prefix_view=Agent._prefix_view,
    )
    assert comp is not None
    line(
        "实测",
        GREEN,
        f"压缩完成：compaction entry {comp.id}（keep_from={comp.payload['keep_from_id']}）；"
        f"投影 {len(build_context(h.traj).messages)} 条 = [system, <摘要>, 保留窗…]",
    )

    # —— 落点一：rewind 到压缩发生前的 entry ——
    first_user = next(
        e
        for e in h.traj.entries()
        if e.type == MESSAGE and e.payload["message"]["role"] == "user"
    )
    before_bytes = h.traj.log.raw_bytes()
    h.traj.branch(first_user.id)
    p1 = build_context(h.traj)
    line(
        "实测",
        GREEN,
        f"落点一 branch({first_user.id})：文件字节未变："
        f"{h.traj.log.raw_bytes() == before_bytes}；投影 {len(p1.messages)} 条 = "
        "[system, 那条 user]——compaction 不在路径上，旧消息逐字回来",
    )
    for m in p1.messages:
        line("  ", GREY, f"{m['role']:<9} {brief(m.get('content') or '')}")

    # —— 继续对话 = 开分支 ——
    line("用户", YELLOW, "换个思路重来：先查规则再动手")
    h.send("换个思路重来：先查规则再动手")
    await h.wait_turn()
    dup = h.traj.branch_points()
    line(
        "实测",
        GREEN,
        f"回退后继续对话 = 分支：{first_user.id} 现在有两个孩子 {dup.get(first_user.id)}",
    )

    # —— 落点二：branch 到 compaction 节点本身 ——
    h.traj.branch(comp.id)
    p2 = build_context(h.traj)
    line(
        "实测",
        GREEN,
        f"落点二 branch({comp.id})：投影 {len(p2.messages)} 条 = 回到压缩刚做完那一刻："
        "[system, <摘要>, 保留窗…]",
    )
    for m in p2.messages:
        line("  ", GREY, f"{m['role']:<9} {brief(m.get('content') or '')}")
    await h.stop()
    note(
        "两个落点，文件都一个字节没动；差别只在 leaf 指针走到哪、投影因此算出什么。"
        "被抛弃的分支（含压缩前的那段）原样躺在文件里，随时能 branch 回去。"
    )


# ---------------------------------------------------------------- demo 5：fork


async def case_fork(workdir: Path) -> None:
    banner(
        "05-fork",
        "session 切换：fork",
        "真模型跑一轮后，把当前路径克隆进一份新会话文件（id 与 parentId 原样保留）。"
        "新文件是完整合法的轨迹，可以独立继续生长；旧文件原封不动。",
    )
    try:
        h = Harness(workdir, isolated=True)
    except RuntimeError as exc:
        line("系统", ORANGE, f"跳过真模型段：{exc}")
        return
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


# ---------------------------------------------------------------- demo 6：从有问题的轨迹 resume


async def case_resume_broken(workdir: Path) -> None:
    """主动构造不完整的轨迹：每个现场先摆坏轨迹原文，再摆修复结果。"""
    banner(
        "06-resume-broken",
        "异常恢复：残尾、悬挂调用",
        "主动构造两份不完整的轨迹，各自先看坏成什么样、再看修成什么样："
        "崩在写一半的字节（CRC 判定，resume 裁掉残尾续写）；"
        "崩在工具执行前的语义残尾（投影层补占位，原文件字节不变）。",
    )
    try:
        h1 = Harness(workdir / "torn", isolated=True)
    except RuntimeError as exc:
        line("系统", ORANGE, f"跳过真模型段：{exc}")
        return

    # —— 现场一：字节级残尾 ——
    h1.send("保温杯还有库存吗")
    await h1.wait_turn()
    await h1.stop()
    path1 = h1.store.path_of(h1.sid)
    intact = len(TrajectoryLog(path1).read()[1])
    raw = path1.read_bytes()
    # 主动构造：崩在写一半——不只砍掉信封尾巴，直接断进 message 正文中间
    syn = raw.rfind(b'"synthetic"')  # 信封字段在正文之后，从它往前再砍 20 字节
    cut = len(raw) - syn + 20
    path1.write_bytes(raw[:-cut])
    torn_size = path1.stat().st_size
    broken = Trajectory.load(TrajectoryLog(path1))
    torn_raw = path1.read_bytes()
    torn_line = torn_raw[torn_raw.rfind(b"\n") + 1 :].decode("utf-8", errors="replace")
    parts = torn_line.split(" ", 2)
    declared, actual = int(parts[0], 16), len(parts[2].encode("utf-8"))
    tid = re.search(r'"id": "([0-9a-f]+)"', parts[2])
    tparent = re.search(r'"parentId": "([0-9a-f]+)"', parts[2])
    ttype = re.search(r'"type": "(\w+)"', parts[2])
    line(
        "实测",
        RED,
        f"坏轨迹：第 6 条记录断在 message 正文中间——行格式和完好记录一模一样（长度 CRC json），"
        f"但头部自报 {declared} 字节、实际只剩 {actual} 字节（差 {declared - actual}），CRC 对不上 → 整条拒收：",
    )
    line(
        "残尾",
        RED,
        f"{tid.group(1) if tid else '?'} ← {tparent.group(1) if tparent else '?'}  "
        f"{(ttype.group(1) if ttype else '?'):<16} {brief(parts[2], 100)}",
    )
    show_trajectory(broken)
    line(
        "实测",
        RED,
        f"完好 {intact} 条 → 只读到 {len(broken.entries())} 条（停在坏记录之前，torn={broken.torn}）。"
        f"残尾声明的父节点就是上面最后一条 entry——它是没出生的第 6 条，没写完的不算已发生",
    )
    resumed = h1.store.resume(h1.sid, note="crash 恢复演练")
    line(
        "实测",
        GREEN,
        f"修好之后：resume 先把残尾字节物理裁掉（文件 {torn_size} → {len(resumed.log.raw_bytes())} 字节），"
        "补 session_resumed 留痕 + 主动补一条 assistant 占位进轨迹，轨迹尾部三条：",
    )
    show_trajectory(resumed, tail=3)
    line(
        "实测",
        GREEN,
        f"resumed 事件留痕 torn_tail={session_facts(resumed)[-1]['payload']['torn_tail']}",
    )
    fixed1 = build_context(resumed)
    line("实测", GREEN, "修好之后的投影（模型实际看到的）——占位已在轨迹里，投影照常透传：")
    for m in fixed1.messages:
        line("  ", GREEN, f"{m['role']:<9} {brief(m.get('content') or '')}")

    # —— 现场二：悬挂的工具调用（崩在工具执行前） ——
    h2 = Harness(workdir / "dangling", isolated=True)
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
    # 主动构造：assistant 要了工具结果，结果永远没来
    line("实测", RED, "坏轨迹：末尾是 assistant 的 toolCall，底下没有 tool 回执：")
    show_trajectory(traj)
    before_bytes = traj.log.raw_bytes()
    fixed = build_context(traj)  # 补自描述占位；system prompt 从 header 提取
    line("实测", GREEN, "修好之后的投影（模型实际看到的）：")
    for m in fixed.messages:
        line("  ", GREEN, f"{m['role']:<9} {brief(m.get('content') or '')}")
    line(
        "实测",
        GREEN,
        f"占位只补在投影里，原文件字节未变：{traj.log.raw_bytes() == before_bytes}",
    )
    note(
        "两处的共同纪律：修复只作用于喂给模型的投影（语义级），"
        "轨迹文件一个字节不动——轨迹是唯一真相，坏的地方用视图补。"
    )
    await h2.stop()


CASES = {
    "01-trajectory-shape": case_trajectory_shape,
    "02-resume-normal": case_resume_normal,
    "03-compact": case_compact,
    "04-rewind-after-compact": case_rewind_after_compact,
    "05-fork": case_fork,
    "06-resume-broken": case_resume_broken,
}


async def main(case_ids: list[str] | None = None, sessions_dir: Path | None = None) -> None:
    root = sessions_dir or (Path(__file__).resolve().parents[2] / "sessions" / "stage04")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    picked = [c for c in CASE_ORDER if not case_ids or "all" in case_ids or c in case_ids]
    if not picked:
        print(f"{GREY}没有匹配的 case：{case_ids}；可选：{', '.join(CASE_IDS)}{RESET}")
        return

    print(f"{BOLD}Stage 04 轨迹层演示 —— 与 04 章的六个 demo 对应{RESET}")
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

        stage04-demo                      # 六段全跑（全部真模型）
        stage04-demo 03-compact           # 只跑指定段
        stage04-demo --list               # 列 case 及其说明（不加载模型配置）
    """
    parser = argparse.ArgumentParser(
        prog="stage04-demo", description="Stage 04 轨迹层演示（与 04 章六个 demo 对应）。"
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
