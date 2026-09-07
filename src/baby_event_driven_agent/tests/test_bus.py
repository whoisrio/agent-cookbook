"""验证事件机制里几个不看代码就发现不了的不变量。

    PYTHONPATH=src python -m baby_event_driven_agent.tests.test_bus
    # 或者
    PYTHONPATH=src pytest src/baby_event_driven_agent/tests/test_bus.py
"""

from __future__ import annotations

import asyncio
import time

from baby_event_driven_agent.agent import AgentRuntime, AssistantMessage, Tool
from baby_event_driven_agent.bus import AgentInbox, EventBus, InboxFull
from baby_event_driven_agent.events import (
    DENY,
    FAIL_CLOSED,
    FAIL_OPEN,
    INTERCEPT,
    MODIFY,
    OBSERVE,
    Decision,
    Event,
    EventType,
    Subscription,
)
from baby_event_driven_agent.llm import ScriptedLLM
from baby_event_driven_agent.trajectory import Trajectory

SESSION = "sess_test"


def _evt(type: str = "test.event", **payload) -> Event:
    return Event(type=type, session_id=SESSION, payload=dict(payload))


# ---- 收件箱：优先级与背压 ----


def test_ctrl_jumps_the_queue() -> None:
    async def go() -> None:
        inbox = AgentInbox()
        inbox.put(_evt("normal.one"))
        inbox.put(_evt("normal.two"))
        inbox.put(Event(EventType.INTERRUPT, SESSION, ctrl=True))
        first = await inbox.get()
        assert first.type == EventType.INTERRUPT, "控制事件必须插到队首"
        assert (await inbox.get()).type == "normal.one"

    asyncio.run(go())


def test_backpressure_rejects_instead_of_dropping() -> None:
    inbox = AgentInbox(maxsize=1)
    inbox.put(_evt())
    try:
        inbox.put(_evt())
    except InboxFull:
        return
    raise AssertionError("队列满了必须抛异常，不能静默丢弃")


def test_ctrl_never_rejected_by_backpressure() -> None:
    inbox = AgentInbox(maxsize=1)
    inbox.put(_evt())
    inbox.put(Event(EventType.INTERRUPT, SESSION, ctrl=True))  # 不抛


# ---- emit：拦截、短路、预算、失败策略 ----


def test_deny_short_circuits() -> None:
    async def go() -> None:
        bus = EventBus()
        seen: list[str] = []

        async def first(event: Event) -> Decision:
            seen.append("first")
            return Decision.deny("first", "不行")

        async def second(event: Event) -> Decision:
            seen.append("second")
            return Decision.allow("second")

        bus.subscribe(
            Subscription("first", ("x",), first, mode=INTERCEPT, order=10)
        )
        bus.subscribe(
            Subscription("second", ("x",), second, mode=INTERCEPT, order=20)
        )

        result = await bus.emit(_evt("x"))
        assert result.allowed is False
        assert seen == ["first"], "被否决后后面的拦截者不该再跑"

    asyncio.run(go())


def test_interceptors_share_one_budget() -> None:
    """预算是阶段级的：两个慢拦截者共 50ms，第二个必然超时。"""

    async def go() -> None:
        bus = EventBus()

        async def slow(event: Event) -> Decision:
            await asyncio.sleep(0.04)
            return Decision.allow("slow")

        for i in range(2):
            bus.subscribe(
                Subscription(
                    f"slow{i}",
                    ("x",),
                    slow,
                    mode=INTERCEPT,
                    order=10 * i,
                    on_failure=FAIL_CLOSED,
                )
            )

        result = await bus.emit(_evt("x"), budget_ms=50)
        # 各算各的话两个都能过（0.04 < 0.05），现在是共享一份，第二个必然超时
        assert result.allowed is False, "预算是阶段级的，fail-closed 必须拒绝"
        assert len(result.decisions) == 2
        assert result.decisions[-1].reason in ("拦截者超时", "阶段预算已耗尽")

    asyncio.run(go())


def test_failure_policy_is_declared_per_subscriber() -> None:
    async def go() -> None:
        async def boom(event: Event) -> Decision:
            raise RuntimeError("插件炸了")

        bus = EventBus()
        bus.subscribe(
            Subscription("closed", ("x",), boom, mode=INTERCEPT, on_failure=FAIL_CLOSED)
        )
        assert (await bus.emit(_evt("x"))).allowed is False

        bus2 = EventBus()
        bus2.subscribe(
            Subscription("open", ("x",), boom, mode=INTERCEPT, on_failure=FAIL_OPEN)
        )
        assert (await bus2.emit(_evt("x"))).allowed is True

    asyncio.run(go())


