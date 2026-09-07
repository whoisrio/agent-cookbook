"""事件总线与 agent 收件箱。

总线只做两件事：把命令路由到某个 agent 的 inbox，把 agent emit 的事件
扇出给订阅者。它不存事件（存了就和 agent 的轨迹变成两份真相），也不做
业务判断（"这个任务给谁"是 orchestrator 的事，不是总线的）。

inbox 才是 agent 的门。两个队列：
- ctrl：控制类事件，无界、优先取。取消和插队指令不能因为队列满而进不来。
- normal：普通事件，有界。满了就拒绝并告诉发布者，绝不静默丢弃。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Callable

from .events import (
    ALLOW,
    DENY,
    INTERCEPT,
    MAX_HOP,
    MODIFY,
    FAIL_CLOSED,
    Decision,
    EmitResult,
    Event,
    Subscription,
)

logger = logging.getLogger(__name__)


class InboxFull(Exception):
    """普通队列满了。发布者必须看到这个异常，不能当没事发生。"""


class AgentInbox:
    """agent 的门：ctrl 队列（无界、优先）+ 普通队列（有界、背压）。

    用 deque 而不是 asyncio.Queue：Queue 只有 get() 一个出口，东西取出
    来就不能反悔。两条队列做优先级必然要竞速两个 getter，实测（3.13）
    输家手里已经取出的事件会被静默丢掉，500/500 全丢。deque 把"唤醒"和
    "取"拆成两步：Event 只管唤醒，取是同步的 popleft，取之前怎么取消都
    不会丢事件。两个信号位分开，唤醒和取用互不干扰。
    """

    def __init__(self, maxsize: int = 256) -> None:
        self._ctrl: deque[Event] = deque()
        self._normal: deque[Event] = deque()
        self._maxsize = maxsize
        self._ctrl_signal = asyncio.Event()
        self._any_signal = asyncio.Event()

    def put(self, event: Event) -> None:
        if event.ctrl:
            self._ctrl.append(event)
            self._ctrl_signal.set()
        else:
            if len(self._normal) >= self._maxsize:
                raise InboxFull(f"inbox 已满，事件被拒绝：{event.type}")
            self._normal.append(event)
        self._any_signal.set()

    async def get(self) -> Event:
        while True:
            if self._ctrl:
                return self._take_ctrl()
            if self._normal:
                return self._take_normal()
            await self._any_signal.wait()

    async def wait_ctrl(self) -> None:
        """等到 ctrl 队列里有东西为止，只等不取。

        while 不是多余的：信号被 set 之后、这个 watcher 真正被调度起来之
        前，事件可能已经被别人取走（take_ctrl_nowait 之后 _refresh 会把
        信号 clear 掉）。少了这个循环，await 回来时队列可能又是空的。
        """
        while not self._ctrl:
            await self._ctrl_signal.wait()

    def take_ctrl_nowait(self) -> Event | None:
        return self._take_ctrl() if self._ctrl else None

    def __len__(self) -> int:
        return len(self._ctrl) + len(self._normal)

    def _take_ctrl(self) -> Event:
        event = self._ctrl.popleft()
        self._refresh()
        return event

    def _take_normal(self) -> Event:
        event = self._normal.popleft()
        self._refresh()
        return event

    def _refresh(self) -> None:
        if not self._ctrl:
            self._ctrl_signal.clear()
        if not self._ctrl and not self._normal:
            self._any_signal.clear()


class EventBus:
    def __init__(self) -> None:
        self._inboxes: dict[str, AgentInbox] = {}
        self._subs: list[tuple[Subscription, Callable[[Event], object]]] = []

    # ---- 注册 ----

    def register(self, agent_id: str, inbox: AgentInbox | None = None) -> AgentInbox:
        inbox = inbox or AgentInbox()
        self._inboxes[agent_id] = inbox
        return inbox

    def subscribe(self, sub: Subscription) -> Subscription:
        self._subs.append((sub, sub.handler))
        return sub

    def inbox_of(self, agent_id: str) -> AgentInbox:
        return self._inboxes[agent_id]

    # ---- inbound：命令路由 ----

    def publish(self, event: Event, to: str | None = None) -> None:
        """把命令送进 agent 的 inbox。to 为 None 时广播给所有 agent。"""
        targets = [self._inboxes[to]] if to else list(self._inboxes.values())
        if not targets:
            raise KeyError(f"没有可投递的 agent：{to!r}")
        for inbox in targets:
            inbox.put(event)

    # ---- outbound：扇出 ----

    async def emit(self, event: Event, budget_ms: float = 50.0) -> EmitResult:
        """agent 在循环里发出一个事件，返回它是否被放行、被改写过什么。

        拦截类：按 order 串行，共用一个阶段预算（不是每人各算一份），
                遇到 deny 立即短路，每个裁决都返回给调用方写进轨迹。
        通知类：并发 fire-and-forget，谁抛异常只记日志，绝不阻塞 agent。
        """
        if event.hop >= MAX_HOP:
            return EmitResult(allowed=True, event=event, decisions=())

        # 先快照，防止处理途中插件热挂载导致同一次 emit 行为不一致
        matched = tuple(s for s, _ in self._subs if s.matches(event.type))
        decisions: list[Decision] = []
        current = event

        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget_ms / 1000.0

        for sub in sorted(
            (s for s in matched if s.mode == INTERCEPT), key=lambda s: s.order
        ):
            remaining = deadline - loop.time()
            decision = await self._call_interceptor(sub, current, remaining)
            decisions.append(decision)
            if decision.action == DENY:
                return EmitResult(False, current, tuple(decisions))
            if decision.action == MODIFY and decision.patch:
                current = current.with_patch(decision.patch)

        for sub in matched:
            if sub.mode == INTERCEPT:
                continue
            asyncio.create_task(self._notify(sub, current))

        return EmitResult(True, current, tuple(decisions))

    async def _call_interceptor(
        self, sub: Subscription, event: Event, remaining: float
    ) -> Decision:
        if remaining <= 0:
            return self._on_failure(sub, "阶段预算已耗尽")
        try:
            decision = await asyncio.wait_for(sub.handler(event), remaining)
        except asyncio.TimeoutError:
            return self._on_failure(sub, "拦截者超时")
        except Exception as exc:  # noqa: BLE001 - 拦截者炸了也不能拖垮 loop
            return self._on_failure(sub, f"拦截者异常：{exc!r}")
        return decision or Decision.allow(sub.name)

    def _on_failure(self, sub: Subscription, reason: str) -> Decision:
        if sub.on_failure == FAIL_CLOSED:
            return Decision.deny(sub.name, reason)
        return Decision.allow(sub.name, reason)

    async def _notify(self, sub: Subscription, event: Event) -> None:
        try:
            await sub.handler(event)
        except Exception as exc:  # noqa: BLE001 - 观测者失败与 agent 无关
            logger.warning("观测者 %s 处理 %s 失败：%r", sub.name, event.type, exc)

    async def drain(self) -> None:
        """等 fire-and-forget 的通知跑完（demo / 测试收尾用）。"""
        pending = [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        if pending:
            await asyncio.wait(pending, timeout=2.0)
