"""事件、总线、append-only session log。

总线从这一章起分了方向，两条路不再混在一起：

- inbound（命令）：publish(event, to) —— **同步**，把命令交给目标 agent 登记的
  投递函数就返回。不 await、不扇出，投递与执行由此分离。
- outbound（事件）：emit(event) —— **异步**，把 agent 发出的事件扇出给订阅者。
  本章的 emit 仍是 await handler 的简单扇出；异步分发 / QoS / 背压 / 治理
  留到 Stage 4。

publish 变成同步还带来一个结构性好处：它返回 None，调用方既 await 不了、
也 create_task 不了——"乱起一个并发 task、把同一个 session 的 history 写坏"
这件事从签名上就不可能发生。串行改由"收件箱 + 单 worker"保证。

Stage 3 的中断也走 inbound：user_interrupt 和 user_input 同样 publish 进来，
投递函数只在 agent 内部换了一条处理方式（不进收件箱），总线不知道这两者的区别。
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

T0 = time.time()


def t() -> float:
    """进程内相对时间戳，demo 输出用。"""
    return time.time() - T0


@dataclass(frozen=True)
class Event:
    # inbound：user_input / user_interrupt
    # outbound：agent_thinking / agent_delta / agent_reply / tool_call_started /
    #           tool_result / steering_consumed / step_cancelled /
    #           turn_interrupted / turn_end
    type: str
    session_id: str
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


class SessionLog:
    """append-only。Stage 1 没人读它，但它记录的是唯一真相。

    Stage 5 靠它回放重建状态，Stage 6 靠它做可重复的 eval。
    """

    def __init__(self, path: str) -> None:
        self._path = path

    def append(self, event: Event, **extra: object) -> None:
        rec: dict = {
            "ts": round(event.ts - T0, 2),
            "type": event.type,
            "session": event.session_id,
            "payload": event.payload,
        }
        rec.update(extra)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


Handler = Callable[[Event], Awaitable[None]]
Sink = Callable[[Event], None]  # inbound 投递函数：同步、无返回


class EventBus:
    """两条方向，两条路。

    inbound 按 agent_id 路由到投递函数（同步）；outbound 按事件类型扇出给
    订阅者（异步）。同一个方法不再既当命令投递、又当事件扇出。
    """

    def __init__(self) -> None:
        self._sinks: dict[str, Sink] = {}
        self._subs: dict[str, list[Handler]] = defaultdict(list)

    # ---- 注册 ----

    def register(self, agent_id: str, sink: Sink) -> None:
        """登记一个 agent 的入站投递函数：publish 按 id 找到它。"""
        self._sinks[agent_id] = sink

    def subscribe(self, type: str, handler: Handler) -> None:
        """订阅 outbound 事件。"""
        self._subs[type].append(handler)

    # ---- inbound：命令 ----

    def publish(self, event: Event, to: str) -> None:
        """同步投递到目标 agent，立即返回。命令不做扇出。"""
        sink = self._sinks.get(to)
        if sink is None:
            raise KeyError(f"没有这个 agent：{to!r}")
        sink(event)

    # ---- outbound：事件 ----

    async def emit(self, event: Event) -> None:
        """把 agent 发出的事件扇出给订阅者。"""
        for handler in self._subs.get(event.type, []):
            await handler(event)
