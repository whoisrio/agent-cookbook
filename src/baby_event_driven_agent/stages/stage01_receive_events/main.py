"""Stage 1 演示：真实 LLM 流式输出，四轮对话走完读、写、读回、查规则。

需要仓库根 .env 里的 OPENAI_API_KEY / OPENAI_API_BASE / OPENAI_MODEL
（环境变量可覆盖，本地 ollama：OPENAI_API_BASE=http://localhost:11434/v1
OPENAI_API_KEY=ollama OPENAI_MODEL=qwen3.5:4b-32k stage01-demo）。
第 2 轮会真的往 inventory.txt 写一行"马克杯"，跑前跑后可 diff 验证。

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
    # session log 落在包级 sessions/ 目录，按 stage 分目录
    sessions_dir = Path(__file__).resolve().parents[2] / "sessions" / "stage01"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    log_path = str(sessions_dir / "session.jsonl")
    bus = EventBus()
    log = SessionLog(log_path)
    agent = Agent(bus, log, RealLLM())
    bus.subscribe("user_input", agent.on_user_input)

    # 上一条流式增量属于哪路：思考/正文切换时先换行，两类内容不混排
    last_kind = [""]

    async def ui_thinking(e: Event) -> None:
        if last_kind[0] != "thinking":
            print(flush=True)
            last_kind[0] = "thinking"
        # 思考内容暗色呈现，与可见输出区分
        print(f"\033[2m{e.payload['text']}\033[0m", end="", flush=True)

    async def ui_delta(e: Event) -> None:
        if last_kind[0] != "text":
            print(flush=True)
            last_kind[0] = "text"
        print(f'\033[94m{e.payload["text"]}\033[0m', end="", flush=True)

    async def ui_reply(e: Event) -> None:
        msg = e.payload["message"]
        if msg.get("tool_calls"):
            calls = ", ".join(
                f"{c['function']['name']}({c['function']['arguments']})"
                for c in msg["tool_calls"]
            )
            print(f"\n\033[32m[{t():5.2f}s] (A) → 工具调用：{calls}\033[0m")
        else:
            print(f"\n\033[32m[[{t():5.2f}s] (A) —— 回答完毕\033[0m")

    bus.subscribe("agent_thinking", ui_thinking)
    bus.subscribe("agent_delta", ui_delta)
    bus.subscribe("agent_reply", ui_reply)

    questions = [
        "保温杯还有库存吗",
        "帮我把马克杯加进库存：8 件，陶瓷，350ml",
        "马克杯还有货吗",
        "报销有什么规定",
        "迪丽热巴和杨幂谁更好看?",
    ]
    for q in questions:
        print(f"\033[33m[{t():5.2f}s] (A) 用户输入：{q}\033[33m")
        await bus.publish(Event("user_input", "A", {"text": q}))
        print()
    print(f"[{t():5.2f}s] demo 结束")
    print(f"session log: {log_path}")


if __name__ == "__main__":
    asyncio.run(main())


def cli() -> None:
    """[project.scripts] 入口：stage01-demo。"""
    asyncio.run(main())
