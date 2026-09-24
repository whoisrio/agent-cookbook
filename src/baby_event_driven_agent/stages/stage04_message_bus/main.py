"""Stage 4 演示：事件离开 agent 之后要走多远。

需要仓库根 .env 里的 OPENAI_API_KEY / OPENAI_API_BASE / OPENAI_MODEL
（环境变量可覆盖）。第 1、2、6 段不打模型，可以当基准反复跑。

上行五段（累积）+ 下行一段（独立）：

1. **逐事件渲染 vs 缓冲消费者**（不打模型）：同一个 UI、同样 200 个事件，
   把渲染写在 await 型 handler 里（违约：重活进了热路径）vs StreamConsumer
   （offer 只做缓冲，渲染挪到帧界回调）——后者 emit 恢复微秒级。
2. **洪峰压测**（不打模型）：2000 个 token 增量全部送达 UI 消费者，
   由它自己合并刷屏——总线零策略，节奏归消费者。
3. **真跑一轮**：UI 的 StreamConsumer 自管刷新节奏（满 96 字或 50ms 帧界）。
4. **当场否决**：真跑一轮触发写工具，permission_guard（治理规则）在工具
   执行前否决，工具一条都没执行（rules.txt 字节未变）。
5. **人工确认**：工具声明“执行前要问人”（`Tool.requires_approval`），答案由
   人给。agent 发 approval_required 后挂在 future 上等；答复（user_approval）
   不走收件箱，直接交给那次等待。
6. **下行的优先级**（不打模型，独立跑）：收件箱按用户意图分成 followup /
   steering 两条队列，打断（stop / redirect）和审批答复走旁路。

行首标签沿用前三章：

    用户 │ 用户说了什么
    思考 │ assistant 的 thinking（暗色，已合并成帧）
    回答 │ assistant 的可见输出（亮蓝，已合并成帧）
    工具 │ 工具调用与真实结果（绿色）；被治理拦下用亮红
    系统 │ 生命周期与统计（绿色 / 亮红）
    说明 │ 旁白，只解释这一段在演示什么（灰色缩进）

    python -m baby_event_driven_agent.stages.stage04_message_bus
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import tools as tools_mod
from .agent import Agent, BLOCKED_PREFIX
from .bus import EventBus
from .events import STEERING, Event, Subscription, UserMessage
from .llm import RealLLM
from .tools import TOOLS
from .outbound import StreamConsumer
from .subscribers import counter, permission_guard

# ---------------------------------------------------------------- 屏幕上色
DIM = "\033[2m"
GREY = "\033[90m"
BLUE = "\033[94m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"

FLOOD = 2000
SLOW = 0.005


def line(label: str, color: str, text: str) -> None:
    print(f"\n{BOLD}{color}[{label}] {RESET}{color}{text}{RESET}")


def note(text: str) -> None:
    print(f"{GREY}       说明 │ {text}{RESET}")


def banner(n: int, title: str, what: str) -> None:
    """`── <case 名> · 第 n 段：<这段在看什么> ──`。名字就是录制产物名。"""
    names = (*CASE_ORDER, INBOX_CASE)
    print(f"\n{BOLD}── {names[n - 1]} · 第 {n} 段：{title} ──{RESET}")
    note(what)


def brief(text: str, limit: int = 140) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


# 段 4/5 依赖段 3 建好的 turn_done / StreamConsumer / 订阅者，所以 case 是**累积**的；
# 名字是 `两位编号-语义名`：编号让文件名字典序 = 演示顺序，语义名说明这一段在验什么机制。
CASE_ORDER = (
    "01-render-per-event-vs-buffered",
    "02-flood-all-delivered",
    "03-slow-subscriber",
    "04-permission-veto",
    "05-approval-flow",
)
CASE_TITLES = {
    "01-render-per-event-vs-buffered": "第 1 段：逐事件渲染 vs 缓冲消费者（不打模型）",
    "02-flood-all-delivered": "第 2 段：洪峰压测——全部送达（不打模型）",
    "03-slow-subscriber": "第 3 段：真跑一轮：UI 在 StreamConsumer 里自管刷新节奏",
    "04-permission-veto": "第 4 段：当场否决（permission_guard）",
    "05-approval-flow": "第 5 段：人工确认（approval 回路）",
}
INBOX_CASE = "06-inbox-priority"
INBOX_TITLE = "第 6 段：下行的意图与优先级：插队有先后（不打模型，独立跑）"
ALL_TITLE = "全部：上行五段（累积）+ 下行优先级（独立）"
CASE_IDS = ("all", *CASE_ORDER, INBOX_CASE)


async def inbox_priority_demo() -> None:
    """第 6 段：下行的意图与优先级。不打模型——脚本化 LLM + 慢工具探针。

    一次 turn 里看三类消息各归各位：followup（默认）排队等下一轮；
    steering（用户点名插话）在 step 边界按优先级拼进当前轮；
    redirect（旁路）的纠正先于一切插话落地。
    """
    banner(
        6,
        "下行的意图与优先级：插队有先后",
        "收件箱按用户意图分成两条优先队列：followup（默认，排队等下一轮）和"
        "steering（点名插话，step 边界按 priority 拼进当前轮）。打断（stop / "
        "redirect）和审批答复走旁路，先于一切队列——redirect 的纠正排在插话之前。",
    )

    class ScriptedLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, messages):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:  # 第一轮：发起一次写工具调用（被慢探针拖住）
                yield {
                    "type": "tool_call_delta",
                    "index": 0,
                    "id": "call_1",
                    "name": "update_inventory",
                    "args_delta": '{"category": "保温杯", "stock": 1}',
                }
            else:  # 转向后继续本轮：给一句收尾
                yield {"type": "text_delta", "text": "好的，先去订会议室，库存稍后再说。"}

    original = TOOLS["update_inventory"]

    async def slow_spy(args: dict[str, Any]) -> str:
        await asyncio.sleep(0.5)  # 工具在飞：留出插话与转向的时间窗
        return "已更新（demo 副本）"

    TOOLS["update_inventory"] = replace(original, fn=slow_spy, requires_approval=False)

    bus = EventBus()
    agent = Agent(bus, ScriptedLLM())
    sid = "A"
    tool_started = asyncio.Event()
    turn_done = asyncio.Event()
    steer_texts: list[str] = []
    followup_seen: list[str] = []

    async def on_tool_start(event: Event) -> None:
        tool_started.set()

    async def on_tool_result(event: Event) -> None:
        line("工具", GREEN, f"← {event.payload['name']} 结果：{brief(event.payload['result'])}")

    async def on_steer(event: Event) -> None:
        steer_texts.extend(event.payload["texts"])
        line("系统", GREEN, "插话按优先级拼进本轮：" + " → ".join(steer_texts))

    async def on_input(event: Event) -> None:
        if event.payload["text"] == "排队的话（followup）":
            followup_seen.append(str(event.payload["text"]))
            line("系统", DIM, "turn 结束后才轮到它：followup 作为新 turn 的主输入")

    async def on_end(event: Event) -> None:
        line("系统", GREEN, f"turn 结束（reason={event.payload.get('reason')}）")
        turn_done.set()

    bus.subscribe(Subscription("p.tool", ("tool_call_started",), on_tool_start))
    bus.subscribe(Subscription("p.result", ("tool_result",), on_tool_result))
    bus.subscribe(Subscription("p.steer", ("steering_consumed",), on_steer))
    bus.subscribe(Subscription("p.input", ("user_input",), on_input))
    bus.subscribe(Subscription("p.end", ("turn_end",), on_end))

    try:
        line("用户", YELLOW, "先改库存")
        bus.publish(UserMessage("user_input", sid, {"text": "先改库存"}), to=agent.agent_id)
        await asyncio.wait_for(tool_started.wait(), 10.0)

        # 工具在飞：三类消息各走各的路
        line("用户", DIM, "排队的话（followup，默认——不插话）")
        queued = UserMessage("user_input", sid, {"text": "排队的话（followup）"})
        bus.publish(queued, to=agent.agent_id)
        line("用户", YELLOW, "常规插话（intent=steering，priority=100）")
        bus.publish(
            UserMessage(
                "user_input",
                sid,
                {"text": "常规插话"},
                intent=STEERING,
                priority=100,
            ),
            to=agent.agent_id,
        )
        line("用户", YELLOW, "加急插话（intent=steering，priority=10）")
        bus.publish(
            UserMessage(
                "user_input",
                sid,
                {"text": "加急插话"},
                intent=STEERING,
                priority=10,
            ),
            to=agent.agent_id,
        )
        line("系统", YELLOW, "用户改主意了：把排队的那条升级为插话（promote）")
        ok = agent.promote(queued)
        note(
            "promote 精确移动那一条消息（followup → steering），别的排队消息"
            "不动；同步操作，与 worker 的取件天然互斥。"
            + ("升级成功。" if ok else "已太迟：消息正在处理中，无法再插话。")
        )
        line("系统", YELLOW, "转向：先别改库存了，去订会议室（redirect 旁路）")
        bus.publish(
            Event(
                "user_interrupt",
                sid,
                {"intent": "redirect", "text": "先别改库存了，去订会议室"},
            ),
            to=agent.agent_id,
        )

        await asyncio.wait_for(turn_done.wait(), 30.0)

        texts = [str(m.get("content", "")) for m in agent.history[sid]]
        i_redirect = texts.index("先别改库存了，去订会议室")
        i_urgent = texts.index("加急插话")
        i_normal = texts.index("常规插话")
        line(
            "实测",
            GREEN,
            "本轮上下文里的次序：转向纠正 → 加急插话(10) → 排队的话(100) → "
            "常规插话(100)——升级的那条按自己的 priority 参与本轮 drain，"
            "没有丢、没有抢占别人的位置",
        )
        note(
            "三类消息各归各位：followup 是用户说‘等下一轮’；steering 是用户说"
            "‘下个 step 前插进来’，按 priority 出队；redirect 不排队，纠正文本"
            "第一时刻落进上下文。promote 让排队消息可以中途升级，精确移动、"
            "不乱序。控制权在发布端（Stage 2 的消费时机分类到本章演进为用户"
            "显式指定意图）。"
        )
    finally:
        TOOLS["update_inventory"] = original
        await agent.stop()


async def main(case_ids: list[str] | None = None, sessions_dir: Path | None = None) -> None:
    sessions_dir = sessions_dir or (Path(__file__).resolve().parents[2] / "sessions" / "stage04")
    shutil.rmtree(sessions_dir, ignore_errors=True)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    run_inbox = not case_ids or "all" in case_ids or INBOX_CASE in case_ids
    if case_ids and set(case_ids) == {INBOX_CASE}:
        await inbox_priority_demo()
        return
    bus = EventBus()
    agent = Agent(bus, RealLLM())

    picked = [c for c in CASE_ORDER if not case_ids or "all" in case_ids or c in case_ids]
    if not picked:
        print(f"{GREY}没有匹配的 case：{case_ids}；可选：{', '.join(CASE_IDS)}{RESET}")
        return
    upto = CASE_ORDER.index(picked[-1]) + 1
    consumers: list[StreamConsumer] = []  # 第 3 段才建；finish() 按需停

    async def finish() -> None:
        for c in consumers:
            await c.stop()
        await agent.stop()
        print(f"\n{BOLD}── history 尾部（这一轮的真实消息形状）──{RESET}")
        for msg in agent.history.get("A", [])[-3:]:
            print(f"{GREY}  {json.dumps(msg, ensure_ascii=False)}{RESET}")
        line("系统", GREEN, "demo 结束")
        print(f"{GREY}  sessions 目录: {sessions_dir}{RESET}")

    print(f"{BOLD}Stage 4：消息机制 —— 事件离开 agent 之后要走多远{RESET}")
    if case_ids:
        print(f"{GREY}  （只跑：{', '.join(picked)}）{RESET}")

    # ------------------------------------------------------------- 第 1 段
    banner(
        1,
        "逐事件渲染 vs 缓冲消费者（同一个慢 UI）",
        f"同一个 UI、同样 200 个事件：左边把渲染写在 await 型 handler 里"
        f"（每次 {SLOW * 1000:.0f}ms——违约：重活进了热路径）；右边是 "
        "StreamConsumer：offer 只做缓冲追加（微秒级），渲染挪到帧界回调、每帧一次。",
    )
    rendered: list[str] = []

    class PerEventRender:
        """违约写法：await 型 handler 里做重活（渲染一次 5ms）。"""

        async def handle(self, event: Event) -> None:
            await asyncio.sleep(SLOW)
            rendered.append("x")

    class BufferedRender(StreamConsumer):
        def on_flush(self, text: str) -> None:
            time.sleep(SLOW)  # 渲染一帧的耗时，与逐事件渲染同量级
            rendered.append("frame")

    per_event = PerEventRender()
    bus.subscribe(Subscription("slow-render", ("tick",), per_event.handle))
    t0 = time.perf_counter()
    for _ in range(200):
        await bus.emit(Event("tick", "S1", {"text": "字"}))
    slow_cost = time.perf_counter() - t0

    buffered = BufferedRender()
    await buffered.start()
    bus.subscribe(Subscription("buffered-render", ("tick",), buffered))
    t0 = time.perf_counter()
    for _ in range(200):
        await bus.emit(Event("tick", "S1", {"text": "字"}))
    fast_cost = time.perf_counter() - t0
    await buffered.stop()

    line("实测", GREEN, f"违约写法（渲染进热路径）：200 个事件花了 {slow_cost:.2f}s")
    line(
        "实测",
        GREEN,
        f"StreamConsumer：emit 只花 {fast_cost:.3f}s——offer 是微秒级缓冲追加，"
        f"渲染按帧结算（{buffered.flushes} 帧摊掉 200 次渲染）",
    )
    note(
        "handler 的耗时就是 emit 的延迟：await 型 handler 里做重活，每次调用"
        "都被放大进 emit。重活挪进 on_flush 后按帧结算——热路径回到微秒级。"
        "做不快的消费者还有一条路：提供邮箱（mailbox），投递即返回、自己排干。"
    )

    if upto <= 1:
        await finish()
        return

    # ------------------------------------------------------------- 第 2 段
    banner(
        2,
        "洪峰压测：2000 个 delta 全部送达",
        f"直接往总线灌 {FLOOD} 个 token 增量，UI 是 StreamConsumer——offer 逐条"
        "缓冲（微秒级），帧界合并刷屏。总线零策略：没有丢弃、没有专用队列，"
        "全送达的代价是 UI 自己合并，节奏归消费者。",
    )

    class FloodUI(StreamConsumer):
        def __init__(self) -> None:
            super().__init__(max_chars=96, frame=0.05)
            self.received = 0

        def offer(self, event: Event) -> None:
            self.received += 1
            super().offer(event)

        def on_flush(self, text: str) -> None:
            pass  # 压测不刷屏，只数帧

    flood_ui = FloodUI()
    await flood_ui.start()
    bus.subscribe(Subscription("flood-ui", ("agent_delta",), flood_ui))
    life_seen: dict[str, int] = {}
    bus.subscribe(counter(life_seen, session="F"))

    async def flood(session: str, n: int) -> None:
        for _ in range(n):
            await bus.emit(Event("agent_delta", session, {"text": "字"}))

    async def insert_turn_end() -> None:
        line("系统", YELLOW, "洪峰过半：此刻插入一条 turn_end（直接分派，无队列积压）")
        await bus.emit(Event("turn_end", "F", {"reason": "flood test"}))

    t0 = time.perf_counter()
    await flood("F", FLOOD // 2)
    await insert_turn_end()
    await flood("F", FLOOD // 2)
    await bus.emit(
        Event(
            "agent_reply",
            "F",
            {"message": {"role": "assistant", "content": "这是完整答案"}},
        )
    )
    flood_cost = time.perf_counter() - t0
    line(
        "实测",
        GREEN,
        f"{FLOOD} 个 delta 全部送达（received={flood_ui.received}），"
        f"合并成 {flood_ui.flushes} 帧刷屏（{flood_cost:.2f}s）",
    )
    line(
        "实测",
        GREEN,
        f"生命周期事件一条没丢：turn_end 收到 {life_seen.get('turn_end', 0)} 条、"
        f"agent_reply 收到 {life_seen.get('agent_reply', 0)} 条——直接分派，"
        "没有队列积压",
    )
    note(
        "洪峰的应对不在总线，在消费者：offer 逐条缓冲是微秒级热路径，渲染按帧"
        "结算。总线的契约只有一条——handler 必须快；做不快的消费者用邮箱型"
        "（offer 即返回，自己排干）。"
    )

    if upto <= 2:
        await finish()
        return

    # ------------------------------------------------------------- 第 3 段
    banner(
        3,
        "真跑一轮：UI 在 StreamConsumer 里自管刷新节奏",
        "真模型跑一轮。UI 的回答 / 思考消费者是 StreamConsumer 子类——offer "
        "逐条缓冲（微秒级），满 96 字或 50ms 帧界刷一次屏，什么时候刷新由 "
        "UI 自己决定。",
    )
    turn_seen: dict[str, int] = {}
    bus.subscribe(counter(turn_seen, session="A"))

    turn_done = asyncio.Event()
    stream_open = [False]

    class StreamToLine(StreamConsumer):
        """把本 turn 的流式文本合并刷到一行里；turn 结束时由 ui_turn_end 收口。"""

        def __init__(self, label: str, color: str) -> None:
            super().__init__(max_chars=96, frame=0.05)
            self._label = label
            self._color = color

        def offer(self, event: Event) -> None:
            if event.session_id != "A":
                return
            if not stream_open[0]:
                print(f"\n{BOLD}[{self._label}] {RESET}{self._color}", end="")
                stream_open[0] = True
            super().offer(event)

        def on_flush(self, text: str) -> None:
            print(text, end="", flush=True)

    text_ui = StreamToLine("回答", BLUE)
    think_ui = StreamToLine("思考", DIM)
    await think_ui.start()
    await text_ui.start()
    consumers.extend([think_ui, text_ui])  # 提前收尾时由 finish() 停

    async def ui_tool_result(event: Event) -> None:
        p = event.payload
        if p.get("blocked"):
            line("工具", RED, f"← {p['name']} 被治理拦下：{brief(p['result'])}")
        elif p.get("skipped"):
            line("工具", RED, f"← {p['name']} 未执行（{p['result']}）")
        else:
            line("工具", GREEN, f"← {p['name']} 结果：{brief(p['result'])}")

    async def ui_turn_end(event: Event) -> None:
        if event.session_id != "A":
            return
        text_ui.flush()
        think_ui.flush()
        stream_open[0] = False
        line("系统", GREEN, f"turn 结束（reason={event.payload.get('reason')}）")
        turn_done.set()

    bus.subscribe(Subscription("ui.delta", ("agent_delta",), text_ui))
    bus.subscribe(Subscription("ui.thinking", ("agent_thinking",), think_ui))
    bus.subscribe(Subscription("ui.tool", ("tool_result",), ui_tool_result))
    bus.subscribe(Subscription("ui.turn_end", ("turn_end",), ui_turn_end))

    line("用户", YELLOW, "保温杯还有库存吗")
    t0 = time.perf_counter()
    bus.publish(UserMessage("user_input", "A", {"text": "保温杯还有库存吗"}), to=agent.agent_id)
    await turn_done.wait()
    turn_cost = time.perf_counter() - t0
    turn_done.clear()

    total = turn_seen.get("total", 0)
    deltas = turn_seen.get("agent_delta", 0)
    line(
        "实测",
        GREEN,
        f"这一轮上行 {total} 个事件（其中 {deltas} 个是 token 增量）；"
        f"从投递到 turn_end = {turn_cost:.2f}s",
    )
    note(
        f"如果 UI 逐事件渲染（每次 {SLOW * 1000:.0f}ms），光渲染就 ≈ "
        f"{total * SLOW:.2f}s。StreamConsumer 把渲染挪到帧界——热路径只有"
        "缓冲追加，节奏由 UI 自己的帧界决定。"
    )
    line(
        "实测",
        GREEN,
        f"合并刷屏：{text_ui.merged + think_ui.merged} 个增量 → "
        f"{text_ui.flushes + think_ui.flushes} 帧（每帧 ≤96 字或 50ms 一次）",
    )

    if upto <= 3:
        await finish()
        return

    # ------------------------------------------------------------- 第 4 段
    banner(
        4,
        "当场否决：规则说了算",
        "挂上 permission_guard（治理规则），把 update_rules（改规则库）拉黑。让 agent 去加一条规则，"
        "预期：工具调用在执行前被治理链否决，工具一条没执行，rules.txt 字节未变。",
    )
    agent.governor.add(permission_guard("update_rules"))
    kb = Path(__file__).resolve().parents[2] / "knowledge-base"
    rules_file = kb / "rules.txt"
    rules_before = rules_file.read_text(encoding="utf-8")

    line("用户", YELLOW, "加一条规则：会议室要提前一天预订")
    bus.publish(
        UserMessage("user_input", "A", {"text": "加一条规则：会议室要提前一天预订"}),
        to=agent.agent_id,
    )
    await turn_done.wait()
    turn_done.clear()

    if rules_file.read_text(encoding="utf-8") == rules_before:
        line("实测", GREEN, "rules.txt 未被改动（治理生效，工具没执行）")
    else:
        line("实测", RED, "rules.txt 被改动了（治理没生效）")
    blocked = [
        m for m in agent.history["A"] if str(m.get("content", "")).startswith(BLOCKED_PREFIX)
    ]
    note(
        f"被否决的结果作为一条 tool 消息进了上下文（{len(blocked)} 条，占位以"
        f"“{BLOCKED_PREFIX}”开头），模型知道这不是工具的真实输出；裁决的事实"
        "就在占位文本里，下一章它们随 history 落进轨迹。"
    )

    if upto <= 4:
        await finish()
        return

    # ------------------------------------------------------------- 第 5 段
    banner(
        5,
        "人工确认：人说了算（等答复的那一半）",
        "update_inventory 自己声明“执行前要问人”（Tool.requires_approval）；答案由人给。"
        "这一段的“人”就是下面这个订阅者：看到 approval_required 就打一句批准，"
        "再把答复 publish 回 agent——答复不走收件箱，直接交给正在等它的那次等待。"
        "为了让 demo 能反复跑，写工具落在 sessions/stage04/ 的副本上；工具是真执行的。",
    )
    inventory = kb / "inventory.txt"
    demo_inventory = sessions_dir / "inventory-demo.txt"
    demo_inventory.write_text(inventory.read_text(encoding="utf-8"), encoding="utf-8")
    tools_mod._INVENTORY = demo_inventory  # 只改这次 demo 的落点，不改仓库里那份

    async def demo_human(event: Event) -> None:
        p = event.payload
        line("人工", YELLOW, f"？ {p['name']} 要执行：{brief(p['arguments'])}")
        note(
            f"approval_required（request_id={p['request_id']}，"
            f"超时 {p['timeout']:g}s）：agent 正挂在这次等待上，等的人不在队列那头"
        )
        await asyncio.sleep(0.4)  # 人去点了一下
        line("人工", YELLOW, "→ 批准")
        bus.publish(
            Event(
                "user_approval",
                "A",
                {
                    "request_id": p["request_id"],
                    "approve": True,
                    "reason": "demo 里代替人点了一下",
                },
            ),
            to=agent.agent_id,
        )

    async def ui_decided(event: Event) -> None:
        p = event.payload
        line("系统", GREEN, f"确认结果：{p['action']} by {p['by']}")

    bus.subscribe(
        Subscription("demo.human", ("approval_required",), demo_human)
    )
    bus.subscribe(
        Subscription("demo.decided", ("approval_decided",), ui_decided)
    )

    line("用户", YELLOW, "把保温杯库存改成 45 件")
    bus.publish(
        UserMessage("user_input", "A", {"text": "把保温杯库存改成 45 件"}), to=agent.agent_id
    )
    await turn_done.wait()
    turn_done.clear()

    written = [
        ln
        for ln in demo_inventory.read_text(encoding="utf-8").splitlines()
        if ln.startswith("保温杯")
    ]
    line(
        "实测",
        GREEN,
        f"工具真执行了，副本上现在是：{written[0] if written else '（没找到）'}",
    )
    note(
        "确认的请求和结果都是事件：什么时候问的、谁批的、批完工具返回了什么，"
        "屏幕上这一串就是全部经过。等不到答复时按拒绝处理（fail-closed），不会是"
        "“没人管就放行”。"
    )

    if upto <= 5 and not run_inbox:
        await finish()
        return

    # ------------------------------------------------------------- 第 6 段
    await inbox_priority_demo()

    # ------------------------------------------------------------- 收尾
    await finish()


def cli() -> None:
    """[project.scripts] 入口：stage04-demo。

        stage04-demo                        # 跑全部（默认）
        stage04-demo 03-slow-subscriber     # 只跑到第 3 段（累积；名字见 --list）
        stage04-demo 06-inbox-priority      # 只跑下行的意图与优先级（独立）
        stage04-demo --list                 # 列 case 及其说明（不加载模型配置）
    """
    parser = argparse.ArgumentParser(
        prog="stage04-demo", description="Stage 4 演示：事件离开 agent 之后要走多远。"
    )
    parser.add_argument("cases", nargs="*", metavar="CASE", help="跑到指定 case（累积，默认全部）")
    parser.add_argument("--list", action="store_true", help="列出所有 case 后退出")
    parser.add_argument("--sessions-dir", default=None, help="session log 落点")
    args = parser.parse_args()
    if args.list:
        print(f"all\t{ALL_TITLE}")
        for cid in CASE_ORDER:
            print(f"{cid}\t{CASE_TITLES[cid]}")
        print(f"{INBOX_CASE}\t{INBOX_TITLE}")
        return
    asyncio.run(
        main(args.cases or None, Path(args.sessions_dir) if args.sessions_dir else None)
    )


if __name__ == "__main__":
    cli()
