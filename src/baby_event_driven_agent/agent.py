"""agent 主循环：拉事件、跑一步、emit 生命周期事件。

三个设计点在这里落地：

1. 拉模型，不推。agent 主动从 inbox 取事件，一次只处理一个，天然串行
   无锁。回调推送会在等模型返回时重入，状态就得加锁。
2. 取消的粒度是单步，不是终止执行体。收到 interrupt 时取消的只是当前
   这一步（模型调用或工具执行），loop 照常活着，会话状态完整，用户
   下一句立刻能接上。
3. 每步之间看一眼 ctrl 队列。所以"下一步做什么"是运行时决定的，
   steering 消息能插进来改方向，不用取消重来。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Protocol

from .bus import AgentInbox, EventBus
from .events import (
    DENY,
    AFTER_MODEL,
    BEFORE_MODEL,
    BEFORE_TOOL_CALL,
    Event,
    EventType,
    INTERRUPT,
    TURN_END,
    TURN_START,
    TOOL_RESULT,
    new_id,
)
from .trajectory import Trajectory

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 3000


class StepInterrupted(Exception):
    """当前这一步被控制类事件打断。loop 不退出，交给 _run_turn 处置。"""

    def __init__(self, event: Event) -> None:
        super().__init__(event.type)
        self.event = event


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON 字符串，不是对象


@dataclass(frozen=True)
class AssistantMessage:
    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()

    def as_message(self) -> dict[str, Any]:
        """整个 assistant message，包括 tool_calls。只挑 text 会丢推理状态。"""
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in self.tool_calls
            ]
        return message


class CancelSignal:
    """显式取消信号，传给 IO 层（模型调用、长循环）。

    task.cancel() 只在调度层把协程撕掉，被调用方不知道发生了什么，来
    不及关流、来不及收尾。这个信号让它在自己的安全点停下来。

    顺序是：先 set，给一个 grace 窗口让它体面收尾；超时再 task.cancel()
    强撕。两道都留着，因为被调用方可能根本不看这个信号。
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


class LLMClient(Protocol):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        cancel: CancelSignal | None = None,
    ) -> AssistantMessage: ...


