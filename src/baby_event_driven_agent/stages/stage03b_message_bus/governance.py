"""治理：agent 工具执行前的准入关卡——当场判"这个调用现在能不能执行"。

治理不是总线的事：总线只管事件到达消费者；"工具有没有资格执行"发生在
agent 的工具执行路径上，和工具自己的审批声明（`Tool.requires_approval`）
是同一位置的同一类关卡。Governor 把一组当场能判的规则（查表、正则）组织
成 order 串行、共享预算的一条链：DENY 短路，MODIFY 改写参数，失败按规则
注册时声明的 open/closed。要等人的那半不在这里——那走人工确认流程，
触发条件也是工具自己的声明。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .events import DENY, MODIFY, Decision

logger = logging.getLogger(__name__)

FAIL_OPEN = "open"  # 规则自己失败 / 超时 → 放行
FAIL_CLOSED = "closed"  # 规则自己失败 / 超时 → 拒绝


@dataclass(frozen=True)
class Rule:
    """一条当场能判的治理规则。失败怎么办（on_failure）注册时说清楚，运行时不猜。"""

    name: str  # 进裁决署名，事后答得出"谁拒的"
    handler: Callable[[str, str], Awaitable[Decision | None]]  # (工具名, 参数) → 裁决
    order: int = 100  # 串行顺序，小的先跑
    on_failure: str = FAIL_OPEN


@dataclass(frozen=True)
class Verdict:
    """一次准入检查的结果：放行与否、（可能被改写过的）参数、全部裁决。"""

    allowed: bool
    arguments: str
    decisions: tuple[Decision, ...]


class Governor:
    """规则链：按 order 串行、共享预算、失败按注册时声明的 open/closed。"""

    def __init__(self, *, budget_ms: float = 50.0) -> None:
        self.budget_ms = budget_ms
        self._rules: list[Rule] = []

    def add(self, rule: Rule) -> None:
        """挂一条规则（agent 构造时或运行中，都是这一口）。"""
        self._rules.append(rule)

    async def check(self, name: str, arguments: str) -> Verdict:
        """跑一遍规则链：DENY 短路，MODIFY 改写参数，全部裁决带回去。"""
        if not self._rules:
            return Verdict(True, arguments, ())  # 快路径：没有规则，连 await 都没有
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.budget_ms / 1000.0
        decisions: list[Decision] = []
        current = arguments
        for rule in sorted(self._rules, key=lambda r: r.order):
            remaining = deadline - loop.time()
            decision = await self._call(rule, name, current, remaining)
            decisions.append(decision)
            if decision.action == DENY:
                return Verdict(False, current, tuple(decisions))
            if decision.action == MODIFY and decision.patch:
                current = str(decision.patch.get("arguments", current))
        return Verdict(True, current, tuple(decisions))

    async def _call(
        self, rule: Rule, name: str, arguments: str, remaining: float
    ) -> Decision:
        if remaining <= 0:
            return self._on_failure(rule, "阶段预算已耗尽")
        try:
            decision = await asyncio.wait_for(rule.handler(name, arguments), remaining)
        except asyncio.TimeoutError:
            return self._on_failure(rule, "规则超时")
        except Exception as exc:  # noqa: BLE001 - 规则炸了也不能拖垮 loop
            return self._on_failure(rule, f"规则异常：{exc!r}")
        return decision or Decision.allow(rule.name)

    @staticmethod
    def _on_failure(rule: Rule, reason: str) -> Decision:
        """规则自己出问题时怎么办，注册时就说清楚，运行时不猜。"""
        if rule.on_failure == FAIL_CLOSED:
            return Decision.deny(rule.name, reason)
        return Decision.allow(rule.name, reason)
