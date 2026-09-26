"""事件四件套、事件子类（约束与路由元数据）、治理裁决、订阅声明。

事件类型是类层次：基类 `Event` 保持 stage01 的四件套（type, session_id,
payload, ts），子类各管各的声明——

- **StreamEvent（上行）**：声明消费约束 `HANDLER_SHAPE = Mailbox`——高频、
  逐 token 的 stream 事件只允许邮箱型消费者订阅（逐条 await handler 会把
  每次调用的耗时放大进 emit）。
- **UserMessage（下行）**：声明收件箱路由元数据 `intent` / `priority`——
  排队还是插话、同队列内的先后。打断（stop / redirect）与审批答复不排队，
  是独立的旁路事件，不在这个类型上。

上行与下行的差异都收在子类里，基类不认识"消费约束"和"排队"这些词。
订阅约束的执行在绑定形成处（总线订阅进门），但那里只有一条对所有事件
类型都相同的通用规则——查事件类声明的 HANDLER_SHAPE；具体哪个类型
要求什么形状，是子类的事。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, Protocol, runtime_checkable

ALLOW = "allow"
DENY = "deny"
MODIFY = "modify"
# 治理裁决只有当场能判的三种（治理链在 governance.py）。"要不要先问人"不是
# 裁决——它声明在工具定义上（Tool.requires_approval），agent 执行前查一次，
# 要走人工确认流程。

# ---- 下行消息的用户意图（UserMessage 的路由元数据） ----

FOLLOWUP = "followup"  # 默认：排队，等当前 turn 结束后作为新 turn 处理
STEERING = "steering"  # 插话：turn 在飞时，下一个 step 之前拼进当前上下文；
# turn 空闲时无可插对象，自然降级成 followup（worker 空闲取到它就开新 turn）。
# 打断（stop / redirect）不是 intent——那是独立的 user_interrupt 事件、走旁路。

# 收件箱的默认排队序（与 Subscription.order 同一惯例：小的先跑）。
# 打断 / 审批答复不走队列，天生在所有排队消息之前。
DEFAULT_PRIORITY = 100


@runtime_checkable
class Mailbox(Protocol):
    """邮箱型消费者的结构：offer 把事件放进消费者自己的缓冲，永不阻塞。

    结构约束而非基类——任何提供 offer 的对象都是邮箱型（StreamConsumer、
    自定义邮箱皆可）；总线分派时只认这个结构。
    """

    def offer(self, event: Event) -> None: ...


@dataclass(frozen=True)
class Event:
    """事件四件套。frozen：一旦发出去就改不动，否则轨迹对不上。"""

    type: str
    session_id: str
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    #: 能消费我的 handler 必须满足的形状；object = 无约束。
    #: 声明在事件类上，子类改写它——这是"我能被谁消费"的唯一出处。
    HANDLER_SHAPE: ClassVar[type] = object

    def with_patch(self, patch: Mapping[str, Any]) -> "Event":
        """治理改写：返回一个 payload 被改过的新事件，原事件不动。"""
        return replace(self, payload={**self.payload, **patch})


class StreamEvent(Event):
    """stream 事件（高频、逐 token）：只允许邮箱型消费者订阅。"""

    HANDLER_SHAPE = Mailbox


@dataclass(frozen=True)
class UserMessage(Event):
    """下行用户消息：收件箱的路由元数据声明在这个类型上。

    上行事件不带这些字段——排队是下行消息的事，与总线 emit 的事件无关。
    """

    intent: str = FOLLOWUP  # 排队（默认）或插话
    priority: int = DEFAULT_PRIORITY  # 同一队列内的先后：小的先被取，同序按到达先后


# wire 类型名 → 事件类：约束跟着类型声明，查表只在这一处。
# 上行 / 下行各自的类型清单在各自的构造点（agent._emit 走总线分道，
# 收件箱只进 UserMessage），这张表只服务订阅校验。
EVENT_CLASSES: dict[str, type[Event]] = {
    "agent_delta": StreamEvent,
    "agent_thinking": StreamEvent,
}


def event_class(event_type: str) -> type[Event]:
    """wire 类型名对应的事件类；没登记的按基类（无约束）。"""
    return EVENT_CLASSES.get(event_type, Event)


def require_consumable(event_type: str, handler: object) -> None:
    """事件类型自己的消费约束：我能被谁消费，问我（的类），不查别处。"""
    shape = event_class(event_type).HANDLER_SHAPE
    if shape is not object and not isinstance(handler, shape):
        raise ValueError(
            f"事件 {event_type} 只允许 {shape.__name__} 型消费者"
            "（提供 offer，如 StreamConsumer）："
            "逐条 await handler 会把每次调用的耗时放大进 emit"
        )


def validate_subscription(sub: "Subscription") -> None:
    """订阅进门时，对声明的每个事件类型问一遍消费约束（fail fast）。"""
    for event_type in sub.event_types:
        require_consumable(event_type, sub.handler)


Handler = Callable[[Event], Awaitable[None]]
Sink = Callable[[Event], None]  # inbound 投递函数：同步、无返回


@dataclass(frozen=True)
class Decision:
    """拦截型订阅者的裁决。每个裁决都要进轨迹，否则事后答不出
    “这个工具为什么没执行”。"""

    action: str
    by: str
    reason: str = ""
    patch: dict | None = None

    @classmethod
    def allow(cls, by: str, reason: str = "") -> "Decision":
        return cls(action=ALLOW, by=by, reason=reason)

    @classmethod
    def deny(cls, by: str, reason: str) -> "Decision":
        return cls(action=DENY, by=by, reason=reason)


@dataclass(frozen=True)
class Subscription:
    """观测订阅者的声明。纯数据，零校验——事件类型能不能被这个 handler
    消费，由总线在订阅进门时问事件类（validate_subscription），订阅者对
    约束一无所知。治理规则不在这里——那不是订阅，是 agent 工具执行路径上
    的准入关卡（governance.py 的 Rule / Governor）。
    """

    name: str  # 进轨迹，用于归因
    event_types: tuple[str, ...]
    handler: Callable[[Event], Awaitable[None]] | Mailbox

    def matches(self, type: str) -> bool:
        return type in self.event_types or "*" in self.event_types
