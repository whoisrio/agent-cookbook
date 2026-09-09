"""验证 agent 主循环里 steering / interrupt 的落点。

这几个不变量都是"不看代码发现不了"的：steering 注入成功后本轮的收尾
状态对不对、空闲时收到的控制类事件进没进轨迹。

    PYTHONPATH=src pytest src/baby_event_driven_agent/tests/test_agent_loop.py
"""

from __future__ import annotations

import asyncio

from baby_event_driven_agent.agent import AgentRuntime
from baby_event_driven_agent.bus import EventBus
from baby_event_driven_agent.events import EventType
from baby_event_driven_agent.llm import ScriptedLLM
from baby_event_driven_agent.trajectory import Trajectory

SESSION = "sess_loop"


def _runtime(
    script: list[dict], trajectory: Trajectory, **kwargs
) -> tuple[AgentRuntime, asyncio.Task]:
    agent = AgentRuntime(
        agent_id="a",
        session_id=SESSION,
        bus=EventBus(),
        llm=ScriptedLLM(script),
        tools=[],
        trajectory=trajectory,
        **kwargs,
    )
    return agent, asyncio.create_task(agent.run())


def _turn_end(trajectory: Trajectory) -> dict:
    ended = [r for r in trajectory.records if r.type == EventType.TURN_END]
    assert len(ended) == 1, f"TURN_END 只能有一条，实际 {len(ended)}"
    return ended[0].payload


def test_steering_mid_turn_keeps_end_reason_stop() -> None:
    """steering 让本轮继续往下走，收尾状态不能被它污染成 None。

    steering 是"改方向"不是"结束"，本轮最后正常说完就该是 stop。写成
    None 的话，回放的人答不出这一轮到底是怎么结束的。
    """

    async def go() -> None:
        trajectory = Trajectory()
        agent, runner = _runtime(
            [
                {"sleep": 1.0, "content": "第一轮（会被打断）"},
                {"content": "第二轮（带着 steering 跑）"},
            ],
            trajectory,
            max_steps=4,
        )

        await agent.submit("讲个笑话")
        await asyncio.sleep(0.2)
        await agent.steer("换个话题聊天气")
        await asyncio.sleep(0.6)

        payload = _turn_end(trajectory)
        assert payload["end_reason"] == "stop", (
            f"被 steering 改过方向、但本轮正常收尾，end_reason 该是 stop，"
            f"实际 {payload['end_reason']!r}"
        )

        # steering 的文本得真的进了历史，否则下一步还是老的上下文
        assert any(
            m.get("content") == "换个话题聊天气"
            for m in agent.history
            if m["role"] == "user"
        ), "steering 的内容没进历史"

        agent.stop()
        await asyncio.wait_for(runner, timeout=3.0)

    asyncio.run(go())


def test_idle_control_events_are_recorded() -> None:
    """空闲（没有 turn 在跑）时收到的控制类事件也要进轨迹。

    不写轨迹的话这条消息在回放里凭空消失——replay_messages 正是靠
    user.steering 记录重建用户消息的。
    """

    async def go() -> None:
        trajectory = Trajectory()
        agent, runner = _runtime([{"content": "回答"}], trajectory)

        await agent.steer("空闲时的 steering")
        await asyncio.sleep(0.2)
        await agent.interrupt("空闲时的取消")
        await asyncio.sleep(0.2)

        kinds = [r.type for r in trajectory.records]
        assert EventType.USER_STEERING in kinds, f"steering 没进轨迹，实际 {kinds}"
        assert EventType.INTERRUPT in kinds, f"interrupt 没进轨迹，实际 {kinds}"

        replayed = trajectory.replay_messages(SESSION)
        assert replayed and replayed[0]["content"] == "空闲时的 steering", (
            f"回放重建不出空闲时的 steering，实际 {replayed}"
        )

        agent.stop()
        await asyncio.wait_for(runner, timeout=3.0)

    asyncio.run(go())


def _all() -> list:
    return [obj for name, obj in sorted(globals().items()) if name.startswith("test_")]


if __name__ == "__main__":
    for case in _all():
        case()
        print(f"  ok  {case.__name__}")
    print(f"\n{len(_all())} 个用例通过")
