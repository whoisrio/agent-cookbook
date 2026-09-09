"""事件、总线、append-only session log。

总线只做一件事：订阅了谁，事件来了原地 await 谁。
agent 的整个 turn 在总线回调里跑完——这是本 stage 的设计决定，
代价（没有排队）由后续 stage 的收件箱偿还。
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
    type: str  # user_input / agent_delta / agent_reply / turn_end
    session_id: str
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


class SessionLog:
    """append-only。Stage 1 没人读它，但它记录的是唯一真相。

    Stage 6 靠它回放重建状态，Stage 7 靠它做可重复的 eval。
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


class EventBus:
    """同步总线：publish 原地 await handler。"""

    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, type: str, handler: Handler) -> None:
        self._subs[type].append(handler)

    async def publish(self, event: Event) -> None:
        for handler in self._subs.get(event.type, []):
            await handler(event)
