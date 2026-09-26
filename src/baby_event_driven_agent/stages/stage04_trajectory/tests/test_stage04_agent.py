"""Stage 04 agent 集成用例：全部离线（ScriptedLLM）。

要验的是"history 换成轨迹层之后，stage02-03 的行为一个没坏，且轨迹账目正确"：

- 一轮 turn 在轨迹里是合法序列：user → assistant(tool_calls) → tool → assistant
- 投影是每次 step 现算的（system 前置、历史完整、合成注脚被 strip）
- steering / 打断（纯停止与附新消息）落到轨迹上的是合法序列 + synthetic 留痕
- 治理否决的占位进轨迹；resume 换一个 agent 实例能接着聊（不重复事实）
- sid 由 store 分配：没 attach 过的 sid 直接报错（宁可炸也不静默开新历史）
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from baby_event_driven_agent.stages.stage04_trajectory.agent import (
    BLOCKED_PREFIX,
    INTERRUPTED,
    STOP_CLOSER,
    Agent,
)
from baby_event_driven_agent.stages.stage04_trajectory.transport.bus import EventBus
from baby_event_driven_agent.stages.stage04_trajectory.transport.events import (
    OBSERVE,
    Event,
    Subscription,
)
from baby_event_driven_agent.stages.stage04_trajectory.llm import TOOLS
from baby_event_driven_agent.stages.stage04_trajectory.transport.persistence import EventLog
from baby_event_driven_agent.stages.stage04_trajectory.session.store import SessionStore
from baby_event_driven_agent.stages.stage04_trajectory.transport.subscribers import permission_guard
from baby_event_driven_agent.stages.stage04_trajectory.agent import build_context

TIMEOUT = 10.0

CALL_INVENTORY = [
    {
        "type": "tool_call_delta",
        "index": 0,
        "id": "call_1",
        "name": "update_inventory",
        "args_delta": '{"category": "保温杯", "stock": 42}',
    }
]
FINAL_TEXT = [{"type": "text_delta", "text": "已记录。"}]
SECOND_TEXT = [{"type": "text_delta", "text": "第二轮的回答。"}]


class ScriptedLLM:
    """脚本化 LLM：记录每次调用收到的上下文（验投影用）。

    `hang_after=i`：第 i 个 chunk 吐完之后挂住（默认 30s）——把"中断落在
    stream 阶段"从竞速变成确定：chunk 已经消费（tool_call_started 已发、
    partial 已累积），流还没结束。取消必然打在挂起的那一觉上。
    """

    def __init__(
        self,
        script: list[list[dict[str, Any]]],
        *,
        hang_after: int | None = None,
        hang: float = 5.0,
    ) -> None:
        self.script = script
        self.calls = 0
        self.contexts: list[list[dict[str, Any]]] = []
        self.hang_after = hang_after
        self.hang = hang

    async def stream_chat(self, messages: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
        self.contexts.append([dict(m) for m in messages])
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for i, chunk in enumerate(self.script[idx]):
            yield chunk
            # 只挂第一次调用的第 i 块之后：redirect 之后的续走不再挂（否则
            # 第二步也在流里睡 30s，turn 永远收不了尾）
            if self.hang_after is not None and i == self.hang_after and self.calls == 1:
                await asyncio.sleep(self.hang)


@pytest.fixture()
def workdir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stage04_agent_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def inventory_spy() -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    original = TOOLS["update_inventory"]

    async def spy(args: dict[str, Any]) -> str:
        calls.append(args)
        return f"已更新：{args.get('category')}：库存 {args.get('stock')} 件"

    TOOLS["update_inventory"] = spy
    yield calls
    TOOLS["update_inventory"] = original


class Harness:
    """bus + EventLog + store + agent（attach 好一个新会话）。"""

    def __init__(
        self,
        workdir: Path,
        script: list[list[dict[str, Any]]],
        *,
        subs: tuple = (),
        system_prompt: str = "SYS",
        hang_after: int | None = None,
    ) -> None:
        self.log = EventLog(str(workdir / "events"))
        self.bus = EventBus(self.log)
        self.store = SessionStore(workdir / "sessions")
        self.system_prompt = system_prompt
        self.agent = Agent(
            self.bus,
            ScriptedLLM(script, hang_after=hang_after),
            store=self.store,
            system_prompt=self.system_prompt,
        )
        self.traj = self.store.start()
        self.sid = self.agent.attach(self.traj)
        self.ended = asyncio.Event()
        self.end_reason: list[str] = []
        self.events: list[Event] = []

        async def on_end(event: Event) -> None:
            self.end_reason.append(str(event.payload.get("reason")))
            self.ended.set()

        async def on_any(event: Event) -> None:
            self.events.append(event)

        self.bus.subscribe(Subscription("rec-end", ("turn_end",), on_end, mode=OBSERVE))
        self.bus.subscribe(Subscription("rec-any", ("*",), on_any, mode=OBSERVE))
        for sub in subs:
            self.bus.subscribe(sub)

    def send(self, text: str) -> None:
        self.bus.publish(Event("user_input", self.sid, {"text": text}), to=self.agent.agent_id)

    def publish(self, event: Event) -> None:
        self.bus.publish(event, to=self.agent.agent_id)

    async def wait_turn(self) -> str:
        await asyncio.wait_for(self.ended.wait(), TIMEOUT)
        self.ended.clear()
        await self.bus.drain(timeout=TIMEOUT)
        return self.end_reason[-1]

    async def stop(self) -> None:
        await self.agent.stop()

    def traj_roles(self) -> list[str]:
        return [
            str(e.payload["message"].get("role"))
            for e in self.traj.entries()
            if e.type == "message"
        ]


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(asyncio.wait_for(coro, TIMEOUT))


# ------------------------------------------------------------------ 轨迹账目


def test_turn_writes_valid_sequence_into_trajectory(
    workdir: Path, inventory_spy: list[dict[str, Any]]
) -> None:
    """一轮 turn 在轨迹里是合法序列；生命周期事实也在。"""
    h = Harness(workdir, [CALL_INVENTORY, FINAL_TEXT])

    async def go() -> str:
        h.send("把保温杯库存改成 42 件")
        return await h.wait_turn()

    assert run(go()) == "turn end"
    assert h.traj_roles() == ["user", "assistant", "tool", "assistant"]
    assert inventory_spy == [{"category": "保温杯", "stock": 42}]
    types = [e.type for e in h.traj.entries()]
    assert types[0] == "session_started"
    # 合成的注脚只属于合成消息：正常消息不带 synthetic
    assert not any(e.payload.get("synthetic") for e in h.traj.entries())


def test_projection_built_per_step_and_notes_stripped(
    workdir: Path, inventory_spy: list[dict[str, Any]]
) -> None:
    """每次 step 现算投影：system 永远第一条，第二次调用带上完整历史。"""
    h = Harness(workdir, [CALL_INVENTORY, FINAL_TEXT])

    async def go() -> None:
        h.send("把保温杯库存改成 42 件")
        await h.wait_turn()

    run(go())
    llm = h.agent.llm
    assert llm.calls == 2
    for ctx in llm.contexts:
        assert ctx[0]["role"] == "system" and ctx[0]["content"] == "SYS"
    assert [m["role"] for m in llm.contexts[0]] == ["system", "user"]
    # 第二次调用：user + assistant(tool_calls) + tool 结果
    assert [m["role"] for m in llm.contexts[1]] == ["system", "user", "assistant", "tool"]
    assert not any("synthetic" in m or "note" in m for m in llm.contexts[1])


def test_steering_lands_in_trajectory_mid_turn(
    workdir: Path, inventory_spy: list[dict[str, Any]]
) -> None:
    """step 边界 drain 的插话：进轨迹（role=user），且发 steering_consumed。"""
    h = Harness(workdir, [FINAL_TEXT])

    async def go() -> None:
        h.send("第一句")
        h.publish(Event("user_input", h.sid, {"text": "插话：顺便查下规则"}))
        await h.wait_turn()

    run(go())
    users = [
        str(e.payload["message"]["content"])
        for e in h.traj.entries()
        if e.type == "message" and e.payload["message"]["role"] == "user"
    ]
    assert users == ["第一句", "插话：顺便查下规则"]
    assert any(r["type"] == "steering_consumed" for r in h.log.read_since(0))


def test_interrupt_stop_marks_trajectory(workdir: Path) -> None:
    """流式中断：turn 以 interrupted 收尾，轨迹里的合成消息带 synthetic 留痕。"""
    # hang_after=0：第一个 chunk 吐完就挂住——中断必然打在 stream 阶段
    h = Harness(workdir, [[{"type": "text_delta", "text": "很长的一段回答"}]], hang_after=0)

    async def go() -> None:
        h.send("随便说点什么")
        await asyncio.sleep(0.05)  # step 确定挂在流中
        h.publish(Event("user_interrupt", h.sid, {}))
        await h.wait_turn()

    run(go())
    assert h.end_reason[-1] == "interrupted"
    synth = [
        e for e in h.traj.entries()
        if e.type == "message" and e.payload.get("synthetic")
    ]
    assert synth, "合成消息必须进事实层，否则投影重建不出这段 history"
    assert all(e.payload.get("note") for e in synth)
    # 被掐的 step 不留半截：尾部是 assistant 打断占位（封死没被回答的问题）
    last = h.traj.last_message()
    assert last is not None and last["content"] == INTERRUPTED


def test_interrupt_with_message_keeps_turn_alive(workdir: Path, inventory_spy: list[dict[str, Any]]) -> None:
    """打断并附新消息：掐掉在飞的一步 + 补占位与新消息，turn 不结束，同一 turn 内继续。

    时序是钉死的：tool_call chunk 已消费（tool_call_started 已发、partial 里有
    tool_calls），流挂在 hang 上；此刻打断 → 取消必然命中 → 在飞产物整步丢、
    补占位与新消息，同一 turn 内继续，最终回答收尾。
    """
    h = Harness(workdir, [CALL_INVENTORY, FINAL_TEXT], hang_after=0)
    started = asyncio.Event()

    async def on_started(event: Event) -> None:
        started.set()

    async def go() -> None:
        h.bus.subscribe(Subscription("rec-tcs", ("tool_call_started",), on_started, mode=OBSERVE))
        h.send("改成 45 件")
        await asyncio.wait_for(started.wait(), TIMEOUT)  # 流确定挂在半路
        h.publish(
            Event(
                "user_interrupt",
                h.sid,
                {"text": "不对，改成 45 件，规格也要改"},
            )
        )
        reason = await h.wait_turn()
        assert reason == "turn end", h.end_reason  # turn 没有被掐死

    run(go())
    roles = h.traj_roles()
    assert roles[0] == "user"
    assert roles[-1] == "assistant"  # 最终回答收尾
    notes = [
        str(e.payload.get("note"))
        for e in h.traj.entries()
        if e.type == "message" and e.payload.get("synthetic")
    ]
    assert notes == ["interrupted", "redirect"]
    # 在飞 step 的产物整步丢：没有半截 assistant(tool_calls)、没有 NO_EXEC 占位结果
    tool_contents = [
        str(e.payload["message"].get("content"))
        for e in h.traj.entries()
        if e.type == "message" and e.payload["message"].get("role") == "tool"
    ]
    assert not tool_contents
    assert inventory_spy == []  # 被掐的工具一次都没执行
    new_user = [
        str(e.payload["message"]["content"])
        for e in h.traj.entries()
        if e.type == "message"
        and e.payload["message"].get("role") == "user"
        and e.payload.get("synthetic")
    ]
    assert new_user == ["不对，改成 45 件，规格也要改"], new_user


def test_governance_deny_placeholder_in_trajectory(
    workdir: Path, inventory_spy: list[dict[str, Any]]
) -> None:
    """被否决的工具：占位结果进轨迹（自描述），裁决进 EventLog。"""
    h = Harness(
        workdir,
        [CALL_INVENTORY, FINAL_TEXT],
        subs=(permission_guard("update_inventory"),),
    )

    async def go() -> None:
        h.send("把保温杯库存改成 42 件")
        await h.wait_turn()

    run(go())
    assert inventory_spy == []  # 一次都没执行
    tool_contents = [
        str(e.payload["message"].get("content"))
        for e in h.traj.entries()
        if e.type == "message" and e.payload["message"].get("role") == "tool"
    ]
    assert tool_contents and tool_contents[0].startswith(BLOCKED_PREFIX)
    decisions = [
        d
        for r in h.log.read_since(0)
        for d in r.get("decisions", [])
        if d["action"] == "deny"
    ]
    assert decisions and decisions[0]["by"] == "permission_guard"


# ------------------------------------------------------------------ 生命周期


def test_resume_with_new_agent_continues_conversation(
    workdir: Path, inventory_spy: list[dict[str, Any]]
) -> None:
    """resume：换一个 agent 实例接着聊，投影带完整历史，事实不重复。"""
    h = Harness(workdir, [CALL_INVENTORY, FINAL_TEXT])

    async def go() -> None:
        h.send("把保温杯库存改成 42 件")
        await h.wait_turn()
        await h.stop()

    run(go())
    sid = h.sid
    first_projection = build_context(h.traj).messages

    # 重启：全新的 bus / agent / 实例，只从 store 恢复
    log2 = EventLog(str(workdir / "events"))
    bus2 = EventBus(log2)
    agent2 = Agent(bus2, ScriptedLLM([SECOND_TEXT]), store=h.store, system_prompt=h.system_prompt)
    traj2 = h.store.resume(sid)
    agent2.attach(traj2)

    ended = asyncio.Event()

    async def on_end(event: Event) -> None:
        ended.set()

    bus2.subscribe(Subscription("rec-end2", ("turn_end",), on_end, mode=OBSERVE))

    async def go2() -> None:
        bus2.publish(Event("user_input", sid, {"text": "第二句"}), to=agent2.agent_id)
        await asyncio.wait_for(ended.wait(), TIMEOUT)
        await bus2.drain(timeout=TIMEOUT)
        await agent2.stop()

    run(go2())
    llm2 = agent2.llm
    assert llm2.calls == 1
    # 第二轮的上下文 = 第一轮投影 + 新的 user（事实各只有一份）
    assert [m["role"] for m in llm2.contexts[0]] == [
        "system", "user", "assistant", "tool", "assistant", "user",
    ]
    assert llm2.contexts[0][1]["content"] == "把保温杯库存改成 42 件"
    assert llm2.contexts[0][-1]["content"] == "第二句"
    assert len(first_projection) == 5  # system + 第一轮四条


def test_unknown_sid_raises_loudly(workdir: Path) -> None:
    """没 attach 过的 sid：宁可炸也不静默开一段新历史。"""
    h = Harness(workdir, [FINAL_TEXT])
    with pytest.raises(KeyError, match="SessionStore"):
        h.agent._traj("ghost-session")


def test_attach_records_prompt_change(workdir: Path) -> None:
    """resume 后换了模板：attach 发现轨迹记录的 prompt 和本次运行不一致，
    追加 prompt_change 留痕（不落盘审计就有洞）；投影随即用新 prompt；
    再 attach 一次（幂等）：不再重复追加。"""
    from baby_event_driven_agent.stages.stage04_trajectory.agent import build_context
    from baby_event_driven_agent.stages.stage04_trajectory.transport.events import Event as Ev

    store = SessionStore(workdir / "s-att")
    traj = store.start(system_prompt="旧模板")
    bus = EventBus(EventLog(str(workdir / "ev-att")))
    agent = Agent(bus, ScriptedLLM([FINAL_TEXT]), store=store, system_prompt="新模板")
    agent.attach(traj)

    changes = [e for e in traj.entries() if e.type == "prompt_change"]
    assert len(changes) == 1
    assert changes[0].payload["system_prompt"] == "新模板"
    assert build_context(traj).messages[0]["content"] == "新模板"

    agent.attach(traj)  # 幂等：记录已一致，不再追加
    assert len([e for e in traj.entries() if e.type == "prompt_change"]) == 1
