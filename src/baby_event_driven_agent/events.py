"""事件与订阅的契约。

两个方向走的是两条不同的路，别混：

- inbound（命令）：外部送进 agent 的 inbox。有目标、有顺序、要背压、
  可以被插队。语义是"你要去做这件事"。
- outbound（通知 / 拦截）：agent 在循环里 emit 出去。语义是"这件事
  发生了"或者"这件事即将发生"。没有目标，谁订阅谁收到。

所有结构都是 frozen：事件一旦发出去就不能改，否则轨迹对不上。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

# 订阅者回发事件可能形成回环（A 发 Y、B 发 Y 又发 X），超过这个跳数就停止扇出
MAX_HOP = 3


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class EventType:
    """inbound 是命令，outbound 是事实或即将发生的事。命名上分得开：
    命令用点分动作名，拦截用 before_ 前缀，事实用过去时或名词。"""

    # ---- inbound：命令 ----
    USER_INPUT = "user.input"
    USER_STEERING = "user.steering"      # 中途改方向，插到下一步之前
    USER_FOLLOWUP = "user.followup"      # 排队，本轮结束后接着做
    INTERRUPT = "control.interrupt"      # 取消当前这一步

    # ---- outbound：生命周期 ----
    TURN_START = "turn.start"
    BEFORE_MODEL = "model.before"
    AFTER_MODEL = "model.after"
    BEFORE_TOOL_CALL = "tool.before_call"   # 拦截点
    TOOL_RESULT = "tool.result"
    TURN_END = "turn.end"
    ERROR = "agent.error"


# 进 ctrl 队列的事件类型：不排队、不背压、优先取
CTRL_EVENTS = frozenset({EventType.INTERRUPT, EventType.USER_STEERING})

# 模块级别名，省得到处写 EventType.X
USER_INPUT = EventType.USER_INPUT
USER_STEERING = EventType.USER_STEERING
USER_FOLLOWUP = EventType.USER_FOLLOWUP
INTERRUPT = EventType.INTERRUPT
TURN_START = EventType.TURN_START
BEFORE_MODEL = EventType.BEFORE_MODEL
AFTER_MODEL = EventType.AFTER_MODEL
BEFORE_TOOL_CALL = EventType.BEFORE_TOOL_CALL
TOOL_RESULT = EventType.TOOL_RESULT
TURN_END = EventType.TURN_END
ERROR = EventType.ERROR

OBSERVE = "observe"
INTERCEPT = "intercept"

ALLOW = "allow"
DENY = "deny"
MODIFY = "modify"

FAIL_OPEN = "open"       # 拦截者失败 / 超时 → 放行
FAIL_CLOSED = "closed"   # 拦截者失败 / 超时 → 拒绝


@dataclass(frozen=True)
class Event:
    type: str
    session_id: str
    payload: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("evt"))
    ts: float = field(default_factory=time.time)
    ctrl: bool = False
    correlation_id: str | None = None
    hop: int = 0
    attempt: int = 0

    def __post_init__(self) -> None:
        if self.type in CTRL_EVENTS:
            object.__setattr__(self, "ctrl", True)

    @property
    def text(self) -> str:
        return str(self.payload.get("text", ""))

    def with_patch(self, patch: Mapping[str, Any]) -> "Event":
        """返回一个 payload 被改写过的新事件，原事件不动。"""
        return replace(self, payload={**self.payload, **patch})

    def reply(self, type: str, **payload: Any) -> "Event":
        """构造一条同 session、可配对的回复事件。"""
        return Event(
            type=type,
            session_id=self.session_id,
            payload=dict(payload),
            correlation_id=self.id,
            hop=self.hop + 1,
        )


@dataclass(frozen=True)
class Decision:
    """拦截类订阅者的裁决。每个裁决都要进轨迹，否则事后答不出
    "这个工具为什么没执行"。"""

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
class EmitResult:
    allowed: bool
    event: Event
    decisions: tuple[Decision, ...]


@dataclass(frozen=True)
class Subscription:
    """订阅者注册时就声明清楚自己的行为，运行时照单执行，
    不让实现的人临时决定。"""

    name: str                      # 进轨迹，用于归因
    event_types: tuple[str, ...]
    handler: Callable[[Event], Awaitable[Decision | None]]
    mode: str = OBSERVE            # observe | intercept
    order: int = 100               # 拦截类串行顺序，小的先跑
    on_failure: str = FAIL_OPEN    # open | closed，仅拦截类有效
    idempotent: bool = True        # 通知类重放时能否安全重复执行

    def matches(self, event_type: str) -> bool:
        if event_type in self.event_types:
            return True
        return "*" in self.event_types
