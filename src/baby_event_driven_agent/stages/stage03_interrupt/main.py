"""Stage 3 演示：打断在飞的一步，以及掐完之后"结束"还是"原地转向"。

需要仓库根 .env 里的 OPENAI_API_KEY / OPENAI_API_BASE / OPENAI_MODEL
（环境变量可覆盖，比如临时换模型：OPENAI_MODEL=qwen3.7-flash stage03-demo）。

路基沿用 stage02：inbound 的 publish 是**同步**的（`bus.publish(event, to=...)`，
投进目标 agent 的收件箱就返回），outbound 的 agent 事件走 emit 扇出。
用户输入和中断信号都从 inbound 进来——中断只是另一种类型的命令，
不额外订阅、也不经过 UI handler。

四个动作，每个都在屏幕上说明"这一段在演示什么"：
1. 完整一轮：worker 空闲 → 投递即开新 turn，走完"工具调用 → 流式回答"，
   先确立 agent 正常可用。
2. stop（停）：等它**真的发起工具调用**（tool_call_started，此刻 step 正在飞）
   再按停止 → 在飞的那一步被掐掉，turn 以 interrupted 收尾，尾部补一条自描述的
   中断标记；下一轮模型不会再翻出这条没答的问题重答。
3. redirect（转向）：**和动作 2 同一个时机**，但意图是"原地转向" → turn 不结束，
   已经观测到的半成品留在 history 里，纠正作为一条 user 消息，同一个 turn 里重发。
4. 中断之后照常可用：worker 没死、history 完好，新问题开新 turn。

什么时候是 stop、什么时候是 redirect —— **由用户选，agent 不猜**：

    用户意图            信封                             收尾
    ───────────────────────────────────────────────────────────────
    停（问错了/等不及）   {"intent": "stop"}              掐掉 + 封口，turn 结束
    转向（改主意、接着干）{"intent": "redirect",          掐掉 + 补纠正 user，
                        "text": "…纠正内容…"}            turn 不结束，同 turn 重发

"落在哪"只决定**收尾形状**（补中断标记 / 补 assistant 占位 / 补纠正 user），
不决定意图。动作 2 和 3 故意用同一个时机、同一格（工具调用已在飞），唯一区别就是
信封里的 intent —— 想说明"同一格，两个意图，两种收尾"。

每一行都带**行首标签**，角色一眼分得开：

    用户 │ 用户说了什么
    思考 │ assistant 的 thinking（暗色流）
    LLM(回答) │ assistant 的可见输出（亮蓝流）
    LLM(要求执行工具) │ 模型要求调用的工具（绿色）
    执行工具 │ 工具真实执行与结果（绿色）
    系统 │ 生命周期：掐掉 / 边界命中 / turn 结束（按 intent 上色）
    说明 │ 旁白，只解释这一段在演示什么（灰色缩进，不属于对话）

    python -m baby_event_driven_agent.stages.stage03_interrupt
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .agent import Agent
from .events import Event, EventBus, SessionLog, t
from .llm import RealLLM

# ---------------------------------------------------------------- 屏幕上色
# 与 stage01 / stage02 同一套底子：思考暗色、正文亮蓝、工具/生命周期绿色、
# 用户输入黄色。本章多出两类用户动作，各给一色（命中行沿用同色，谁按的、
# 命中在哪一步一眼对上）：
#   亮红 = 停（intent=stop，掐掉就结束）
#   洋红 = 转向（intent=redirect，掐掉后原地重发）
# 旁白单独用灰色，且只缩进不出现在对话流里——不再和"思考"共用一个暗色。
DIM = "\033[2m"  # 思考内容
GREY = "\033[90m"  # 旁白说明 / 收尾信息
BLUE = "\033[94m"  # assistant 可见输出
GREEN = "\033[32m"  # 工具调用与结果 / 正常 turn 收尾
YELLOW = "\033[33m"  # 用户输入
RED = "\033[91m"  # 停（stop）及其命中
MAGENTA = "\033[35m"  # 转向（redirect）及其命中
BOLD = "\033[1m"
RESET = "\033[0m"

INTENT_COLOR = {"stop": RED, "redirect": MAGENTA}


def line(label: str, color: str, text: str) -> None:
    """对话流里的一行：`[时间] 标签 │ 内容`。"""
    print(f"\n{BOLD}{color}[{t():5.2f}s] {label} │ {RESET}{color}{text}{RESET}")


def stream_head(label: str, color: str) -> None:
    """流式输出的行首（后面跟着同一颜色的增量）。"""
    print(f"\n{BOLD}{color}[{t():5.2f}s] {label} │ {RESET}{color}", end="")


def note(text: str) -> None:
    """旁白：缩进 + 灰色 + "说明"标签，明确不在对话流里。"""
    print(f"{GREY}       说明 │ {text}{RESET}")


def banner(n: int, title: str, what: str) -> None:
    print(f"\n{BOLD}── 动作 {n}：{title} ──{RESET}")
    note(what)


def brief(text: str, limit: int = 140) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


async def _wait_any(*events: asyncio.Event) -> None:
    """等其中任意一个先发生。"""
    waiters = [asyncio.ensure_future(e.wait()) for e in events]
    await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    for waiter in waiters:
        waiter.cancel()


async def main() -> None:
    # session log 落在包级 sessions/ 目录，按 stage 分目录
    sessions_dir = Path(__file__).resolve().parents[2] / "sessions" / "stage03"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    log_path = str(sessions_dir / "session.jsonl")
    bus = EventBus()
    log = SessionLog(log_path)
    agent = Agent(bus, log, RealLLM())

    turn_done = asyncio.Event()
    tool_started = asyncio.Event()
    # 最近一次用户动作的意图：命中行和 turn 收尾行都用它的颜色
    pressed = ["stop"]
    # 上一条流式增量属于哪路：思考/正文切换时换行重新起标签
    last_kind = [""]

    async def ui_thinking(e: Event) -> None:
        if last_kind[0] != "thinking":
            stream_head("思考", DIM)
            last_kind[0] = "thinking"
        print(f"{e.payload['text']}", end="", flush=True)

    async def ui_delta(e: Event) -> None:
        if last_kind[0] != "text":
            stream_head("LLM(回答)", BLUE)
            last_kind[0] = "text"
        print(f'{e.payload["text"]}', end="", flush=True)

    async def ui_reply(e: Event) -> None:
        msg = e.payload["message"]
        last_kind[0] = ""
        if msg.get("tool_calls"):
            calls = ", ".join(
                f"{c['function']['name']}({c['function']['arguments']})"
                for c in msg["tool_calls"]
            )
            line("LLM(要求执行工具)", GREEN, f"→ {calls}")
        else:
            line("LLM(回答)", GREEN, "回答完毕，本步不再调工具")

    async def ui_tool_result(e: Event) -> None:
        p = e.payload
        if p.get("skipped"):
            line("执行工具", RED, f"← {p['name']} 未执行（{p['result']}）")
        else:
            line("执行工具", GREEN, f"← {p['name']} 结果：{brief(p['result'])}")

    async def ui_tool_start(e: Event) -> None:
        tool_started.set()

    async def ui_step_cancelled(e: Event) -> None:
        last_kind[0] = ""
        intent = e.payload.get("intent", "stop")
        line(
            "系统",
            INTENT_COLOR.get(intent, GREEN),
            f"已掐掉在飞的那一步（intent={intent}，step_cancelled）",
        )

    async def ui_turn_interrupted(e: Event) -> None:
        last_kind[0] = ""
        intent = e.payload.get("intent", "stop")
        line(
            "系统",
            INTENT_COLOR.get(intent, GREEN),
            f"边界命中：step 没在飞，turn 在这里"
            f"{'转向' if intent == 'redirect' else '收尾'}"
            f"（intent={intent}，turn_interrupted）",
        )

    async def ui_turn_end(e: Event) -> None:
        reason = e.payload.get("reason", "")
        color = INTENT_COLOR.get(pressed[0], GREEN) if reason == "interrupted" else GREEN
        line("系统", color, f"turn 结束（reason={reason}）")
        turn_done.set()

    bus.subscribe("agent_thinking", ui_thinking)
    bus.subscribe("agent_delta", ui_delta)
    bus.subscribe("agent_reply", ui_reply)
    bus.subscribe("tool_result", ui_tool_result)
    bus.subscribe("tool_call_started", ui_tool_start)
    bus.subscribe("step_cancelled", ui_step_cancelled)
    bus.subscribe("turn_interrupted", ui_turn_interrupted)
    bus.subscribe("turn_end", ui_turn_end)

    print(f"{BOLD}Stage 3：打断在飞的一步 —— interrupt（停）与 redirect（转向）{RESET}")

    # ------------------------------------------------------------- 动作 1
    banner(
        1,
        "完整一轮",
        "worker 空闲，投递即开新 turn：工具调用 + 流式回答，先确立 agent 正常可用。",
    )
    line("用户", YELLOW, "保温杯还有库存吗")
    bus.publish(
        Event("user_input", "A", {"text": "保温杯还有库存吗"}), to=agent.agent_id
    )
    await turn_done.wait()
    turn_done.clear()

    # ------------------------------------------------------------- 动作 2
    banner(
        2,
        "stop（intent=stop）：掐掉在飞的一步，turn 结束",
        "等它真的发起工具调用（tool_call_started，step 正在飞）再按停止。"
        "预期：一行「已掐掉在飞的那一步」+ turn 以 interrupted 收尾。"
        "下一动手势完全一样，只有信封里的 intent 不同。",
    )
    line("用户", YELLOW, "报销有什么规定")
    bus.publish(
        Event("user_input", "A", {"text": "报销有什么规定"}), to=agent.agent_id
    )
    tool_started.clear()
    await _wait_any(tool_started, turn_done)
    if tool_started.is_set() and not turn_done.is_set():
        line("用户", RED, "按下停止（intent=stop）")
        pressed[0] = "stop"
        bus.publish(Event("user_interrupt", "A", {"intent": "stop"}), to=agent.agent_id)
        await turn_done.wait()
    else:
        note("模型没调工具就答完了，没赶上中断窗口")
    turn_done.clear()

    # ------------------------------------------------------------- 动作 3
    banner(
        3,
        "redirect（intent=redirect）：掐掉后原地转向",
        "和动作 2 同一时机、同一格，只差 intent。预期：turn 不结束，半成品留在"
        "history，纠正作为一条 user 消息，同一个 turn 里重发。",
    )
    line("用户", YELLOW, "会议室怎么预订")
    bus.publish(
        Event("user_input", "A", {"text": "会议室怎么预订"}), to=agent.agent_id
    )
    tool_started.clear()
    await _wait_any(tool_started, turn_done)
    if tool_started.is_set() and not turn_done.is_set():
        line("用户", MAGENTA, "改主意（intent=redirect）：先别查会议室了，改成查报销规定")
        pressed[0] = "redirect"
        # 中断也是 inbound 命令：和用户输入同一个收件地址，只是类型是
        # user_interrupt，投递函数不会把它塞进收件箱排队。
        bus.publish(
            Event(
                "user_interrupt",
                "A",
                {"intent": "redirect", "text": "先别查会议室了，改成查报销规定"},
            ),
            to=agent.agent_id,
        )
    else:
        note("没赶上窗口，这一轮按普通问答结束")
    await turn_done.wait()
    turn_done.clear()

    # ------------------------------------------------------------- 动作 4
    banner(
        4,
        "中断之后照常可用",
        "worker 没死、history 完好：新问题开新 turn，正常答完。",
    )
    line("用户", YELLOW, "顺便说说VPN怎么申请")
    bus.publish(
        Event("user_input", "A", {"text": "顺便说说VPN怎么申请"}), to=agent.agent_id
    )
    await turn_done.wait()

    await agent.stop()

    print(f"\n{BOLD}── history 尾部（中断标记 / 纠正消息的实际长相）──{RESET}")
    for msg in agent.history["A"][-4:]:
        print(f"{GREY}  {json.dumps(msg, ensure_ascii=False)}{RESET}")
    line("系统", GREEN, "demo 结束")
    print(f"{GREY}  session log: {log_path}{RESET}")


if __name__ == "__main__":
    asyncio.run(main())


def cli() -> None:
    """[project.scripts] 入口：stage03-demo。"""
    asyncio.run(main())