def test_interceptor_can_rewrite_payload() -> None:
    async def go() -> None:
        bus = EventBus()

        async def rewrite(event: Event) -> Decision:
            return Decision(
                action=MODIFY, by="rewriter", patch={"command": "ls -la"}
            )

        bus.subscribe(Subscription("rewriter", ("x",), rewrite, mode=INTERCEPT))
        result = await bus.emit(_evt("x", command="rm -rf /"))
        assert result.event.payload["command"] == "ls -la"

    asyncio.run(go())


def test_observers_never_block_the_agent() -> None:
    async def go() -> None:
        bus = EventBus()

        async def slow_observer(event: Event) -> None:
            await asyncio.sleep(0.5)

        bus.subscribe(Subscription("slow", ("x",), slow_observer, mode=OBSERVE))

        started = time.perf_counter()
        await bus.emit(_evt("x"))
        assert time.perf_counter() - started < 0.1, "观测者再慢也不能拖住 emit"

    asyncio.run(go())


def test_hop_limit_stops_loops() -> None:
    """订阅者可以回发事件，A 发 X、B 发 X 会死循环，靠 hop 截断。"""

    async def go() -> None:
        result = await EventBus().emit(Event("x", SESSION, {}, hop=3))
        assert result.allowed is True
        assert result.decisions == (), "超过跳数不再扇出"

    asyncio.run(go())


# ---- 端到端：取消粒度 ----


def test_cancel_kills_the_step_not_the_loop() -> None:
    """取消之后 loop 必须还活着，历史不能被撕掉。"""

    async def go() -> None:
        bus = EventBus()
        trajectory = Trajectory()

        async def forever(args: dict) -> str:
            await asyncio.sleep(10)
            return "不可能返回"

        agent = AgentRuntime(
            agent_id="a",
            session_id=SESSION,
            bus=bus,
            llm=ScriptedLLM([{"tool_calls": [{"name": "forever", "args": {}}]}]),
            tools=[Tool("forever", forever, "永远跑不完")],
            trajectory=trajectory,
        )
        runner = asyncio.create_task(agent.run())

        await agent.submit("启动")
        await asyncio.sleep(0.2)
        await agent.interrupt("用户取消")
        await asyncio.sleep(0.2)

        ended = [r for r in trajectory.records if r.type == EventType.TURN_END]
        assert len(ended) == 1, f"TURN_END 只能有一条，实际 {len(ended)}"
        assert ended[0].payload["end_reason"] == "interrupted"
        assert len(agent.history) >= 2, "历史还在，会话没被撕掉"

        # 取消之后还能接着用
        await agent.submit("继续")
        await asyncio.sleep(0.2)
        assert len([r for r in trajectory.records if r.type == EventType.TURN_START]) == 2

        agent.stop()
        await asyncio.wait_for(runner, timeout=3.0)

    asyncio.run(go())


def test_cancel_signal_reaches_the_llm() -> None:
    """取消不能只停在调度层。IO 层必须看得见信号，才有机会关流、收尾。"""

    async def go() -> None:
        bus = EventBus()
        seen: list[bool] = []

        class WatchingLLM:
            async def chat(self, messages, tools, cancel=None) -> AssistantMessage:
                seen.append(cancel is not None)
                if cancel is None:
                    return AssistantMessage(content="没拿到信号")
                await cancel.wait()
                seen.append(cancel.is_set())
                raise asyncio.CancelledError()

        agent = AgentRuntime(
            agent_id="a",
            session_id=SESSION,
            bus=bus,
            llm=WatchingLLM(),
            tools=[],
            trajectory=Trajectory(),
        )
        runner = asyncio.create_task(agent.run())

        await agent.submit("启动")
        await asyncio.sleep(0.1)
        await agent.interrupt("用户取消")
        await asyncio.sleep(0.2)

        assert seen == [True, True], f"IO 层必须收到同一个信号对象，实际 {seen}"

        agent.stop()
        await asyncio.wait_for(runner, timeout=3.0)

    asyncio.run(go())


def _all() -> list:
    return [obj for name, obj in sorted(globals().items()) if name.startswith("test_")]


if __name__ == "__main__":
    for case in _all():
        case()
        print(f"  ok  {case.__name__}")
    print(f"\n{len(_all())} 个用例通过")
