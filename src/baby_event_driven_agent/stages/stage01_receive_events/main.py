"""Stage 1 演示：真实 LLM 流式输出，一问答完，再问下一句。

需要仓库根 .env 里的 OPENAI_API_KEY / OPENAI_API_BASE / OPENAI_MODEL
（环境变量可覆盖，比如临时换模型：OPENAI_MODEL=qwen3.7-flash stage01-demo）。

    python -m baby_event_driven_agent.stages.stage01_receive_events
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from .agent import Agent
from .events import Event, EventBus, SessionLog, t
from .llm import RealLLM


async def main() -> None:
    log_path = str(Path(tempfile.gettempdir()) / "stage01_session.jsonl")
    bus = EventBus()
    log = SessionLog(log_path)
    agent = Agent(bus, log, RealLLM())
    bus.subscribe("user_input", agent.on_user_input)

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

    bus.subscribe("agent_delta", ui_delta)
    bus.subscribe("agent_reply", ui_reply)

    print(f"[{t():5.2f}s] (A) 用户输入：保温杯还有库存吗")
    await bus.publish(Event("user_input", "A", {"text": "保温杯还有库存吗"}))
    print(f"\n[{t():5.2f}s] (A) 收到完整回答，用户接着问：玻璃杯呢")
    await bus.publish(Event("user_input", "A", {"text": "玻璃杯呢"}))
    print(f"\n[{t():5.2f}s] demo 结束")
    print(f"session log: {log_path}")


if __name__ == "__main__":
    asyncio.run(main())


def cli() -> None:
    """[project.scripts] 入口：stage01-demo。"""
    asyncio.run(main())
