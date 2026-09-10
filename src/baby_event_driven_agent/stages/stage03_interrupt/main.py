"""Stage 3 演示：用户中断——掐掉正在飞的一步，agent 活着。

需要仓库根 .env 里的 OPENAI_API_KEY / OPENAI_API_BASE / OPENAI_MODEL
（环境变量可覆盖，比如临时换模型：OPENAI_MODEL=qwen3.7-flash stage03-demo）。

三个动作：
1. 问保温杯库存 → 完整一个 turn（search + 流式回答），确立正常可用。
2. 紧接着问报销规定 → 等这轮真的发起工具调用（tool_call_started，
   step 正在飞）就发 user_interrupt——正在飞的 step 被取消，
   turn 以 step_cancelled 收尾，屏幕上打印"已停止"。
   兜底：若那一步抢在信号前跑完了（取消与完成竞速），如实打印
   "中断落空"——两种结局都可见。
3. 再问 VPN → 正常新 turn 完整回答，证明 worker 和 history 都活着。

    python -m baby_event_driven_agent.stages.stage03_interrupt
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .agent import Agent
from .events import Event, EventBus, SessionLog, t
from .llm import RealLLM


async def main() -> None:
    # session log 落在包级 sessions/ 目录，按 stage 分目录
    sessions_dir = Path(__file__).resolve().parents[2] / "sessions" / "stage03"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    log_path = str(sessions_dir / "session.jsonl")
    bus = EventBus()
    log = SessionLog(log_path)
    agent = Agent(bus, log, RealLLM())
    bus.subscribe("user_input", agent.on_user_input)
    bus.subscribe("user_interrupt", agent.on_interrupt)

    turn_done = asyncio.Event()
    tool_started = asyncio.Event()
    step_cancelled = asyncio.Event()

    async def ui_delta(e: Event) -> None:
        print(e.payload["text"], end="", flush=True)

    async def ui_reply(e: Event) -> None:
        msg = e.payload["message"]
        if msg.get("tool_calls"):
            calls = ", ".join(
                f"{c['function']['name']}({c['function']['arguments']})"
                for c in msg["tool_calls"]
            )
            print(f"\n[{t():5.2f}s] (A) → 工具调用：{calls}")
        else:
            print(f"\n[{t():5.2f}s] (A) —— 回答完毕")

    async def ui_tool_start(e: Event) -> None:
        tool_started.set()

    async def ui_step_cancelled(e: Event) -> None:
        step_cancelled.set()
        print(f"\n[{t():5.2f}s] (A) 已停止：正在飞的那一步被取消，turn 结束")

    async def ui_turn_end(e: Event) -> None:
        turn_done.set()

    bus.subscribe("agent_delta", ui_delta)
    bus.subscribe("agent_reply", ui_reply)
    bus.subscribe("tool_call_started", ui_tool_start)
    bus.subscribe("step_cancelled", ui_step_cancelled)
    bus.subscribe("turn_end", ui_turn_end)

    # 1. 第一问：完整一个 turn，确立 agent 正常可用
    print(f"[{t():5.2f}s] (A) 用户输入：保温杯还有库存吗")
    await bus.publish(Event("user_input", "A", {"text": "保温杯还有库存吗"}))
    await turn_done.wait()
    turn_done.clear()
    tool_started.clear()

    # 2. 第二问（报销）：等它真的发起工具调用、step 正在飞时按停止。
    #    报销的答案不在第一轮检索结果里，模型必然发起 search——
    #    在飞窗口是确定的。
    print(f"\n[{t():5.2f}s] (A) 用户接着问：报销有什么规定")
    await bus.publish(Event("user_input", "A", {"text": "报销有什么规定"}))

    tool_wait = asyncio.ensure_future(tool_started.wait())
    turn_wait = asyncio.ensure_future(turn_done.wait())
    await asyncio.wait(
        {tool_wait, turn_wait}, return_when=asyncio.FIRST_COMPLETED
    )
    if tool_started.is_set() and not turn_done.is_set():
        print(f"\n[{t():5.2f}s] (A) 用户按下停止（工具调用正在飞）")
        await bus.publish(Event("user_interrupt", "A", {}))
        await turn_done.wait()
        if not step_cancelled.is_set():
            # 信号发出但那一步抢先完成：取消与完成竞速，完成赢了。
            print(f"\n[{t():5.2f}s] (A) （中断落空：那一步已抢在信号前完成）")
    else:
        print(
            f"\n[{t():5.2f}s] (A) （模型没调工具直接答完了，没赶上中断窗口）"
        )
    turn_done.clear()
    step_cancelled.clear()

    # 3. 中断之后再问一句：worker 活着，history 完好，正常新 turn
    print(f"\n[{t():5.2f}s] (A) 用户再问：顺便说说VPN怎么申请")
    await bus.publish(Event("user_input", "A", {"text": "顺便说说VPN怎么申请"}))
    await turn_done.wait()

    await agent.stop()
    print(f"\n[{t():5.2f}s] demo 结束")
    print(f"session log: {log_path}")


if __name__ == "__main__":
    asyncio.run(main())


def cli() -> None:
    """[project.scripts] 入口：stage03-demo。"""
    asyncio.run(main())
