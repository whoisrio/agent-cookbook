"""Stage 4 演示：事件离开 agent 之后要走多远。

需要仓库根 .env 里的 OPENAI_API_KEY / OPENAI_API_BASE / OPENAI_MODEL
（环境变量可覆盖）。前两段不打模型，可以当基准反复跑。

六段：

1. **同步扇出 vs 异步分发**（不打模型）：同一个慢订阅者（每事件 5ms）、
   同样 200 个事件，两种发法各花多久。stage02/03 的 `for h in subs: await h`
   是前者——慢订阅者会把 loop 拖慢 N×5ms。
2. **洪峰压测**（不打模型）：2000 个 token 增量里夹一条 turn_end。
   看谁被丢（stream 道，可丢）、谁一条没丢（state 道，不可丢）。
3. **真跑一轮**：慢订阅者照样慢，但 loop 不等它；UI 用合并缓冲把 token
   攒成帧再刷（只等满会一顿一顿，只等帧界会白等）。
4. **当场否决**：真跑一轮触发写工具，permission_guard 在 before_tool_call 上
   否决，工具一条都没执行（rules.txt 字节未变）。
5. **人工确认**：规则只说“这个要问人”，答案由人给。agent 发 approval_required
   后挂在 future 上等；答复（user_approval）不走收件箱，直接交给那次等待。
   最后再点一次同一条确认：没人在等的答复也落盘留痕（不认领、不伪造裁决）。
6. **落盘与位点**：段 / 字节 / seq；从位点续读；崩进程留下的残尾怎么读。

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

import asyncio
import json
import shutil
import time
from pathlib import Path

from . import llm as llm_mod
from .agent import Agent, BLOCKED_PREFIX
from .bus import EventBus
from .events import OBSERVE, Event, Subscription
from .llm import RealLLM
from .outbound import CoalescingBuffer
from .persistence import EventLog
from .subscribers import (
    approval_policy,
    counter,
    permission_guard,
    slow_observer,
)

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
    print(f"\n{BOLD}── 第 {n} 段：{title} ──{RESET}")
    note(what)


def brief(text: str, limit: int = 140) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


async def main() -> None:
    sessions_dir = Path(__file__).resolve().parents[2] / "sessions" / "stage04"
    shutil.rmtree(sessions_dir, ignore_errors=True)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    log = EventLog(str(sessions_dir), segment_bytes=32 * 1024, keep_segments=8)
    bus = EventBus(log, state_size=1024, stream_size=64)
    agent = Agent(bus, RealLLM())

    print(f"{BOLD}Stage 4：消息机制 —— 事件离开 agent 之后要走多远{RESET}")

    # ------------------------------------------------------------- 第 1 段
    banner(
        1,
        "同步扇出 vs 异步分发（同一个慢订阅者）",
        f"慢订阅者每个事件睡 {SLOW * 1000:.0f}ms，同样发 200 个事件。左边是 stage02/03 的"
        "写法（emit 里 await 每一个订阅者），右边是本章的写法（emit 只入队，lane worker 去送）。",
    )
    seen: dict[str, int] = {}

    async def slow_one(event: Event) -> None:
        await asyncio.sleep(SLOW)

    bus.subscribe(slow_observer(SLOW, session="S1", name="slow@S1"))
    bus.subscribe(counter(seen, session="S1"))

    t0 = time.perf_counter()
    for _ in range(200):
        await slow_one(Event("tick", "S1", {"text": "字"}))
    sync_cost = time.perf_counter() - t0

    t0 = time.perf_counter()
    for _ in range(200):
        # tick 走 state 道（不可丢）：这一段要比的是“谁在等”，不能有丢弃掺进来
        await bus.emit(Event("tick", "S1", {"text": "字"}))
    emit_cost = time.perf_counter() - t0
    t0 = time.perf_counter()
    await bus.drain(timeout=30.0)
    drain_cost = time.perf_counter() - t0

    line("实测", GREEN, f"同步扇出：200 个事件，loop 等订阅者等了 {sync_cost:.2f}s")
    line(
        "实测",
        GREEN,
        f"异步分发：同样 200 个事件，emit 只花 {emit_cost:.3f}s"
        f"（订阅者的 {drain_cost:.2f}s 由 lane worker 背，drain 时才等）",
    )
    note(
        f"两套发法订阅者都收到了 {seen.get('total', 0)} 条：异步分发没有少送，"
        "只是把“等”这件事从 loop 挪到了 lane worker。"
    )

    # ------------------------------------------------------------- 第 2 段
    banner(
        2,
        "洪峰压测：token 流里夹一条 turn_end",
        f"直接往总线灌 {FLOOD} 个 token 增量（stream 道，队列 64，满了丢最新），"
        "中间夹一条 turn_end（state 道，不可丢）。看两条道各自的账。",
    )
    async def flood(session: str, n: int, yield_every: int = 10) -> None:
        """灌洪峰。每 yield_every 条让出一次：真实 token 之间有 await（等 HTTP
        流），emit 自己不主动让出——lane worker 能跑起来靠的是 agent loop 的
        await 点，这里用 sleep(0) 模拟它。"""
        for i in range(n):
            await bus.emit(Event("agent_delta", session, {"text": "字"}))
            if i % yield_every == 0:
                await asyncio.sleep(0)

    bus.subscribe(slow_observer(0.0005, session="F", name="flood-consumer"))
    flood_seen: dict[str, int] = {}
    bus.subscribe(counter(flood_seen, session="F"))
    t0 = time.perf_counter()
    await flood("F", FLOOD)
    # 洪峰之后紧跟着两条“不可丢”的：turn_end，以及完整答案 agent_reply
    await bus.emit(Event("turn_end", "F", {"reason": "flood test"}))
    await bus.emit(
        Event(
            "agent_reply",
            "F",
            {"message": {"role": "assistant", "content": "这是完整答案"}},
        )
    )
    flood_cost = time.perf_counter() - t0
    await bus.drain(timeout=60.0)
    st = bus.stats()
    line(
        "统计",
        GREEN,
        f"stream 道：投递 {st['lanes']['stream']['delivered']} / "
        f"丢弃 {st['lanes']['stream']['dropped']}（{flood_cost:.2f}s）",
    )
    line(
        "统计",
        GREEN,
        f"state 道：投递 {st['lanes']['state']['delivered']} / "
        f"丢弃 {st['lanes']['state']['dropped']}；turn_end 收到 "
        f"{flood_seen.get('turn_end', 0)} 条、agent_reply 收到 "
        f"{flood_seen.get('agent_reply', 0)} 条",
    )
    note(
        "两条道各有自己的 worker：token 流堵了只丢自己的，turn_end 排在洪峰之后"
        "也照样先到 —— 这就是 QoS 分道要买的东西。丢的是可丢的：屏幕上会少几个字，"
        "但完整答案走 state 道（agent_reply）一条没丢，UI 拿它兜底就能补齐。"
    )

    # 2b：同样的洪峰，消费者不慢 + UI 侧合并缓冲 → 不丢，且刷屏次数远少于事件数
    frames: list[str] = []
    flood_buf = CoalescingBuffer(frames.append, max_chars=96, frame=0.05)
    await flood_buf.start()
    g_seen: dict[str, int] = {}
    bus.subscribe(counter(g_seen, session="G"))

    async def flood_ui(event: Event) -> None:
        if event.session_id == "G":
            flood_buf.add(str(event.payload.get("text", "")))

    bus.subscribe(Subscription("flood.ui", ("agent_delta",), flood_ui, mode=OBSERVE))
    await flood("G", 800)
    await bus.drain(timeout=30.0)
    await flood_buf.stop()
    line(
        "统计",
        GREEN,
        f"同样的洪峰换个快消费者：{g_seen.get('agent_delta', 0)} 个增量一条没丢，"
        f"合并缓冲把它们刷成了 {flood_buf.flushes} 帧",
    )
    note(
        "背压的两半在这里：队列满了丢最新（可丢的那一半），攒够了或到帧界再刷"
        "（合并的那一半）。少刷的这几百次 IO，就是“UI 刷不动”的解药。"
    )

    # ------------------------------------------------------------- 第 3 段
    banner(
        3,
        "真跑一轮：慢订阅者不拖 loop，UI 用合并缓冲刷屏",
        "同一个慢订阅者挂在这一轮上（每事件 5ms）。loop 不等它；UI 侧把 token 攒成"
        "帧再刷：满 96 字或到 50ms 帧界，先到先刷（只等满会一顿一顿）。",
    )
    turn_seen: dict[str, int] = {}
    bus.subscribe(slow_observer(SLOW, session="A", name="slow@A"))
    bus.subscribe(counter(turn_seen, session="A"))

    turn_done = asyncio.Event()
    stream_open = [False]

    def make_sink(label: str, color: str):
        def sink(text: str) -> None:
            if not stream_open[0]:
                print(f"\n{BOLD}{color}[{label}] {RESET}{color}", end="")
                stream_open[0] = True
            print(text, end="", flush=True)

        return sink

    text_buf = CoalescingBuffer(make_sink("回答", BLUE), max_chars=96, frame=0.05)
    think_buf = CoalescingBuffer(make_sink("思考", DIM), max_chars=96, frame=0.05)
    await think_buf.start()
    await text_buf.start()
    await think_buf.start()

    async def ui_delta(event: Event) -> None:
        if event.session_id == "A":
            text_buf.add(str(event.payload.get("text", "")))

    async def ui_thinking(event: Event) -> None:
        if event.session_id == "A":
            think_buf.add(str(event.payload.get("text", "")))

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
        text_buf.flush()
        think_buf.flush()
        stream_open[0] = False
        line("系统", GREEN, f"turn 结束（reason={event.payload.get('reason')}）")
        turn_done.set()

    bus.subscribe(Subscription("ui.delta", ("agent_delta",), ui_delta, mode=OBSERVE))
    bus.subscribe(Subscription("ui.thinking", ("agent_thinking",), ui_thinking, mode=OBSERVE))
    bus.subscribe(Subscription("ui.tool", ("tool_result",), ui_tool_result, mode=OBSERVE))
    bus.subscribe(Subscription("ui.turn_end", ("turn_end",), ui_turn_end, mode=OBSERVE))

    line("用户", YELLOW, "保温杯还有库存吗")
    t0 = time.perf_counter()
    bus.publish(Event("user_input", "A", {"text": "保温杯还有库存吗"}), to=agent.agent_id)
    await turn_done.wait()
    turn_cost = time.perf_counter() - t0
    turn_done.clear()
    await bus.drain(timeout=30.0)

    total = turn_seen.get("total", 0)
    deltas = turn_seen.get("agent_delta", 0)
    line(
        "实测",
        GREEN,
        f"这一轮上行 {total} 个事件（其中 {deltas} 个是 token 增量）；"
        f"从投递到 turn_end = {turn_cost:.2f}s",
    )
    note(
        f"同步扇出的写法要在每个事件上等 {SLOW * 1000:.0f}ms，"
        f"光等待就 ≈ {total * SLOW:.2f}s（这一轮的实测总耗时是 {turn_cost:.2f}s，"
        "里面主要是模型请求）"
    )
    line(
        "实测",
        GREEN,
        f"合并缓冲：{text_buf.merged + think_buf.merged} 个增量 → "
        f"{text_buf.flushes + think_buf.flushes} 帧（每帧 ≤96 字或 50ms 一次）",
    )

    # ------------------------------------------------------------- 第 4 段
    banner(
        4,
        "当场否决：规则说了算",
        "挂上 permission_guard，把 update_rules（改规则库）拉黑。让 agent 去加一条规则，"
        "预期：before_tool_call 被否决，工具一条没执行，rules.txt 字节未变。",
    )
    bus.subscribe(permission_guard("update_rules"))
    kb = Path(__file__).resolve().parents[2] / "knowledge-base"
    rules_file = kb / "rules.txt"
    rules_before = rules_file.read_text(encoding="utf-8")

    line("用户", YELLOW, "加一条规则：会议室要提前一天预订")
    bus.publish(
        Event("user_input", "A", {"text": "加一条规则：会议室要提前一天预订"}),
        to=agent.agent_id,
    )
    await turn_done.wait()
    turn_done.clear()
    await bus.drain(timeout=30.0)

    if rules_file.read_text(encoding="utf-8") == rules_before:
        line("实测", GREEN, "rules.txt 未被改动（治理生效，工具没执行）")
    else:
        line("实测", RED, "rules.txt 被改动了（治理没生效）")
    blocked = [
        m for m in agent.history["A"] if str(m.get("content", "")).startswith(BLOCKED_PREFIX)
    ]
    note(
        f"被否决的结果作为一条 tool 消息进了上下文（{len(blocked)} 条，占位以"
        f"“{BLOCKED_PREFIX}”开头），模型知道这不是工具的真实输出；裁决本身也在 log 里，"
        "事后答得出“这个工具为什么没执行”。"
    )

    # ------------------------------------------------------------- 第 5 段
    banner(
        5,
        "人工确认：人说了算（等答复的那一半）",
        "规则只判“这个要不要问人”（当场，微秒级）；答案由人来给（之后，可能要几十秒）。"
        "这一段的“人”就是下面这个订阅者：看到 approval_required 就打一句批准，"
        "再把答复 publish 回 agent——答复不走收件箱，直接交给正在等它的那次等待。"
        "为了让 demo 能反复跑，写工具落在 sessions/stage04/ 的副本上；工具是真执行的。",
    )
    inventory = kb / "inventory.txt"
    demo_inventory = sessions_dir / "inventory-demo.txt"
    demo_inventory.write_text(inventory.read_text(encoding="utf-8"), encoding="utf-8")
    llm_mod._INVENTORY = demo_inventory  # 只改这次 demo 的落点，不改仓库里那份
    bus.subscribe(approval_policy("update_inventory"))

    asked_ids: list[str] = []

    async def demo_human(event: Event) -> None:
        p = event.payload
        asked_ids.append(str(p["request_id"]))
        line("人工", YELLOW, f"？ {p['name']} 要执行：{brief(p['arguments'])}")
        note(
            f"approval_required（seq={event.seq}，request_id={p['request_id']}，"
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
        line("系统", GREEN, f"确认结果：{p['action']} by {p['by']}（seq={event.seq}）")

    bus.subscribe(
        Subscription("demo.human", ("approval_required",), demo_human, mode=OBSERVE)
    )
    bus.subscribe(
        Subscription("demo.decided", ("approval_decided",), ui_decided, mode=OBSERVE)
    )

    line("用户", YELLOW, "把保温杯库存改成 45 件")
    bus.publish(
        Event("user_input", "A", {"text": "把保温杯库存改成 45 件"}), to=agent.agent_id
    )
    await turn_done.wait()
    turn_done.clear()
    await bus.drain(timeout=30.0)

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
        "确认的请求和结果都是事件，seq 排得出来：什么时候问的、谁批的、批完工具返回了"
        "什么。审计链就是这么攒出来的——不用另外写一套日志。"
        "等不到答复时按拒绝处理（fail-closed），不会是“没人管就放行”。"
    )

    async def ui_stale(event: Event) -> None:
        p = event.payload
        line(
            "系统",
            YELLOW,
            f"又一条答复到了（request_id={p['request_id']}）：没人在等它了 → "
            f"不认领，只留痕（seq={event.seq}）",
        )

    bus.subscribe(
        Subscription("demo.stale", ("approval_reply",), ui_stale, mode=OBSERVE)
    )
    line("人工", YELLOW, "（手抖又点了一下同一条确认）")
    bus.publish(
        Event(
            "user_approval",
            "A",
            {
                "request_id": asked_ids[0],
                "approve": True,
                "reason": "重复点了一下",
            },
        ),
        to=agent.agent_id,
    )
    await bus.drain(timeout=30.0)
    note(
        "没人等的答复也进 log（`approval_reply`，带 stale=true）：一次确认的输入不能因为"
        "“没人在等”就消失——否则用户以为批了、系统按拒绝走了，两边对不上账。"
        "但留痕不等于伪造裁决：那次确认的回执仍然只有超时/批准那一条。"
    )

    # ------------------------------------------------------------- 第 6 段
    banner(
        6,
        "落盘与位点",
        "段 + 稀疏索引 + 长度前缀 + CRC；位点记在 offsets.json，重启从下一条续读；"
        "崩进程留下的残尾读到这里为止，前面一条不少。",
    )
    log.close()
    st = log.stats()
    line(
        "统计",
        GREEN,
        f"落盘：{st['segments']} 段 / {st['bytes']} 字节 / "
        f"seq 1..{st['last_seq']}（最老可用 {st['oldest_seq']}）",
    )
    log.commit("ui", 5)
    rest = log.read_since(log.offset("ui"))
    line(
        "统计",
        GREEN,
        f"位点：ui 已消费到 seq {log.offset('ui')}，从下一条续读 → {len(rest)} 条"
        f"（{rest[0]['seq'] if rest else '-'}..{rest[-1]['seq'] if rest else '-'}）",
    )
    note(
        f"位点 5 已经落在保留窗口之外（最老可用 {log.oldest_seq}）：保留策略删掉的段"
        "读不回来，续读只能从现存最老的一条开始——这是保留与位点唯一的冲突点，"
        "要么把保留期放长，要么接受“回放不回那么远”。"
    )

    torn_dir = sessions_dir / "_torn"
    shutil.rmtree(torn_dir, ignore_errors=True)
    torn_dir.mkdir()
    seg = sorted(sessions_dir.glob("evt-*.log"))[-1]
    raw = seg.read_bytes()
    (torn_dir / seg.name).write_bytes(raw)
    intact = len(EventLog(str(torn_dir)).read_since(0))
    (torn_dir / seg.name).write_bytes(raw[:-11])  # 砍掉最后 11 字节 = 崩在写一半
    torn = EventLog(str(torn_dir))
    got = torn.read_since(0)
    line(
        "统计",
        GREEN,
        f"残尾：完好时这一段读到 {intact} 条；砍掉最后 11 字节后读到 {len(got)} 条"
        f"（停在坏记录之前，没炸，前 {len(got)} 条一条不少）",
    )
    note(
        f"判定靠长度前缀 + CRC，不靠 try/except 猜：这一段原本 {len(raw)} 字节，"
        "砍掉最后 11 字节模拟崩在写一半，只丢没写完的那一条。"
    )

    # ------------------------------------------------------------- 收尾
    await think_buf.stop()
    await text_buf.stop()
    await agent.stop()
    print(f"\n{BOLD}── history 尾部（这一轮的真实消息形状）──{RESET}")
    for msg in agent.history["A"][-3:]:
        print(f"{GREY}  {json.dumps(msg, ensure_ascii=False)}{RESET}")
    line("系统", GREEN, "demo 结束")
    print(f"{GREY}  session log: {sessions_dir}{RESET}")


if __name__ == "__main__":
    asyncio.run(main())


def cli() -> None:
    """[project.scripts] 入口：stage04-demo。"""
    asyncio.run(main())
