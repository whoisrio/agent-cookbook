"""Stage 1 演示：真实 LLM 流式输出，四轮对话走完读、写、读回、查规则。

需要仓库根 .env 里的 OPENAI_API_KEY / OPENAI_API_BASE / OPENAI_MODEL
（环境变量可覆盖，本地 ollama：OPENAI_API_BASE=http://localhost:11434/v1
OPENAI_API_KEY=ollama OPENAI_MODEL=qwen3.5:4b-32k stage01-demo）。
第 2 轮会真的往 inventory.txt 写一行"马克杯"，跑前跑后可 diff 验证。

每一行都带**行首标签**，角色一眼分得开：

    用户 │ 用户说了什么
    思考 │ assistant 的 thinking（暗色流）
    LLM(回答) │ assistant 的可见输出（亮蓝流）
    LLM(要求执行工具) │ 模型要求调用的工具（绿色）
    执行工具 │ 工具真实执行与结果（绿色）
    系统 │ 生命周期 / 收尾信息（绿色）

    python -m baby_event_driven_agent.stages.stage01_receive_events
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .agent import Agent
from .events import Event, EventBus, SessionLog, t
from .llm import RealLLM

# ---------------------------------------------------------------- 屏幕上色
# 与 stage02 / stage03 同一套底子：思考暗色、正文亮蓝、工具/生命周期绿色、
# 用户输入黄色，旁白单独灰色。
DIM = "\033[2m"  # 思考内容
GREY = "\033[90m"  # 收尾信息
BLUE = "\033[94m"  # LLM 可见输出
GREEN = "\033[32m"  # 工具调用与结果 / 生命周期
YELLOW = "\033[33m"  # 用户输入
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
    sessions_dir = Path(__file__).resolve().parents[2] / "sessions" / "stage01"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    log_path = str(sessions_dir / "session.jsonl")
    bus = EventBus()
    log = SessionLog(log_path)
    agent = Agent(bus, log, RealLLM())
    bus.subscribe("user_input", agent.on_user_input)

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

    bus.subscribe("agent_thinking", ui_thinking)
    bus.subscribe("agent_delta", ui_delta)
    bus.subscribe("agent_reply", ui_reply)
    bus.subscribe("tool_result", ui_tool_result)

    questions = [
        "保温杯还有库存吗",
        "帮我把马克杯加进库存：8 件，陶瓷，350ml",
        "马克杯还有货吗",
        "报销有什么规定",
        "迪丽热巴和杨幂谁更好看?",
    ]
    for q in questions:
        line("用户", YELLOW, q)
        await bus.publish(Event("user_input", "A", {"text": q}))
    line("系统", GREEN, "demo 结束")
    print(f"{GREY}  session log: {log_path}{RESET}")


if __name__ == "__main__":
    asyncio.run(main())


def cli() -> None:
    """[project.scripts] 入口：stage01-demo。"""
    asyncio.run(main())
