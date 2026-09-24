"""总线：inbound 同步投递（一字不改），outbound 按声明分派订阅者。

总线只有一件事：**事件到达消费者**。下行 publish 旁路直达，上行 emit 分派
订阅者——没有治理、没有裁决、没有返回值。治理发生在 agent 的工具执行路径
上（governance.py 的 Governor，准入检查），审批是工具自己的声明
（Tool.requires_approval）；订阅的约束由事件类声明（HANDLER_SHAPE），
投递方式由消费者声明（邮箱型 offer / await 型 handler）。
"""

from __future__ import annotations

import logging

from .events import (
    Mailbox,
    Event,
    Sink,
    Subscription,
    validate_subscription,
)

logger = logging.getLogger(__name__)


class EventBus:
    """两条方向：inbound 旁路直达，outbound 分派订阅者。"""

    def __init__(self) -> None:
        self._sinks: dict[str, Sink] = {}
        self._subs: list[Subscription] = []

    # -------------------------------------------------- 注册

    def register(self, agent_id: str, sink: Sink) -> None:
        """登记 agent 的入站投递函数：publish 按 id 找到它。"""
        self._sinks[agent_id] = sink

    def subscribe(self, sub: Subscription) -> Subscription:
        """订阅进门：校验后登记。

        进门执行的唯一检查是**绑定形成处的通用类型检查**——handler 满足
        事件类声明的 HANDLER_SHAPE，对所有事件类型永远是同一条规则；
        具体哪个类型要求什么形状，声明在事件子类上。新增有约束的事件类型
        = 加一个子类 + 登记一行，总线零改动。
        """
        validate_subscription(sub)
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

    async def emit(self, event: Event) -> None:
        """分派一个事件：按各订阅者声明的方式送达，没有返回值。

        邮箱型（提供 offer）走 offer（微秒级，热路径只做缓冲）；其余 handler
        直接 await（契约：微秒级只做接收）。要不要拦截某个事件，不是总线的
        问题——治理在 agent 的工具执行路径上（governance.py）。
        """
        for sub in self._subs:
            if not sub.matches(event.type):
                continue
            try:
                if isinstance(sub.handler, Mailbox):
                    sub.handler.offer(event)
                else:
                    await sub.handler(event)
            except Exception as exc:  # noqa: BLE001 - 观测者失败与 agent 无关
                logger.warning("观测者 %s 处理 %s 失败：%r", sub.name, event.type, exc)
