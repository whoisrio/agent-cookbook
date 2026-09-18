"""几个示例订阅者，演示“加能力不用改 loop”。

- permission_guard：拦截型（治理），当场否决一次工具调用
- approval_policy：拦截型（治理），当场判“这个要问人”，然后由人去给答案
- slow_observer：观测型，故意慢——本章要证明它慢也拖不垮 loop
- counter：观测型，统计（demo / 测试用来量“上行有多少”）

两个拦截者的分工就是那把刀切在哪：**当场能判的归规则，要等的归人**。
"""

from __future__ import annotations

import asyncio

from .events import (
    FAIL_CLOSED,
    INTERCEPT,
    OBSERVE,
    Decision,
    Event,
    Subscription,
)


def permission_guard(*blocked: str, order: int = 10) -> Subscription:
    """禁止一组工具。安全相关的拦截，失败时拒绝（fail-closed）。

    裁决会跟着事件进 log —— 事后能答出“这个工具为什么没执行”。
    """
    denied = set(blocked)

    async def guard(event: Event) -> Decision | None:
        name = str(event.payload.get("name", ""))
        if name in denied:
            return Decision.deny("permission_guard", f"工具 {name} 在黑名单里")
        return None

    return Subscription(
        name="permission_guard",
        event_types=("before_tool_call",),
        handler=guard,
        mode=INTERCEPT,
        order=order,
        on_failure=FAIL_CLOSED,
    )


def approval_policy(*gated: str, order: int = 20, by: str = "approval_policy") -> Subscription:
    """这些工具先别执行：去问人。

    它只做**当场能判的那一半**——"要不要问人"（查个集合，微秒级）。
    人的答案之后才来，由 agent 把答复等回来（`Agent._request_approval`）。
    等回来的还是一个 Decision：批准 = ALLOW、拒绝 = DENY、批准并改了参数 = MODIFY。

    fail-closed：策略自己坏了就拒绝——"问不问人"这件事上，宁可停下来。
    """

    async def policy(event: Event) -> Decision | None:
        name = str(event.payload.get("name", ""))
        if name in gated:
            return Decision.ask(by, f"工具 {name} 需要人工确认")
        return None

    return Subscription(
        name=by,
        event_types=("before_tool_call",),
        handler=policy,
        mode=INTERCEPT,
        order=order,
        on_failure=FAIL_CLOSED,
    )


def slow_observer(
    delay: float, *, session: str | None = None, name: str = "slow_observer"
) -> Subscription:
    """慢消费者：每个事件睡一会。它慢只该拖慢自己那条道。"""

    async def observe(event: Event) -> None:
        if session is not None and event.session_id != session:
            return
        await asyncio.sleep(delay)

    return Subscription(
        name=name,
        event_types=("*",),
        handler=observe,
        mode=OBSERVE,
    )


def counter(sink: dict[str, int], *, session: str | None = None) -> Subscription:
    """按类型计数。sink 会被就地累加：total 与每个 type 的条数。"""

    async def count(event: Event) -> None:
        if session is not None and event.session_id != session:
            return
        sink["total"] = sink.get("total", 0) + 1
        sink[event.type] = sink.get(event.type, 0) + 1

    return Subscription(
        name="counter",
        event_types=("*",),
        handler=count,
        mode=OBSERVE,
    )
