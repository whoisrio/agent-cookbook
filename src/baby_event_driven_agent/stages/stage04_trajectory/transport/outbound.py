"""outbound 末端的两个零件：合并缓冲（UI 侧）与示例订阅者。

合并缓冲解决的是“UI 刷不动”：token 是一个一个来的，屏幕没必要一个一个刷。
刷屏的两个触发条件缺一不可：

- **满了就刷**：洪峰时不攒着，攒着就是延迟。
- **到帧界就刷**：只等满的话，尾巴上的字要等下一批才出来，UI 一顿一顿。

二者其一，先到先刷。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from .events import Event


class CoalescingBuffer:
    """把一串增量合并成一帧，再交给 sink。"""

    def __init__(
        self,
        sink: Callable[[str], None],
        *,
        max_chars: int = 96,
        frame: float = 0.05,
    ) -> None:
        self._sink = sink
        self._max_chars = max_chars
        self._frame = frame
        self._buf: list[str] = []
        self._size = 0
        self.merged = 0  # 收到的增量条数
        self.flushes = 0  # 实际刷屏次数
        self._task: asyncio.Task[None] | None = None

    def add(self, text: str) -> None:
        self._buf.append(text)
        self._size += len(text)
        self.merged += 1
        if self._size >= self._max_chars:
            self.flush()

    def offer(self, event: Event) -> None:
        """邮箱型入口：热路径只做缓冲追加（微秒级），永不阻塞。

        让 CoalescingBuffer 满足 stream 事件的消费约束（events.Mailbox），
        可被直接订阅到 agent_delta / agent_thinking。
        """
        self.add(str(event.payload.get("text", "")))

    def flush(self) -> None:
        if not self._buf:
            return
        self._sink("".join(self._buf))
        self._buf.clear()
        self._size = 0
        self.flushes += 1

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._ticker(), name="frame-ticker")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.flush()

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(self._frame)
            self.flush()

    @property
    def pending(self) -> int:
        return self._size


def install_delta_sink(buffer: CoalescingBuffer, kind: str = "") -> Callable[[Event], object]:
    """把 buffer 接到 agent_delta / agent_thinking 上（demo 用）。"""
    return lambda event: buffer.add(str(event.payload.get("text", "")))


class StreamConsumer(CoalescingBuffer):
    """stream 事件消费者基类：热路径 offer 只做缓冲追加（微秒级），
    满格 / 帧界触发 on_flush——子类只实现慢侧钩子（刷新 UI）。

    事件订阅约束：agent_delta / agent_thinking 只允许邮箱型消费者订阅
    （事件类型自己声明，events.require_consumable，订阅构造时问它）——
    逐条 await handler 会把每次调用的耗时放大进 emit，热路径必须留在基类里。
    """

    def __init__(self, *, max_chars: int = 96, frame: float = 0.05) -> None:
        super().__init__(self._emit_flush, max_chars=max_chars, frame=frame)

    def _emit_flush(self, text: str) -> None:
        self.on_flush(text)

    def offer(self, event: Event) -> None:
        self.add(str(event.payload.get("text", "")))

    def on_flush(self, text: str) -> None:
        """帧界回调：子类在这里刷新 UI。基类不提供默认实现。"""
        raise NotImplementedError
