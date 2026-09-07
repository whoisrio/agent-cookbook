"""几个示例订阅者，演示"加能力不用改 loop"。

四类：
- permission_guard  拦截型，fail-closed：插件坏了宁可卡住也不放行
- approval_gate     拦截型，演示闭环：否决 + 回发一条事件改变 agent 下一步
- memory_extractor  通知型，异步做长期记忆提取
- console_observer  通知型，观测；故意慢也不影响 agent
"""

from __future__ import annotations

import asyncio
from typing import Any

from .bus import EventBus
from .events import (
    FAIL_CLOSED,
    INTERCEPT,
    OBSERVE,
    Decision,
    Event,
    EventType,
    Subscription,
)


def permission_guard(*blocked: str) -> Subscription:
    """禁止一组工具。安全相关的拦截，失败时拒绝（fail-closed）。

    注意：这里的裁决会进轨迹 —— 事后能答出"这个工具为什么没执行"。
    """
    denied = set(blocked)

    async def guard(event: Event) -> Decision | None:
        name = event.payload.get("name", "")
        if name in denied:
            return Decision.deny("permission_guard", f"工具 {name} 在黑名单里")
        return None

    return Subscription(
        name="permission_guard",
        event_types=(EventType.BEFORE_TOOL_CALL,),
        handler=guard,
        mode=INTERCEPT,
        order=10,
        on_failure=FAIL_CLOSED,
    )


def approval_gate(bus: EventBus, agent_id: str, *gated: str) -> Subscription:
    """需要人工批准的工具：否决这一步，同时回发一条事件告诉 agent 该怎么办。

    这就是能力扩展的闭环 —— 订阅者不是只读的，它能改变 agent 下一步。
    """
    gated_set = set(gated)
    asked: set[str] = set()

    async def gate(event: Event) -> Decision | None:
        name = event.payload.get("name", "")
        if name not in gated_set:
            return None

        key = f"{name}:{event.payload.get('call_id', '')}"
        if key in asked:
            return Decision.allow("approval_gate", "已批准")
        asked.add(key)

        # 回发一条 steering：不排队，插到 agent 下一步之前
        bus.publish(
            Event(
                EventType.USER_STEERING,
                event.session_id,
                {
                    "text": (
                        f"[审批] {name} 需要人工批准，已暂停。"
                        "请先向用户确认，不要重试同一个调用。"
                    )
                },
            ),
            to=agent_id,
        )
        return Decision.deny("approval_gate", f"{name} 等待人工批准")

    return Subscription(
        name="approval_gate",
        event_types=(EventType.BEFORE_TOOL_CALL,),
        handler=gate,
        mode=INTERCEPT,
        order=20,
        on_failure=FAIL_CLOSED,
    )


def memory_extractor(sink: list[dict[str, Any]], delay: float = 0.05) -> Subscription:
    """轮次结束时异步抽长期记忆。慢一点无所谓，agent 不等它。"""

    async def extract(event: Event) -> None:
        await asyncio.sleep(delay)  # 模拟一次旁路模型调用
        text = str(event.payload.get("text", ""))
        if text:
            sink.append({"kind": "preference", "text": text[:80]})

    return Subscription(
        name="memory_extractor",
        event_types=(EventType.TURN_END,),
        handler=extract,
        mode=OBSERVE,
    )


def console_observer(delay: float = 0.0) -> Subscription:
    """观测型：慢也不阻塞 agent。delay>0 用来演示这一点。"""

    async def observe(event: Event) -> None:
        if delay:
            await asyncio.sleep(delay)
        payload = event.payload
        brief = {
            k: (str(v)[:60] + "…" if len(str(v)) > 60 else v)
            for k, v in payload.items()
        }
        print(f"    [observe] {event.type} {brief}")

    return Subscription(
        name="console_observer",
        event_types=("*",),
        handler=observe,
        mode=OBSERVE,
    )
