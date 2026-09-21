"""总线：inbound 同步投递（一字不改），outbound 异步分发 + QoS + 治理 + 落盘。

emit 的四个步骤，顺序就是语义：

1. **治理**（拦截型，串行有预算）：能否决、能改写，也能说“这得问人”（ASK）。
   裁决跟着事件进 log。
2. **落盘**：seq 在这里分配（落盘即编号）。被否决的事件照样落盘——
   “有人试图做、被拒了”也是事实，事后要答得出“这个工具为什么没执行”。
3. **判决**：deny（有人否了）和 ask（要等人答复）都不往下投递——这条事件本来
   就没被放行。区别在 agent 拿到 EmitResult 之后做什么：deny 到此为止，ask 去等。
4. **投递**：按 lane 入各自的队列，**emit 不等订阅者**。每条 lane 有自己的
   worker，慢订阅者只能拖慢自己那条道。

治理者只能是“当场就能给出答案”的东西（查表、正则、写审计），因为它在 emit 的
调用栈里跑，预算以毫秒计。要等人的那半不在总线里：agent 发出 `approval_required`，
然后自己 await 一个 future，答复由 inbound 的 `user_approval` 直接 resolve 它——
答复不进收件箱，因为等在那一头的不是队列。

state 道满了会 `await put`——生命周期事件不可丢，宁可让 loop 慢下来（背压回
生产者）；stream 道满了直接丢最新并计数：token 少一帧只是屏幕少一个字。

**seq 的语义要说准**：seq 是**落盘顺序**，不是 emit 的调用顺序——emit 先
await 治理（第 1 步），有匹配的拦截者时会真让出，此窗口内别的 task 的事件
会先落盘先拿号。两个推论：

- 全局 log 里 seq 与 ts（构造时刻）可能逆序：**回放排序一律用 seq，不用 ts**。
- 会话内因果不破：同一 session 只有一个 worker，turn 内的 emit 全部 await
  串行，同一 session 不会有第二个 emit 同时在飞——按 session 过滤后，
  seq 就是事件发生顺序（test_seq_is_persist_order_not_call_order 钉死）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from .events import (
    ASK,
    DENY,
    FAIL_CLOSED,
    INTERCEPT,
    MODIFY,
    OBSERVE,
    STATE,
    STREAM,
    Decision,
    EmitResult,
    Event,
    Sink,
    Subscription,
    lane_of,
)
from .persistence import EventLog

logger = logging.getLogger(__name__)


class Lane:
    """一条道：自己的队列、自己的 worker、自己的丢弃策略。"""

    def __init__(self, name: str, maxsize: int, drop_when_full: bool) -> None:
        self.name = name
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self.drop_when_full = drop_when_full
        self.delivered = 0
        self.dropped = 0
        self.task: asyncio.Task[None] | None = None

    @property
    def depth(self) -> int:
        return self.queue.qsize()


class EventBus:
    """两条方向，四条道里的两条（inbound 一条，outbound 两条）。"""

    def __init__(
        self,
        log: EventLog | None = None,
        *,
        state_size: int = 1024,
        stream_size: int = 64,
        intercept_budget_ms: float = 50.0,
    ) -> None:
        self.log = log
        self._sinks: dict[str, Sink] = {}
        self._subs: list[Subscription] = []
        self._lanes: dict[str, Lane] = {
            STATE: Lane(STATE, state_size, drop_when_full=False),
            STREAM: Lane(STREAM, stream_size, drop_when_full=True),
        }
        self._budget_ms = intercept_budget_ms
        self._seq = 0  # 没有 log 时的进程内编号
        # record 的异步投递任务（落盘是同步的，投递是异步的）：drain 要一起等
        self._pending: set[asyncio.Task[None]] = set()

    # -------------------------------------------------- 注册

    def register(self, agent_id: str, sink: Sink) -> None:
        """登记 agent 的入站投递函数：publish 按 id 找到它。"""
        self._sinks[agent_id] = sink

    def subscribe(self, sub: Subscription) -> Subscription:
        self._subs.append(sub)
        return sub

    # -------------------------------------------------- inbound：命令（不动）

    def publish(self, event: Event, to: str) -> None:
        """同步投递到目标 agent，立即返回。**本 stage 一行没改**：

        下行（命令）低频、不可丢、要排队——它和上行根本不是一回事，
        强行统一只会两边都别扭。
        """
        sink = self._sinks.get(to)
        if sink is None:
            raise KeyError(f"没有这个 agent：{to!r}")
        sink(event)

    # -------------------------------------------------- outbound：事件

    async def emit(self, event: Event) -> EmitResult:
        """agent 在循环里发出一个事件。不等订阅者，只等治理和落盘。"""
        decisions, current = await self._intercept(event)
        current = self._persist(current, decisions)
        # deny：有人否了；ask：有人说“这得问人”。两种都不投递（这条事件没被放行），
        # 但都落盘了——裁决就在记录里，事后能答“为什么没执行”“谁要求问的”。
        if any(d.action in (DENY, ASK) for d in decisions):
            return EmitResult(False, current, decisions)
        await self._deliver(current)
        return EmitResult(True, current, decisions)

    def record(self, event: Event) -> Event:
        """同步落一条**已经在发生的事实**（不治理，但会异步补给观察者）。

        存在的理由只有一个：inbound 的旁路是同步的（`publish` → `sink`，不能 await），
        而"答复来了但没人等它"这种事必须留痕——一次确认的输入不能因为没人在等就消失。
        没有这条同步入口，那个事实就只能落在某个内存变量里，回放时看不见。

        代价说清楚（所以它只给异常路径用，正常事件仍然只能走 `emit`）：
        - **不过治理**：既成事实没必要也没法拦。
        - **落盘是同步的，投递是异步的**：seq 按因果顺序当场拿到（这一点很要紧，
          不能让"迟到的答复"拿到一个更晚的号），观察者稍后才看到它。
        """
        recorded = self._persist(event, ())
        try:
            task = asyncio.get_running_loop().create_task(self._deliver(recorded))
        except RuntimeError:
            return recorded  # 不在事件循环里：只落盘（同步脚本 / 收尾阶段）
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return recorded

    async def _intercept(self, event: Event) -> tuple[tuple[Decision, ...], Event]:
        matched = [s for s in self._subs if s.mode == INTERCEPT and s.matches(event.type)]
        if not matched:
            return (), event
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._budget_ms / 1000.0
        decisions: list[Decision] = []
        current = event
        for sub in sorted(matched, key=lambda s: s.order):
            remaining = deadline - loop.time()
            decision = await self._call_interceptor(sub, current, remaining)
            decisions.append(decision)
            if decision.action == DENY:
                return tuple(decisions), current
            if decision.action == ASK:
                # 有人说“这得问人”：后面的拦截者不用再问了，agent 会去等答复。
                # 前面改写过的参数留在 current 上——问人的人看到的该是改写后的。
                return tuple(decisions), current
            if decision.action == MODIFY and decision.patch:
                current = current.with_patch(decision.patch)
        return tuple(decisions), current

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

    @staticmethod
    def _on_failure(sub: Subscription, reason: str) -> Decision:
        """拦截者自己出问题时怎么办，注册时就说清楚，运行时不猜。"""
        if sub.on_failure == FAIL_CLOSED:
            return Decision.deny(sub.name, reason)
        return Decision.allow(sub.name, reason)

    def _persist(self, event: Event, decisions: tuple[Decision, ...]) -> Event:
        record = {
            "type": event.type,
            "session": event.session_id,
            "corr": event.correlation_id,
            "ts": round(event.ts, 4),
            "payload": dict(event.payload),
            "decisions": [asdict(d) for d in decisions],
        }
        seq = self.log.append(record) if self.log is not None else self._next_seq()
        return Event(
            type=event.type,
            session_id=event.session_id,
            payload=event.payload,
            seq=seq,
            correlation_id=event.correlation_id,
            ts=event.ts,
        )

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def _deliver(self, event: Event) -> None:
        lane = self._lanes[lane_of(event.type)]
        self._ensure_workers()
        if lane.drop_when_full:
            try:
                lane.queue.put_nowait(event)
            except asyncio.QueueFull:
                lane.dropped += 1  # 可丢：丢的是 token 增量，不是事实
            return
        # 不可丢：满了就背压回生产者（宁可让 loop 慢下来，也不丢生命周期事件）
        await lane.queue.put(event)

    def _ensure_workers(self) -> None:
        for lane in self._lanes.values():
            if lane.task is None or lane.task.done():
                lane.task = asyncio.create_task(
                    self._lane_loop(lane), name=f"lane-{lane.name}"
                )

    async def _lane_loop(self, lane: Lane) -> None:
        while True:
            event = await lane.queue.get()
            try:
                for sub in self._subs:
                    if sub.mode != OBSERVE or not sub.matches(event.type):
                        continue
                    await self._notify(sub, event)
                lane.delivered += 1
            finally:
                lane.queue.task_done()

    @staticmethod
    async def _notify(sub: Subscription, event: Event) -> None:
        """转发给观测者。**没有超时，这是声明过的边界**：handler 挂死会挂住整条
        lane worker，而 lane 全局共享——所有 session 的这条道连坐（拦截者有
        wait_for + 预算，观测者没有）。"慢"有界、"死"无界；"观测者超时算不算
        已消费"是新语义，留给需求真出现的那天（04 章降级项）。
        """
        try:
            await sub.handler(event)
        except Exception as exc:  # noqa: BLE001 - 观测者失败与 agent 无关
            logger.warning("观测者 %s 处理 %s 失败：%r", sub.name, event.type, exc)

    # -------------------------------------------------- 收尾与观测

    async def drain(self, timeout: float = 5.0) -> None:
        """等两条道把队列里的东西发完（demo / 测试收尾用）。"""
        if self._pending:
            await asyncio.wait(set(self._pending), timeout=timeout)
        for lane in self._lanes.values():
            if lane.task is None:
                continue
            try:
                await asyncio.wait_for(lane.queue.join(), timeout)
            except asyncio.TimeoutError:
                logger.warning("lane %s 在 %.1fs 内没排空", lane.name, timeout)

    @property
    def last_seq(self) -> int:
        return self.log.last_seq if self.log is not None else self._seq

    def stats(self) -> dict[str, Any]:
        return {
            "last_seq": self.last_seq,
            "lanes": {
                name: {
                    "queued": lane.depth,
                    "delivered": lane.delivered,
                    "dropped": lane.dropped,
                }
                for name, lane in self._lanes.items()
            },
        }
