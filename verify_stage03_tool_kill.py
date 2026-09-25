import asyncio, os, sys, tempfile, types
from pathlib import Path

# 本机没装 dotenv / openai（且本次验证不打真模型），给两个顶层 import 打桩，
# 让 agent / llm 模块能加载；RealLLM 实例化在本验证里不会发生。
_fake_dotenv = types.ModuleType("dotenv")
_fake_dotenv.dotenv_values = lambda *a, **k: {}
sys.modules.setdefault("dotenv", _fake_dotenv)

_fake_openai = types.ModuleType("openai")


class _AsyncOpenAI:  # noqa: N801
    def __init__(self, *a, **k): ...


_fake_openai.AsyncOpenAI = _AsyncOpenAI
sys.modules.setdefault("openai", _fake_openai)

SRC = Path(r"e:/repos/vcprjs/agent-cookbook/src")
sys.path.insert(0, str(SRC))

from baby_event_driven_agent.stages.stage03_interrupt.events import (  # noqa: E402
    Event, EventBus, SessionLog,
)
from baby_event_driven_agent.stages.stage03_interrupt.agent import Agent  # noqa: E402
import baby_event_driven_agent.stages.stage03_interrupt.llm as llm_mod  # noqa: E402

LOG = os.path.join(tempfile.gettempdir(), "verify_stage03.log")

# 探针：记录工具是否开始 / 是否被 cancel / 是否跑完
state: dict = {}


def make_probe() -> dict:
    return {"started": False, "finished": False, "cancelled": False}


async def slow_tool(args):
    """被换进 TOOLS 的慢工具：开始执行即标记，然后永远等一个不会 set 的事件，
    只靠外部中断的 cancel 唤醒——用来证明在飞工具被真实杀死。"""
    s = state["tool"]
    s["started"] = True
    state["tool_ready"].set()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        s["cancelled"] = True
        raise


class ToolCallStub:
    """只发一个工具调用，让 agent 进 search_rules（已被换成 slow_tool）。"""
    async def stream_chat(self, messages):
        yield {
            "type": "tool_call_delta", "index": 0, "id": "call_1",
            "name": "search_rules", "args_delta": '{"query": "报销"}',
        }


class StreamStub:
    """先吐一段可见文本，然后卡在流中间（专等中断 cancel）。"""
    async def stream_chat(self, messages):
        yield {"type": "text_delta", "text": "事件驱动架构"}
        await asyncio.Event().wait()


async def _until_flag(flag, timeout=10):
    t0 = asyncio.get_event_loop().time()
    while not flag["v"]:
        if asyncio.get_event_loop().time() - t0 > timeout:
            raise asyncio.TimeoutError()
        await asyncio.sleep(0.005)


async def run_tool_running_kill() -> bool:
    bus, log = EventBus(), SessionLog(LOG)
    agent = Agent(bus, log, ToolCallStub())
    evt = {"step_cancelled": False, "turn_end": False}
    ready = asyncio.Event()
    state["tool"] = make_probe()
    state["tool_ready"] = ready

    async def on_sc(e): evt["step_cancelled"] = True
    async def on_te(e): evt["turn_end"] = True
    bus.subscribe("step_cancelled", on_sc)
    bus.subscribe("turn_end", on_te)
    llm_mod.TOOLS["search_rules"] = slow_tool  # 换成会卡住的真·慢工具

    sid = "A"
    bus.publish(Event("user_input", sid, {"text": "报销有什么规定？"}), to=agent.agent_id)
    await asyncio.wait_for(ready.wait(), timeout=10)  # 工具真的开始执行
    bus.publish(Event("user_interrupt", sid, {"intent": "stop"}), to=agent.agent_id)
    await _until_flag(evt, timeout=10)

    t = state["tool"]
    ok = t["started"] and t["cancelled"] and not t["finished"] and evt["step_cancelled"]
    print("[场景 1] tool_running 真·当场掐死在飞工具")
    print(f"   工具 started={t['started']}  cancelled={t['cancelled']}(被真实 kill)  "
          f"finished={t['finished']}  step_cancelled事件={evt['step_cancelled']}")
    print("   RESULT:", "PASS" if ok else "FAIL")
    return ok


async def run_stream_stop() -> bool:
    bus, log = EventBus(), SessionLog(LOG)
    agent = Agent(bus, log, StreamStub())
    evt = {"step_cancelled": False, "turn_end": False, "delta": 0}
    delta = asyncio.Event()
    state["tool"] = make_probe()  # 本场景不应触发工具

    async def on_delta(e):
        evt["delta"] += 1
        delta.set()
    async def on_sc(e): evt["step_cancelled"] = True
    async def on_te(e): evt["turn_end"] = True
    bus.subscribe("agent_delta", on_delta)
    bus.subscribe("step_cancelled", on_sc)
    bus.subscribe("turn_end", on_te)

    sid = "B"
    bus.publish(Event("user_input", sid, {"text": "讲讲事件驱动架构"}), to=agent.agent_id)
    await asyncio.wait_for(delta.wait(), timeout=10)  # 半句已吐出
    bus.publish(Event("user_interrupt", sid, {"intent": "stop"}), to=agent.agent_id)
    await _until_flag(evt, timeout=10)

    ok = evt["delta"] > 0 and evt["turn_end"] and evt["step_cancelled"] and not state["tool"]["started"]
    print("[场景 2] 半句回答时打断（stream 段取消，未触发工具）—— 确认原有路径没被改坏")
    print(f"   agent_delta次数={evt['delta']}  step_cancelled={evt['step_cancelled']}  "
          f"turn_end={evt['turn_end']}  tool_started={state['tool']['started']}")
    print("   RESULT:", "PASS" if ok else "FAIL")
    return ok


async def main() -> None:
    r1 = await run_tool_running_kill()
    r2 = await run_stream_stop()
    print("\n总体:", "ALL PASS ✅" if (r1 and r2) else "SOME FAIL ❌")
    sys.exit(0 if (r1 and r2) else 1)


asyncio.run(main())
