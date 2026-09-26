"""事件信封、QoS 分道、治理裁决、订阅声明。

上行（outbound）挤着两种量级完全不同的东西：

- 生命周期事件：turn_end / agent_reply / tool_result / step_cancelled …
  低频、**不可丢**、要按序。它们是事实，丢一条轨迹就断了。
- token 流：agent_delta / agent_thinking。高频、**可丢**、可合并。
  它们是呈现，丢一帧只是屏幕上少一个字。

给它们分两条道（QoS lane），是为了让洪峰淹没不了信号：token 流堵了只丢自己
的，生命周期事件走自己的道、照常送达（两条道各有自己的 worker）。

信封在 stage01 的 (type, session_id, payload, ts) 上加两个字段：
- seq：总线在 emit 时分配，落盘即编号，进程重启接着走。Stage 5 拿它当轨迹
  坐标（压缩区间、回放位点），Stage 6 拿它切 eval 切片。
- correlation_id：一次 turn 的簇 id。同一次用户输入引发的所有事件共享它，
  回放时能把散落的 token 流和生命周期事件聚成一簇。tool_result 与 tool_call
  的配对不占这个字段——那是更细的一层关联，靠 payload 里的 tool_call_id。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, Protocol, runtime_checkable

# ---- QoS：两条道 ----
STATE = "state"  # 生命周期 / 命令回执：低频、不可丢、按序。满了背压给生产者
STREAM = "stream"  # token 流：高频、可丢、可合并。满了丢最新的

STREAM_TYPES = frozenset({"agent_delta", "agent_thinking"})

OBSERVE = "observe"  # 观测型：异步 fire-and-forget，慢也不挡 loop
INTERCEPT = "intercept"  # 拦截型：串行、有预算，能否决 / 改写

ALLOW = "allow"
DENY = "deny"
MODIFY = "modify"
# 现在没人知道答案 → 去问人。事件没放行，但也没被否决：agent 负责把答复等回来。
# 拦住（deny）是终点，问人（ask）是中途——这就是"当场能判"和"要等人"的分界。
ASK = "ask"

FAIL_OPEN = "open"  # 拦截者失败 / 超时 → 放行
FAIL_CLOSED = "closed"  # 拦截者失败 / 超时 → 拒绝


def lane_of(type: str) -> str:
    """事件类型 → 道。默认走 state：新类型宁可先被当回事，也别悄悄被丢。"""
    return STREAM if type in STREAM_TYPES else STATE


@dataclass(frozen=True)
class Event:
    """事件信封。frozen：一旦发出去就改不动，否则轨迹对不上。"""

    type: str
    session_id: str
    payload: dict = field(default_factory=dict)
    seq: int = 0  # 总线在 emit 时分配
    correlation_id: str = ""  # 一次 turn 的簇 id
    ts: float = field(default_factory=time.time)

    #: 能消费我的 handler 必须满足的形状；object = 无约束。
    #: 声明在事件类上，子类改写它——这是"我能被谁消费"的唯一出处。
    HANDLER_SHAPE: ClassVar[type] = object

    def with_patch(self, patch: Mapping[str, Any]) -> "Event":
        """治理改写：返回一个 payload 被改过的新事件，原事件不动。"""
        return replace(self, payload={**self.payload, **patch})

    def envelope(self) -> dict[str, Any]:
        """信封摘要（demo 打印 / 测试断言用）。"""
        return {
            "seq": self.seq,
            "type": self.type,
            "session": self.session_id,
            "corr": self.correlation_id,
            "lane": lane_of(self.type),
        }


# ---- 下行消息的用户意图（UserMessage 的路由元数据） ----
# 照搬自 stage04：排队还是插话、同队列内的先后。打断（stop / redirect）与
# 审批答复不走队列，是独立的旁路事件（user_interrupt / user_approval）。

FOLLOWUP = "followup"  # 默认：排队，等当前 turn 结束后作为新 turn 处理
STEERING = "steering"  # 插话：turn 在飞时，下一个 step 之前拼进当前上下文
DEFAULT_PRIORITY = 100


@runtime_checkable
class Mailbox(Protocol):
    """邮箱型消费者的结构：offer 把事件放进消费者自己的缓冲，永不阻塞。

    结构约束而非基类——任何提供 offer 的对象都是邮箱型（StreamConsumer、
    自定义邮箱皆可）；总线分派时只认这个结构。
    """

    def offer(self, event: "Event") -> None: ...


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

    @classmethod
    def ask(cls, by: str, reason: str) -> "Decision":
        return cls(action=ASK, by=by, reason=reason)


@dataclass(frozen=True)
class EmitResult:
    """emit 的返回值：有没有被放行、最终的事件长什么样、裁决有哪些。

    agent 只看这个结果决定下一步（比如工具被否决就不执行），
    不看订阅者内部做了什么。
    """

    allowed: bool
    event: Event
    decisions: tuple[Decision, ...]

    @property
    def needs_approval(self) -> bool:
        """有人说了"这得问人"（ASK）：没放行，也没被否决，等一个答复。

        同一条事件可能既被改写（MODIFY）又被要求问人（ASK）：改写已经打在
        `event.payload` 上，问人的人看到的也是改写后的参数。
        """
        return any(d.action == ASK for d in self.decisions)


@dataclass(frozen=True)
class Subscription:
    """订阅者注册时就声明清楚自己的行为，运行时照单执行。"""

    name: str  # 进轨迹，用于归因
    event_types: tuple[str, ...]
    handler: Callable[[Event], Awaitable[Decision | None]] | Mailbox
    mode: str = OBSERVE
    order: int = 100  # 拦截型串行顺序，小的先跑
    on_failure: str = FAIL_OPEN  # open | closed，仅拦截型有效

    def matches(self, type: str) -> bool:
        return type in self.event_types or "*" in self.event_types
