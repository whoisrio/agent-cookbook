"""Stage 2 演示：总线分方向 + 收件箱接管投递，followup 排队，steering 插话。

本章总线分了方向：inbound 的 publish 是同步的——投进目标 agent 的收件箱
立刻返回，不再 await handler；outbound 的 agent 事件改走 emit。

需要仓库根 .env 里的 OPENAI_API_KEY / OPENAI_API_BASE / OPENAI_MODEL
（环境变量可覆盖，比如临时换模型：OPENAI_MODEL=qwen3.7-flash stage02-demo）。

三个动作，插话时机是确定性的：
1. 问保温杯库存 → 完整一个 turn（search + 流式回答）。
2. 紧接着问报销规定 → 此刻 worker 空闲，作为 followup 立刻开新 turn；
   报销的信息不在第一轮检索结果里，模型必然发起 search。
3. 等报销这轮**真的发起工具调用**（tool_call_started 事件，此刻 step
   还在飞：后面还有增量、工具执行、下一个 step 边界）才插话——
   drain 必然捞到它，屏幕上会打出 ★ steering 生效 的标记。
   兜底：若模型没调工具直接答完（竞速输给 turn_end），插话如实
   打印为 followup 开新 turn——两种语义在屏幕上肉眼可辨。

呈现与 stage01 / stage03 同一套行首标签：用户 │、思考 │、LLM(回答) │、
LLM(要求执行工具) │、执行工具 │、系统 │。本章多出两类用户动作，各给一色，
时间线上直接认语义——洋红 = 插话（steering，想插进当前 turn），
青 = 追问（followup，排到当前 turn 之后）；★ 与降级行沿用这两色，
一眼看出插话最后落成了哪一种。

    python -m baby_event_driven_agent.stages.stage02_inbox_steering
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .agent import Agent
from .events import Event, EventBus, SessionLog, t
from .llm import RealLLM

# ---------------------------------------------------------------- 屏幕上色
# 与 stage01 / stage03 同一套底子（思考暗色、正文亮蓝、工具/生命周期绿色、
# 用户输入黄色），本章多出两类用户动作，各给一色：
#   洋红 = 插话（steering，想插进当前 turn）
#   青   = 追问（followup，排到当前 turn 之后）
DIM = "\033[2m"  # 思考
GREY = "\033[90m"  # 收尾信息
BLUE = "\033[94m"  # LLM 可见输出
GREEN = "\033[32m"  # 工具调用与结果 / 生命周期
YELLOW = "\033[33m"  # 用户输入
MAGENTA = "\033[35m"  # 用户插话（及插话真的成了 steering）
CYAN = "\033[36m"  # 用户追问（及插话降级成 followup）
BOLD = "\033[1m"
RESET = "\033[0m"


def line(label: str, color: str, text: str) -> None:
    """对话流里的一行：`[时间] 标签 │ 内容`。"""
    print(f"\n{BOLD}{color}[{t():5.2f}s] {label} │ {RESET}{color}{text}{RESET}")


def stream_head(label: str, color: str) -> None:
    """流式输出的行首（后面跟着同一颜色的增量）。"""
    print(f"\n{BOLD}{color}[{t():5.2f}s] {label} │ {RESET}{color}", end="")


def brief(text: str, limit: int = 140) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


async def main() -> None:
    # session log 落在包级 sessions/ 目录，按 stage 分目录
    sessions_dir = Path(__file__).resolve().parents[2] / "sessions" / "stage02"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    log_path = str(sessions_dir / "session.jsonl")
    bus = EventBus()
    log = SessionLog(log_path)
    agent = Agent(bus, log, RealLLM())

    turn_done = asyncio.Event()
    tool_started = asyncio.Event()
    steering_seen = False
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
            line("LLM(回答)", GREEN, "回答完毕")

    async def ui_tool_result(e: Event) -> None:
        p = e.payload
        line("执行工具", GREEN, f"← {p['name']} 结果：{brief(p['result'])}")

    async def ui_tool_start(e: Event) -> None:
        tool_started.set()

    async def ui_steering(e: Event) -> None:
        nonlocal steering_seen
        steering_seen = True
        for text in e.payload["texts"]:
            # 插话真的被 drain 进当前 turn：沿用插话的洋红，一眼看出它成了 steering
            line(
                "系统",
                MAGENTA,
                f"★ steering 生效：「{text}」拼进当前 turn 的上下文，不开新 turn",
            )

    async def ui_turn_end(e: Event) -> None:
        turn_done.set()

    bus.subscribe("agent_thinking", ui_thinking)
    bus.subscribe("agent_delta", ui_delta)
    bus.subscribe("agent_reply", ui_reply)
    bus.subscribe("tool_result", ui_tool_result)
    bus.subscribe("tool_call_started", ui_tool_start)
    bus.subscribe("steering_consumed", ui_steering)
    bus.subscribe("turn_end", ui_turn_end)

    # 1. 第一问：worker 空闲，投递即开新 turn
    line("用户", YELLOW, "保温杯还有库存吗")
    bus.publish(Event("user_input", "A", {"text": "保温杯还有库存吗"}), to=agent.agent_id)
    await turn_done.wait()
    turn_done.clear()
    tool_started.clear()  # 第一轮的 search 也发过 tool_call_started，作废

    # 2. 第二问：紧接着发，worker 已空闲 → followup，立刻开新 turn。
    #    故意选报销——它的答案不在第一轮检索结果里，模型必然发起 search。
    line("用户", CYAN, "用户接着问：报销有什么规定")
    bus.publish(
        Event("user_input", "A", {"text": "报销有什么规定"}), to=agent.agent_id
    )

    # 3. 确定性插话：等报销这轮真的发起工具调用（step 正在飞）才发。
    #    但"发得早"不保证"被 steering 消化"——若它落在最后一个 drain 点
    #    之后（临界降级），worker 会在 turn 结束后把它当 followup 取走。
    #    两种结局屏幕上都可见：★ steering 生效 / 降级行。
    tool_wait = asyncio.ensure_future(tool_started.wait())
    turn_wait = asyncio.ensure_future(turn_done.wait())
    await asyncio.wait(
        {tool_wait, turn_wait}, return_when=asyncio.FIRST_COMPLETED
    )
    if tool_started.is_set() and not turn_done.is_set():
        line("用户", MAGENTA, "用户插话（此刻工具调用正在飞）：顺便说说VPN怎么申请")
    else:
        line("用户", MAGENTA, "用户插话（上一问已答完）：顺便说说VPN怎么申请")
    bus.publish(
        Event("user_input", "A", {"text": "顺便说说VPN怎么申请"}), to=agent.agent_id
    )
    turn_done.clear()  # 丢弃竞速遗留信号：等插话所属的下一个 turn_end
    await turn_done.wait()
    if not steering_seen:
        # 插话错过了最后一个 drain 点：turn 收尾没带上它，它降级为
        # followup，worker 正在跑它的 turn。不降级的另一条路（★）上面
        # 已经打出来了。这里绝不能直接 stop()——那会把还在收件箱里的
        # 插话连 worker 一起掐死。
        # 落成了 followup 就用追问的青：颜色本身把结局讲完了。
        line("系统", CYAN, "插话错过了 drain 窗口 → 降级为 followup，新 turn 消化")
        turn_done.clear()
        await turn_done.wait()

    await agent.stop()
    line("系统", GREEN, "demo 结束")
    print(f"{GREY}  session log: {log_path}{RESET}")


if __name__ == "__main__":
    asyncio.run(main())


def cli() -> None:
    """[project.scripts] 入口：stage02-demo。"""
    asyncio.run(main())
