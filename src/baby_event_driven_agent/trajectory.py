"""轨迹（trajectory）。

agent emit 出去的生命周期事件就是轨迹本身，不另建一套埋点。

轨迹是 append-only 的扁平事件流，落盘用 jsonl：进程崩了也不丢已落盘的
部分。turn / step / messages 都是从它派生的视图——热路径不投影，只在
恢复、回放、审计时才走 replay。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .events import Decision, Event


@dataclass(frozen=True)
class Record:
    seq: int
    ts: float
    type: str
    session_id: str
    turn_id: str | None = None
    step_id: str | None = None
    payload: dict = field(default_factory=dict)
    decisions: tuple[dict, ...] = ()

    def to_json(self) -> str:
        data = asdict(self)
        data["decisions"] = list(self.decisions)
        return json.dumps(data, ensure_ascii=False)


class Trajectory:
    def __init__(self, path: str | None = None) -> None:
        self._records: list[Record] = []
        self._path = path
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            open(path, "w", encoding="utf-8").close()

    def append(
        self,
        event: Event,
        *,
        turn_id: str | None = None,
        step_id: str | None = None,
        decisions: Iterable[Decision] = (),
    ) -> Record:
        record = Record(
            seq=len(self._records) + 1,
            ts=time.time(),
            type=event.type,
            session_id=event.session_id,
            turn_id=turn_id,
            step_id=step_id,
            payload=dict(event.payload),
            decisions=tuple(
                {"by": d.by, "action": d.action, "reason": d.reason} for d in decisions
            ),
        )
        self._records.append(record)
        if self._path:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(record.to_json() + "\n")
        return record

    @property
    def records(self) -> list[Record]:
        return list(self._records)

    def replay_messages(self, session_id: str) -> list[dict[str, Any]]:
        """从事件流重建消息视图。只用于恢复 / 回放 / 审计，
        热路径不要调它——agent 内存里那份 messages 才是工作态。"""
        messages: list[dict[str, Any]] = []
        for record in self._records:
            if record.session_id != session_id:
                continue
            if record.type in ("user.input", "user.steering"):
                messages.append({"role": "user", "content": record.payload.get("text", "")})
            elif record.type == "model.after":
                messages.append(record.payload.get("message", {}))
            elif record.type == "tool.result":
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": record.payload.get("call_id"),
                        "name": record.payload.get("name"),
                        "content": record.payload.get("output_sent", ""),
                    }
                )
        return messages