@dataclass
class Tool:
    name: str
    fn: Callable[[dict[str, Any]], Awaitable[str]]
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)

    async def run(self, args: dict[str, Any]) -> str:
        return await self.fn(args)

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class AgentRuntime:
    """一个 runtime 对应一个 session。多会话就起多个 runtime，
    各自一个 inbox，天然按 session 串行、跨 session 并发。"""

    def __init__(
        self,
        agent_id: str,
        session_id: str,
        bus: EventBus,
        llm: LLMClient,
        tools: list[Tool],
        trajectory: Trajectory,
        *,
        system_prompt: str = "你是一个有用的助手。",
        max_steps: int = 12,
        inbox: AgentInbox | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.session_id = session_id
        self._bus = bus
        self._llm = llm
        self._tools = {t.name: t for t in tools}
        self._trajectory = trajectory
        self._max_steps = max_steps
        self.inbox = bus.register(agent_id, inbox)

        self.history: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        self._running = False

    # ---- 对外接口 ----

    async def submit(self, text: str, *, event_type: str = EventType.USER_INPUT) -> None:
        """外部往 inbox 投递一条命令。"""
        event = Event(type=event_type, session_id=self.session_id, payload={"text": text})
        self._bus.publish(event, to=self.agent_id)

    async def interrupt(self, reason: str = "user cancel") -> None:
        self._bus.publish(
            Event(INTERRUPT, self.session_id, {"reason": reason}), to=self.agent_id
        )

    async def steer(self, text: str) -> None:
        """中途改方向：不排队，插到下一步之前。"""
        self._bus.publish(
            Event(EventType.USER_STEERING, self.session_id, {"text": text}),
            to=self.agent_id,
        )

    # ---- 主循环 ----

    async def run(self) -> None:
        self._running = True
        while self._running:
            event = await self.inbox.get()
            try:
                await self._handle(event)
            except Exception as exc:  # noqa: BLE001 - 单条事件炸了不能带走整个 loop
                logger.exception("处理事件 %s 出错：%r", event.type, exc)
                self._trajectory.append(
                    Event(
                        type=EventType.ERROR,
                        session_id=self.session_id,
                        payload={"event": event.type, "error": repr(exc)},
                    )
                )

    def stop(self) -> None:
        self._running = False
        # 塞一条空事件把 await 在 inbox 上的主循环唤醒退出
        self.inbox.put(Event("_stop", self.session_id, ctrl=True))

    async def _handle(self, event: Event) -> None:
        if event.type in (EventType.USER_INPUT, EventType.USER_FOLLOWUP):
            await self._run_turn(event)
        elif event.type == EventType.USER_STEERING:
            # 空闲时收到 steering：直接并入历史，下一轮生效。
            # 必须写轨迹——回放就是靠这条记录重建用户消息的，不写就凭空消失。
            self._trajectory.append(event)
            self.history.append({"role": "user", "content": event.text})
        elif event.type == INTERRUPT:
            # 没有正在跑的步，忽略即可（loop 仍然活着）。事件本身要留痕，
            # 否则事后查不出"用户按过取消，只是当时没在跑"。
            self._trajectory.append(event)

    # ---- 一轮 ----

    async def _run_turn(self, event: Event) -> None:
        turn_id = new_id("turn")
        text = event.text
        # 触发本轮的命令本身也要进轨迹，否则回放不出"用户说了什么"
        self._trajectory.append(event, turn_id=turn_id)
        await self._emit(
            Event(TURN_START, self.session_id, {"text": text}), turn_id=turn_id
        )
        self.history.append({"role": "user", "content": text})

        end_reason = "stop"
        self._interrupt_reason = ""
        for step in range(1, self._max_steps + 1):
            step_id = new_id("step")
            # 每步之前先看 ctrl 队列：下一步做什么由运行时决定
            pending = await self._drain_steering()
            if pending is not None:
                await self._after_interrupt(StepInterrupted(pending), turn_id, step_id)
                end_reason = "interrupted"
                break

            await self._emit(
                Event(BEFORE_MODEL, self.session_id, {"step": step}),
                turn_id=turn_id,
                step_id=step_id,
            )

            specs = [t.spec() for t in self._tools.values()]
            cancel = CancelSignal()
            try:
                response = await self._interruptible(
                    self._llm.chat(list(self.history), specs, cancel=cancel),
                    cancel=cancel,
                )
            except StepInterrupted as exc:
                # _after_interrupt 拿返回值兼职两种意思：None 是本轮继续，
                # 字符串才是结束原因。别直接写进 end_reason——steering 的
                # None 会一直留到本轮收尾，把 stop 顶掉。
                stop = await self._after_interrupt(exc, turn_id, step_id)
                if stop is None:
                    continue  # 是 steering，带着新指令继续下一步
                end_reason = stop
                break

            self.history.append(response.as_message())
            await self._emit(
                Event(
                    AFTER_MODEL,
                    self.session_id,
                    {"message": response.as_message(), "step": step},
                ),
                turn_id=turn_id,
                step_id=step_id,
            )

            if not response.tool_calls:
                break

            for call in response.tool_calls:
                outcome = await self._run_tool(call, turn_id, step_id)
                if outcome is not None:
                    end_reason = outcome
                    break
            if end_reason != "stop":
                break
        else:
            end_reason = "max_steps"

        await self._emit(
            Event(
                TURN_END,
                self.session_id,
                {
                    "end_reason": end_reason,
                    "reason": self._interrupt_reason,
                    "text": text,
                },
            ),
            turn_id=turn_id,
        )

    async def _run_tool(self, call: ToolCall, turn_id: str, step_id: str) -> str | None:
        """返回 None 表示正常继续；返回字符串表示本轮要结束（中断）。"""
        result = await self._emit(
            Event(
                BEFORE_TOOL_CALL,
                self.session_id,
                {"call_id": call.id, "name": call.name, "arguments": call.arguments},
            ),
            turn_id=turn_id,
            step_id=step_id,
        )

        if not result.allowed:
            # 否决不抛异常，转成工具结果让模型自己绕过去；
            # 同时也要进轨迹，否则回放出来的消息对不上历史
            denial = next((d for d in result.decisions if d.action == DENY), None)
            reason = denial.reason if denial else "被拦截"
            by = denial.by if denial else "unknown"
            content = f"[被 {by} 否决] {reason}"
            self.history.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": content,
                }
            )
            await self._emit(
                Event(
                    TOOL_RESULT,
                    self.session_id,
                    {
                        "call_id": call.id,
                        "name": call.name,
                        "output_raw": "",
                        "output_sent": content,
                        "denied": True,
                        "by": by,
                    },
                ),
                turn_id=turn_id,
                step_id=step_id,
            )
            return None

        tool = self._tools.get(result.event.payload.get("name", call.name))
        if tool is None:
            self.history.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": f"没有这个工具：{call.name}",
                }
            )
            return None

        args = result.event.payload.get("arguments") or {}
        if isinstance(args, str):
            args = _safe_json(args)

        try:
            raw = await self._interruptible(tool.run(args))
        except StepInterrupted as exc:
            return await self._after_interrupt(exc, turn_id, step_id)

        # 进 message 的是截断后的，原文留在轨迹里
        sent = raw[:MAX_OUTPUT_CHARS]
        truncated = len(raw) > MAX_OUTPUT_CHARS
        if truncated:
            sent = f"[已截断，原文 {len(raw)} 字]\n{sent}"

        self.history.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.name,
                "content": sent,
            }
        )
        await self._emit(
            Event(
                TOOL_RESULT,
                self.session_id,
                {
                    "call_id": call.id,
                    "name": call.name,
                    "output_raw": raw,
                    "output_sent": sent,
                    "truncated": truncated,
                },
            ),
            turn_id=turn_id,
            step_id=step_id,
        )
        return None

    # ---- 中断与 steering ----

    async def _after_interrupt(
        self, exc: StepInterrupted, turn_id: str, step_id: str
    ) -> str | None:
        """处置一步被打断。返回 None 表示继续本轮，否则返回结束原因。"""
        event = exc.event
        self._interrupt_reason = str(event.payload.get("reason", ""))
        if event.type == EventType.USER_STEERING:
            self.history.append({"role": "user", "content": event.text})
            return None
        # 只返回结束原因，TURN_END 由 _run_turn 统一写，避免重复
        return "interrupted"

    async def _drain_steering(self) -> Event | None:
        """每步之前看一眼 ctrl 队列。steering 就地并入历史（下一步生效），
        interrupt 返回给调用方结束本轮。"""
        while True:
            event = self.inbox.take_ctrl_nowait()
            if event is None:
                return None
            if event.type == EventType.USER_STEERING:
                self.history.append({"role": "user", "content": event.text})
                self._trajectory.append(event)
                continue
            self._trajectory.append(event)
            return event

    async def _interruptible(
        self,
        coro: Coroutine[Any, Any, Any],
        cancel: CancelSignal | None = None,
        grace: float = 0.1,
    ) -> Any:
        """跑一步，同时盯着 ctrl 队列。控制类事件一到就取消这一步。

        参数收协程而不是 Awaitable：协程是冷的，没被调度过，调度和取消
        都归这里；Task / Future 是热的，可能已经属于调用方，在这里取消
        会波及别人。

        取消分两步：先 set 信号让 IO 层自己停（关掉 HTTP 流），等 grace
        秒；还没停就 task.cancel() 强撕。只 cancel 不 set，被调用方来不
        及收尾，连接可能留在半开状态。

        只等信号位、不从队列里取，这样取消 watcher 时不会吞掉事件。
        """
        task = asyncio.create_task(coro, name=f"step:{self.session_id}")
        watcher = asyncio.create_task(self.inbox.wait_ctrl(), name="ctrl-watcher")
        try:
            done, _ = await asyncio.wait(
                {task, watcher}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            watcher.cancel()

        if task in done:
            return await task

        if cancel is not None:
            cancel.set()
            await asyncio.wait({task}, timeout=grace)

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        event = self.inbox.take_ctrl_nowait() or Event(
            type=INTERRUPT, session_id=self.session_id, payload={}, ctrl=True
        )
        raise StepInterrupted(event)

    async def _emit(
        self, event: Event, *, turn_id: str | None = None, step_id: str | None = None
    ):
        result = await self._bus.emit(event)
        self._trajectory.append(
            result.event, turn_id=turn_id, step_id=step_id, decisions=result.decisions
        )
        return result


def _safe_json(text: str) -> dict[str, Any]:
    import json

    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {"value": value}
    except Exception:
        return {"raw": text}
