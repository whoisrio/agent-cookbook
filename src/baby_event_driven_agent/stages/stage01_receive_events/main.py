"""Stage 1 演示：真实 LLM 流式输出。五问走完读 → 写 → 读回 → 查规则 → 越界。

每一问是一个可单跑的 case，名字是 `两位编号-语义名`（编号让文件名字典序 = 演示顺序）：
`01-read-stock` / `02-write-stock` / `03-read-back` / `04-search-rules` / `05-off-topic`。

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

import argparse
import asyncio
from dataclasses import dataclass
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


def banner(name: str, what: str) -> None:
    """每个 case 开头一行：`── <case 名>：这个 case 在看什么 ──`。

    名字就是录制产物名（`rec/stage01/<run>/<name>.gif`），看片时对得上。"""
    print(f"\n{BOLD}── {name}：{what} ──{RESET}")


@dataclass(frozen=True)
class Case:
    """一问就是一个 case：`id` 是 case 名（= 录制文件名），`title` 一句话说明它在看什么。

    id 一律 `两位编号-语义名`：编号让**文件名字典序 = 演示顺序**，语义名说明在看什么。"""

    id: str
    question: str
    title: str


# 每一问是一个 case，可以单独跑（跑法：stage01-demo 02-write-stock）。
# 注意有先后依赖：02-write-stock 会真写 inventory.txt，03-read-back 读它——单独跑 03 前先跑过 02。
CASES: tuple[Case, ...] = (
    Case(
        "01-read-stock",
        "保温杯还有库存吗",
        "纯读：模型自己选读工具，不写任何文件，看它会不会编库存",
    ),
    Case(
        "02-write-stock",
        "帮我把马克杯加进库存：8 件，陶瓷，350ml",
        "带副作用的写：真往 knowledge-base/inventory.txt 追加一行，跑前跑后 diff 可见",
    ),
    Case(
        "03-read-back",
        "马克杯还有货吗",
        "读回刚刚写进去的那行，验证写真的落了库（依赖 write-stock）",
    ),
    Case(
        "04-search-rules",
        "报销有什么规定",
        "换个知识库查规则：同样要事实，看模型选不选得对工具",
    ),
    Case(
        "05-off-topic",
        "迪丽热巴和杨幂谁更好看?",
        "知识库答不了的问题：看它怎么收场（按 system prompt 该答不知道），和前面几问对照",
    ),
)
ALL_TITLE = "全部 5 问（读 → 写 → 读回 → 查规则 → 越界），一条线看完 stage01（不编号）"
CASE_IDS = ("all", *(c.id for c in CASES))


async def main(case_ids: list[str] | None = None, sessions_dir: Path | None = None) -> None:
    # session log 落在包级 sessions/ 目录，按 stage 分目录
    base_dir = sessions_dir or (Path(__file__).resolve().parents[2] / "sessions" / "stage01")
    base_dir.mkdir(parents=True, exist_ok=True)
    log_path = str(base_dir / "session.jsonl")
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

    picks = [c for c in CASES if not case_ids or "all" in case_ids or c.id in case_ids]
    if not picks:
        line("系统", GREEN, f"没有匹配的 case：{case_ids}；可选：{', '.join(CASE_IDS)}")
        return
    if case_ids:
        line("系统", GREEN, f"只跑：{', '.join(c.id for c in picks)}")

    for case in picks:
        banner(case.id, case.title)
        line("用户", YELLOW, case.question)
        await bus.publish(Event("user_input", "A", {"text": case.question}))

    line("系统", GREEN, "demo 结束")
    print(f"{GREY}  session log: {log_path}{RESET}")


def cli() -> None:
    """[project.scripts] 入口：stage01-demo。

        stage01-demo                    # 跑全部（默认）
        stage01-demo 02-write-stock     # 只跑指定 case（名字见 --list）
        stage01-demo --list             # 列 case 及其说明（不加载模型配置）
    """
    parser = argparse.ArgumentParser(
        prog="stage01-demo", description="Stage 1 演示：真实 LLM 流式输出与工具调用。"
    )
    parser.add_argument("cases", nargs="*", metavar="CASE", help="只跑指定 case（默认全部）")
    parser.add_argument("--list", action="store_true", help="列出所有 case 后退出")
    parser.add_argument("--sessions-dir", default=None, help="session log 落点")
    args = parser.parse_args()
    if args.list:
        print(f"all\t{ALL_TITLE}")
        for case in CASES:
            print(f"{case.id}\t{case.title}")
        return
    asyncio.run(
        main(args.cases or None, Path(args.sessions_dir) if args.sessions_dir else None)
    )


if __name__ == "__main__":
    cli()
