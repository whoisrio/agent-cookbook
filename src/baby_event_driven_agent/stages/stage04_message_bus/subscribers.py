"""几个示例订阅者与治理规则。

- permission_guard：治理规则（governance.Rule），当场否决一次工具调用
- counter：观测订阅者，统计（demo / 测试用来量"上行有多少"）

治理判的是"现在能不能执行"（黑名单、规则匹配）——规则挂在 agent 的
governor 上，不在总线；"要不要先问人"是工具自己的声明（tools.py 的
`Tool.requires_approval`）。观测订阅者的契约：handler 只做接收，微秒级——
慢消费者请继承 StreamConsumer（热路径缓冲，帧界回调 on_flush），
或提供自己的邮箱。
"""

from __future__ import annotations

from .events import Decision, Subscription
from .governance import FAIL_CLOSED, Rule


def permission_guard(*blocked: str, order: int = 10) -> Rule:
    """禁止一组工具。安全相关的规则，失败时拒绝（fail-closed）。

    裁决由 Governor 带回 agent —— 事后能答出“这个工具为什么没执行”。
    """
    denied = set(blocked)

    async def guard(name: str, arguments: str) -> Decision | None:
        if name in denied:
            return Decision.deny("permission_guard", f"工具 {name} 在黑名单里")
        return None

    return Rule(
        name="permission_guard",
        handler=guard,
        order=order,
        on_failure=FAIL_CLOSED,
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
    )





