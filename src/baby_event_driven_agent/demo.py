"""可跑的演示（真模型：需要仓库根 .env 里的 OPENAI_API_KEY / OPENAI_API_BASE）。

    PYTHONPATH=src python -m baby_event_driven_agent.demo

三个场景：
  A 危险命令被 permission_guard 拦下，模型拿到否决后自己改口
  B 长任务跑到一半被 interrupt，取消的是这一步，loop 活着，会话接着用
  C 写文件触发审批闭环：订阅者否决 + 回发事件，改变 agent 下一步
"""

from __future__ import annotations

import asyncio

from .agent import AgentRuntime, Tool
from .bus import EventBus
from .events import EventType
from .extensions import (
    approval_gate,
    console_observer,
    memory_extractor,
    permission_guard,
)
from .llm import OpenAIChatLLM
from .trajectory import Trajectory

AGENT_ID = "baby"
SESSION = "sess_demo"

# 现在直接打真模型（OpenAIChatLLM），不再用脚本化的离线回放。


async def get_weather(args: dict) -> str:
    return f"{args.get('city', '北京')}：晴，26℃"


async def run_shell(args: dict) -> str:
    return f"执行完毕：{args.get('command')}"


async def write_file(args: dict) -> str:
    return f"已写入 {args.get('path')}"


async def slow_task(args: dict) -> str:
    seconds = float(args.get("seconds", 3))
    await asyncio.sleep(seconds)
    return f"长任务完成，耗时 {seconds}s"


def build() -> tuple[AgentRuntime, EventBus, Trajectory, list]:
    bus = EventBus()
    trajectory = Trajectory()
    memory: list[dict] = []

    tools = [
        Tool("get_weather", get_weather, "查天气", {"type": "object", "properties": {"city": {"type": "string"}}}),
        Tool("run_shell", run_shell, "执行 shell 命令", {"type": "object", "properties": {"command": {"type": "string"}}}),
        Tool("write_file", write_file, "写文件", {"type": "object", "properties": {"path": {"type": "string"}}}),
        Tool("slow_task", slow_task, "一个很慢的任务", {"type": "object", "properties": {"seconds": {"type": "number"}}}),
    ]

    agent = AgentRuntime(
        agent_id=AGENT_ID,
        session_id=SESSION,
        bus=bus,
        llm=OpenAIChatLLM(),
        tools=tools,
        trajectory=trajectory,
        system_prompt="你是一个助手，可以调用工具。",
    )

    bus.subscribe(permission_guard("run_shell"))
    bus.subscribe(approval_gate(bus, AGENT_ID, "write_file"))
    bus.subscribe(memory_extractor(memory))
    bus.subscribe(console_observer())
    return agent, bus, trajectory, memory


async def main() -> None:
    agent, bus, trajectory, memory = build()
    runner = asyncio.create_task(agent.run())

    print("=" * 68)
    print("A. 危险命令：工具调用被拦截（拦截决定会进轨迹）")
    print("=" * 68)
    await agent.submit("帮我把 /tmp/demo 删掉")
    await asyncio.sleep(0.4)

    print()
    print("=" * 68)
    print("B. 长任务跑到一半被取消：取消的是这一步，loop 仍然活着")
    print("=" * 68)
    await agent.submit("启动那个长任务")
    await asyncio.sleep(0.5)
    await agent.interrupt("用户点了停止")
    await asyncio.sleep(0.3)

    print()
    print("中断后会话继续用，历史没有被撕掉：")
    await agent.submit("那现在怎么办")
    await asyncio.sleep(0.4)

    print()
    print("=" * 68)
    print("C. 审批闭环：订阅者否决 + 回发事件，改变 agent 下一步")
    print("=" * 68)
    await agent.submit("帮我写个文件")
    await asyncio.sleep(0.6)

    agent.stop()
    await asyncio.wait_for(runner, timeout=3.0)
    await bus.drain()

    print()
    print("=" * 68)
    print("轨迹（agent emit 的生命周期事件就是它本身，没有第二套埋点）")
    print("=" * 68)
    for record in trajectory.records:
        decisions = ""
        if record.decisions:
            decisions = "  " + "; ".join(
                f"{d['by']}:{d['action']}" for d in record.decisions
            )
        print(f"{record.seq:>3}  {record.type:<18} {record.payload}{decisions}")

    print()
    print(f"历史消息 {len(agent.history)} 条，长期记忆候选 {len(memory)} 条")
    replayed = trajectory.replay_messages(SESSION)
    print(f"从轨迹回放出的消息：{len(replayed)} 条")
    print("（少 1 条是 system prompt —— 它属于配置，不是事件，不进轨迹）")


if __name__ == "__main__":
    asyncio.run(main())
