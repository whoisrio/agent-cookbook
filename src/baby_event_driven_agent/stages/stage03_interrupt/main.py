"""Stage 3 演示：打断在飞的一步——按**命中落点**逐个看收尾形状。

需要仓库根 .env 里的 OPENAI_API_KEY / OPENAI_API_BASE / OPENAI_MODEL
（环境变量可覆盖，比如临时换模型：OPENAI_MODEL=qwen3.7-flash stage03-demo）。

路基沿用 stage02：inbound 的 publish 是**同步**的（`bus.publish(event, to=...)`，
投进目标 agent 的收件箱就返回），outbound 的 agent 事件走 emit 扇出。
用户输入和中断信号都从 inbound 进来——中断只是另一种类型的命令，
不额外订阅、也不经过 UI handler。

按 books/event-driven-agent/03-interrupt.md
的六个场景逐个演示（等中断信号落在目标那一格再按）：

    场景 1  已发 LLM、未回复    —— 请求刚发出，一个增量都还没回
    场景 2  只在吐 thinking    —— 还没吐可见文本
    场景 3  要调工具、参数没吐完 —— 流里已有 tool_call_delta，工具没执行
    场景 4  tool 执行中        —— 在跑的工具当场掐死（不再等它跑完）
    场景 5  tool 刚好跑完      —— 结果都拿到了，模型还没给最终回答
    场景 6  回答只说了一半     —— 流里只有 text_delta

每个场景演示一次 stop；场景 1–4 另演示一次 redirect（折标注 / 补空壳 /
全丢+折标注 / 纯 steering）。case 名 = **编号 + 落点 + 意图**（`--list` 看全，名字即录制产物名，
字典序就是演示顺序）。收尾只有一条规则：

    stop     ：看**尾部角色**——尾部是 tool → 补 assistant 封口占位；否则 → 补 user 中断标记。
    redirect ：一律补纠正 user；折不折 REDIRECT_NOTE，只看 assistant 输出有没有被截断。

什么时候是 stop、什么时候是 redirect —— **由用户选，agent 不猜**：

    用户意图            信封                             收尾
    ───────────────────────────────────────────────────────────────
    停（问错了/等不及）   {"intent": "stop"}              掐掉 + 封口，turn 结束
    转向（改主意、接着干）{"intent": "redirect",          掐掉 + 补纠正 user，
                        "text": "…纠正内容…"}            turn 不结束，同 turn 重发

每一行都带**行首标签**，角色一眼分得开：

    用户 │ 用户说了什么
    LLM·思考 │ assistant 的 thinking（暗色流，超 300 字截断标（略））
    LLM·回答 │ assistant 的可见输出（亮蓝流）
    LLM(要求执行工具) │ 模型要求调用的工具（绿色）
    执行工具 │ 工具真实执行与结果（绿色）
    收尾 │ 这一段之后的 history 尾部（灰色 JSON）
    系统 │ 生命周期：掐掉 / 边界命中 / turn 结束（统一橙色）
    说明 │ 旁白，只解释这一段在演示什么（灰色缩进，不属于对话）

    python -m baby_event_driven_agent.stages.stage03_interrupt
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from .agent import Agent
from .events import Event, EventBus, SessionLog
from .llm import TOOLS, RealLLM

# ---------------------------------------------------------------- 屏幕上色
# 与 stage01 / stage02 同一套底子：思考暗色、正文亮蓝、工具绿色、用户输入黄色。
# 系统提示（生命周期：掐掉 / 边界命中 / turn 结束）统一橙色，不按 intent 上色；
# 本章多出两类用户动作，各给一色（只标在"用户"行上，标明谁按的、命中哪一步）：
#   亮红 = 停（intent=stop，掐掉就结束）
#   洋红 = 转向（intent=redirect，掐掉后原地重发）
# 旁白单独用灰色，且只缩进不出现在对话流里——不再和"思考"共用一个暗色。
DIM = "\033[2m"  # 思考内容
GREY = "\033[90m"  # 旁白说明 / 收尾信息 / history 尾部
BLUE = "\033[94m"  # assistant 可见输出
GREEN = "\033[32m"  # 工具调用与结果 / 正常 turn 收尾
YELLOW = "\033[33m"  # 用户输入
RED = "\033[91m"  # 停（stop）及其命中
MAGENTA = "\033[35m"  # 转向（redirect）及其命中
ORANGE = "\033[38;5;208m"  # 系统提示（生命周期/收尾）统一橙色
BOLD = "\033[1m"
RESET = "\033[0m"

# 上限，防止 demo 无限等：worker 里模型一旦异常就发不出 turn_end；
# 目标落点事件也可能这次压根没出现（比如模型不吐 thinking）。
TURN_TIMEOUT = 90.0
HIT_TIMEOUT = 30.0
THINK_TIMEOUT = 12.0  # thinking 落点专用：超过此时长还没吐 thinking 就判定模型没思考，不再退化去打断答案


_LAST_LABEL: str | None = None  # 当前正在续打的那一路流；None 表示不在流里


def _end_stream_line() -> None:
    """流还在续打时先换行收口——下一个 line / 不同标签的流才会触发。"""
    global _LAST_LABEL
    if _LAST_LABEL is not None:
        print()
        _LAST_LABEL = None


def line(label: str, color: str, text: str) -> None:
    """对话流里的一行：`[标签] 内容`（和 stage04 / stage05 同一套行首与上色）。"""
    _end_stream_line()
    print(f"\n{BOLD}{color}[{label}] {RESET}{color}{text}{RESET}")


def stream_frame(label: str, color: str, text: str) -> None:
    """流式输出的帧：同一路（同标签）只起一次行首，之后直接续打，避免每帧一行刷屏。"""
    global _LAST_LABEL
    if _LAST_LABEL != label:
        _end_stream_line()
        print(f"{BOLD}{color}[{label}] {RESET}{color}", end="")
        _LAST_LABEL = label
    print(f"{color}{text}{RESET}", end="", flush=True)


def note(text: str) -> None:
    """旁白：缩进 + 灰色 + "说明"标签，明确不在对话流里。"""
    _end_stream_line()
    print(f"{GREY}       说明 │ {text}{RESET}")


def warn(text: str) -> None:
    """红色旁白：场景前提没满足（比如场景 2 模型没吐 thinking）时明确喊出来，
    不要悄悄退化成别的中断还假装演示成功。"""
    _end_stream_line()
    print(f"{RED}       说明 │ {text}{RESET}")


def banner(tag: str, title: str, what: str) -> None:
    _end_stream_line()
    print(f"\n{BOLD}── {tag}：{title} ──{RESET}")
    note(what)


def brief(text: str, limit: int = 140) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


@dataclass(frozen=True)
class Case:
    """一段可单独执行的演示：等中断落在 landing 这一格，再发 intent。

    landing 决定发中断的时机，intent 决定 stop 还是 redirect。"""

    id: str
    tag: str
    title: str
    question: str
    landing: str = ""
    intent: str = ""
    note: str = ""
    redirect_text: str = ""


# case 名是 `两位编号-落点-意图`（如 07-tool-running-stop = 工具执行中被 stop）：
# 编号让**文件名字典序 = 演示顺序**，语义部分说明"在演示哪一格"；整串也是录制产物名。
CASES: tuple[Case, ...] = (
    Case(
        "01-sent-stop", "场景 1", "已发 LLM、未回复 —— stop", "你好，用一句话介绍你自己",
        "immediate", "stop",
        note="还没有任何输出可留；尾部是没被回答的 user → 补 user 中断标记。",
    ),
    Case(
        "02-sent-redirect", "场景 1", "已发 LLM、未回复 —— redirect", "你好，用一句话介绍你自己",
        "immediate", "redirect",
        redirect_text="别自我介绍了，改成说说报销规定",
        note="同一个落点、只差 intent：turn 不结束，补一条折了 REDIRECT_NOTE 的纠正 user。",
    ),
    Case(
        "03-thinking-stop", "场景 2", "只在吐 thinking —— stop",
        "报销和 VPN 分别怎么申请？先想清楚再决定查什么", "thinking", "stop",
        note="thinking 不进 history，尾部仍是 user → 同场景 1，补 user 中断标记。",
    ),
    Case(
        "04-thinking-redirect", "场景 2", "只在吐 thinking —— redirect",
        "报销和 VPN 分别怎么申请？先想清楚再决定查什么", "thinking", "redirect",
        redirect_text="先别查了，改成订会议室",
        note="redirect 先补一个 assistant 空壳占位（只声明被打断，不回灌思维链），再补折标注的纠正 user。",
    ),
    Case(
        "05-toolcall-stop", "场景 3", "要调工具、参数还没吐完 —— stop", "报销有什么规定",
        "tool_start", "stop",
        note="半截 tool_call 不是合法消息、也没执行过 → 整步丢，尾部补 user 中断标记。",
    ),
    Case(
        "06-toolcall-redirect", "场景 3", "要调工具、参数还没吐完 —— redirect", "报销有什么规定",
        "tool_start", "redirect", redirect_text="先别查了，改成订会议室",
        note="没收到完整返回就当没收到：半截 tool_call 整步丢，只补折标注的纠正 user。",
    ),
    Case(
        "07-tool-running-stop", "场景 4", "tool 执行中 —— stop", "报销和 VPN 分别有什么规定，都要查",
        "tool_running", "stop",
        note="在跑的工具被当场掐死（不再等它跑完）；模型已发起的 tool_call 随 step 一起丢，尾部补 user 中断标记。",
    ),
    Case(
        "08-tool-running-redirect", "场景 4", "tool 执行中 —— redirect", "报销和 VPN 分别有什么规定，都要查",
        "tool_running", "redirect", redirect_text="先别查了，改成订会议室",
        note="同上：在跑的工具被当场掐死，不放封口，换成纯纠正 user（工具阶段 = steering，不加标注）。",
    ),
    Case(
        "09-tools-done-stop", "场景 5", "tool 刚好跑完 —— stop", "报销有什么规定",
        "tool_done", "stop",
        note="工具结果都真拿到了；尾部停在 tool → 补 assistant 封口占位（不是因为残缺，是 turn 要收口）。",
    ),
    Case(
        "10-half-answer-stop", "场景 6", "回答只说了一半 —— stop",
        "用三句话说说，事件驱动架构相比轮询好在哪儿",
        "text", "stop",
        note="模型在写最终回答、只说了一半就被掐掉；尾部仍是 user → 补 user 中断标记。",
    ),
)

CASE_IDS = tuple(c.id for c in CASES)


async def main(case_ids: list[str] | None = None, sessions_dir: Path | None = None) -> None:
    # session log 默认落在包级 sessions/stage03/；录制的 runner 会传自己的目录进来
    base_dir = sessions_dir or (Path(__file__).resolve().parents[2] / "sessions" / "stage03")
    base_dir.mkdir(parents=True, exist_ok=True)
    log_path = str(base_dir / "session.jsonl")
    bus = EventBus()
    log = SessionLog(log_path)
    agent = Agent(bus, log, RealLLM())

    turn_done = asyncio.Event()
    think_shown = [0]  # 本轮思考已显示字符数，到 300 截断
    streamed = {}  # sid -> 是否已通过 ui_delta 渲染过可见正文（避免 ui_reply 重复打印）

    # ------------------------------------------------------- 屏幕：订阅 outbound
    # LLM·思考 / LLM·回答两路走 stream_frame：同标签只起一次行首，之后直接续打（帧合并）。
    async def ui_thinking(e: Event) -> None:
        # 新一轮思考（标签切回来）就从头计截断
        if _LAST_LABEL != "LLM·思考":
            think_shown[0] = 0
        if think_shown[0] >= 300:
            return
        text = e.payload["text"]
        room = 300 - think_shown[0]
        shown = text[:room]
        think_shown[0] += len(shown)
        stream_frame("LLM·思考", DIM, shown + ("…（略）" if len(text) > room else ""))

    async def ui_delta(e: Event) -> None:
        stream_frame("LLM·回答", BLUE, e.payload["text"])
        streamed[e.session_id] = True

    async def ui_tool_call_started(e: Event) -> None:
        # 模型一开始调工具就亮出来：即便这一路被中断、agent_reply 没成型也看得到
        line("LLM(要求执行工具)", GREEN, "模型发起工具调用（参数流式到达）")

    async def ui_reply(e: Event) -> None:
        msg = e.payload["message"]
        sid = e.session_id
        if msg.get("tool_calls"):
            calls = ", ".join(
                f"{c['function']['name']}({c['function']['arguments']})"
                for c in msg["tool_calls"]
            )
            line("LLM(要求执行工具)", GREEN, f"→ {calls}")
        content = (msg.get("content") or "").strip()
        if content and not streamed.get(sid):
            # 模型没走增量流、正文只在最终 agent_reply 里：补渲染一次，
            # 否则屏幕上会只剩思考、看不见回答。
            line("LLM·回答", BLUE, content)
        elif not msg.get("tool_calls") and not content:
            note("最终回复：未调用工具，也无可见输出（模型只在思考里作答）")

    async def ui_tool_result(e: Event) -> None:
        p = e.payload
        args = p.get("args") or {}
        arg_s = ", ".join(f"{k}={v!r}" for k, v in args.items())
        tag = f"{p['name']}({arg_s})" if arg_s else p["name"]
        if p.get("skipped"):
            line("执行工具", RED, f"← {tag} 未执行（{p['result']}）")
        else:
            line("执行工具", GREEN, f"← {tag} 结果：{brief(p['result'])}")

    async def ui_step_cancelled(e: Event) -> None:
        intent = e.payload.get("intent", "stop")
        line(
            "系统",
            ORANGE,
            f"已掐掉在飞的那一步（intent={intent}，step_cancelled）",
        )

    async def ui_turn_interrupted(e: Event) -> None:
        intent = e.payload.get("intent", "stop")
        line(
            "系统",
            ORANGE,
            f"边界命中：step 没在飞，turn 在这里"
            f"{'转向' if intent == 'redirect' else '收尾'}"
            f"（intent={intent}，turn_interrupted）",
        )

    async def ui_turn_end(e: Event) -> None:
        reason = e.payload.get("reason", "")
        line("系统", ORANGE, f"turn 结束（reason={reason}）")
        turn_done.set()

    bus.subscribe("agent_thinking", ui_thinking)
    bus.subscribe("agent_delta", ui_delta)
    bus.subscribe("agent_reply", ui_reply)
    bus.subscribe("tool_call_started", ui_tool_call_started)
    bus.subscribe("tool_result", ui_tool_result)
    bus.subscribe("step_cancelled", ui_step_cancelled)
    bus.subscribe("turn_interrupted", ui_turn_interrupted)
    bus.subscribe("turn_end", ui_turn_end)

    # ------------------------------------------------------- 落点探测
    # 把每个 session 关心的事件收进队列，demo 用它决定"什么时候发中断"。
    seen: dict[tuple[str, str], asyncio.Queue] = {}

    async def record(e: Event) -> None:
        q = seen.get((e.session_id, e.type))
        if q is not None:
            q.put_nowait(e)

    for name in ("agent_thinking", "agent_delta", "tool_call_started", "tool_result"):
        bus.subscribe(name, record)

    def watch(sid: str, *types: str) -> None:
        for typ in types:
            seen[(sid, typ)] = asyncio.Queue()

    async def until(sid: str, typ: str, timeout: float = HIT_TIMEOUT) -> None:
        await asyncio.wait_for(seen[(sid, typ)].get(), timeout=timeout)



    # 场景 4 要一个"进去后卡住"的慢工具：中断落在它执行中时，step 被 cancel，
    # 下面的 await 就是 cancel 命中的点（事件永不 set，靠外部中断唤醒）。
    real_search = TOOLS["search_rules"]
    gate_in = asyncio.Event()

    async def gated_search(args: dict) -> str:
        gate_in.set()                      # 标记"工具开始执行"——hit 等这个
        await asyncio.Event().wait()       # 永不自发结束，专等中断 cancel
        return await real_search(args)     # 被 cancel 时不会跑到这里

    LANDING_EVENT = {
        "thinking": "agent_thinking",
        "tool_start": "tool_call_started",
        "tool_done": "tool_result",
        "text": "agent_delta",
    }
    LANDING_WHAT = {
        "immediate": "请求刚发出、一个增量都还没回（已发 LLM、未回复）",
        "thinking": "只吐了 thinking，可见文本一个字都还没有",
        "tool_start": "流里已出现 tool_call_delta（要调工具，参数没吐完、工具没执行）",
        "tool_running": "工具正在执行（当场掐死在跑的工具）",
        "tool_done": "工具已经拿到结果，模型还没给最终回答",
        "text": "模型在写最终回答、只说了一半",
    }

    async def hit(sid: str, landing: str) -> None:
        """等到目标落点——就是发中断的时机。没等到就按此刻发（尽力而为）。"""
        try:
            if landing == "immediate":
                await asyncio.sleep(0.05)  # 请求已发出，首个增量还没回来
            elif landing == "tool_running":
                await asyncio.wait_for(gate_in.wait(), timeout=HIT_TIMEOUT)
            elif landing == "thinking":
                # thinking 落点：只等 thinking。宁可超时也绝不在答案（agent_delta）
                # 或工具（tool_call_started）上命中——一旦退化成打断答案（场景 6）
                # 或工具（场景 3），"thinking-stop" 就名不副实了。
                try:
                    await until(sid, "agent_thinking", timeout=THINK_TIMEOUT)
                except asyncio.TimeoutError:
                    warn(
                        "场景 2 前提未满足：模型没在 "
                        f"{THINK_TIMEOUT:.0f}s 内吐 thinking，纯思考中断无法演示"
                        "（已发 stop，但命中的不是思考）"
                    )
            elif landing == "tool_start":
                # 工具落点：只等 tool_call_started。模型没调工具就绝不退化去打断答案
                # （那是场景 6），前提不满足时红色告警。
                try:
                    await until(sid, LANDING_EVENT["tool_start"])
                except asyncio.TimeoutError:
                    warn(
                        "场景 3 前提未满足：模型没调工具（tool_call_started 没出现），"
                        "工具调用中断无法演示（已发 stop，但命中的不是工具）"
                    )
            elif landing == "tool_done":
                try:
                    await until(sid, LANDING_EVENT["tool_done"])
                except asyncio.TimeoutError:
                    warn(
                        "场景 5 前提未满足：工具结果没回来（tool_result 没出现），"
                        "工具完成中断无法演示（已发 stop，但命中的不是工具结果）"
                    )
            elif landing == "text":
                try:
                    await until(sid, LANDING_EVENT["text"])
                except asyncio.TimeoutError:
                    warn(
                        "场景 6 前提未满足：模型没吐可见文本（agent_delta 没出现），"
                        "半句回答中断无法演示（已发 stop，但命中的不是回答）"
                    )
        except asyncio.TimeoutError:
            note("没等到目标落点，就在此刻发中断（尽力而为）")

    def dump_tail(sid: str) -> None:
        _end_stream_line()
        msgs = [m for m in agent.history.get(sid, []) if m.get("role") != "system"]
        for msg in msgs:
            print(f"{GREY}  {json.dumps(msg, ensure_ascii=False)}{RESET}")

    async def scenario(case: Case) -> None:
        """投一句问题 → 等落点 → 发中断 → 等收尾 → 打 history 尾部。"""
        sid = case.id
        watch(sid, "agent_thinking", "agent_delta", "tool_call_started", "tool_result")
        banner(
            f"{case.tag} · {case.id}",
            case.title,
            f"命中落点：{LANDING_WHAT[case.landing]}；意图：{case.intent}",
        )
        line("用户", YELLOW, f"[{sid}] {case.question}")
        turn_done.clear()
        bus.publish(Event("user_input", sid, {"text": case.question}), to=agent.agent_id)

        await hit(sid, case.landing)
        if case.intent == "stop":
            line("用户", RED, "按下停止（intent=stop）")
            bus.publish(Event("user_interrupt", sid, {"intent": "stop"}), to=agent.agent_id)
        else:
            line("用户", MAGENTA, f"改主意（intent=redirect）：{case.redirect_text}")
            bus.publish(
                Event(
                    "user_interrupt",
                    sid,
                    {"intent": "redirect", "text": case.redirect_text},
                ),
                to=agent.agent_id,
            )
        # tool_running 落点：工具正在执行，on_interrupt 会当场掐死在飞的 step
        #（不再 gate_open.set() 放行）——被掐的工具随 cancel 终止，不会跑完。
        try:
            await asyncio.wait_for(turn_done.wait(), timeout=TURN_TIMEOUT)
        except asyncio.TimeoutError:
            note("等 turn_end 超时")

        line("收尾", GREY, f"[{sid}] 完整 history（已略去 system）")
        dump_tail(sid)

    print(f"{BOLD}Stage 3：打断在飞的一步 —— 按命中落点逐个看收尾{RESET}")

    selected = [c for c in CASES if not case_ids or c.id in case_ids]
    if not selected:
        note(f"没有匹配的 case：{case_ids}；可选：{', '.join(CASE_IDS)}")
        await agent.stop()
        return
    if case_ids:
        note(f"只跑：{', '.join(c.id for c in selected)}")

    for case in selected:
        # 场景 4 要一个"进去后卡住"的慢工具，只在跑它时替换
        if case.landing == "tool_running":
            gate_in.clear()
            TOOLS["search_rules"] = gated_search
        try:
            await scenario(case)
        finally:
            if case.landing == "tool_running":
                TOOLS["search_rules"] = real_search
        if case.note:
            note(case.note)

    await agent.stop()
    print(f"\n{BOLD}── 收尾 ──{RESET}")
    note("中断只动在飞的那一步：worker 没死、history 完好，不影响下一轮。")
    line("系统", ORANGE, "demo 结束")
    print(f"{GREY}  session log: {log_path}{RESET}")


def cli() -> None:
    """[project.scripts] 入口：stage03-demo。

    用法：
        stage03-demo                       # 跑全部 case
        stage03-demo 07-tool-running-stop  # 只跑指定 case（名字见 --list）
        stage03-demo --list                # 列出 case 及其说明（不加载模型配置）
        stage03-demo 09-tools-done-stop --sessions-dir <目录>   # 换 session log 落点
    """
    parser = argparse.ArgumentParser(
        prog="stage03-demo", description="Stage 3 中断演示：按命中落点逐个看收尾。"
    )
    parser.add_argument(
        "cases", nargs="*", metavar="CASE", help="只跑指定 case（默认全部）；--list 看可选值"
    )
    parser.add_argument("--list", action="store_true", help="列出所有 case 后退出")
    parser.add_argument(
        "--sessions-dir", default=None, help="session log 落点（默认 sessions/stage03）"
    )
    args = parser.parse_args()
    if args.list:
        for case in CASES:
            what = f"{case.tag} · {case.title}"
            if case.note:  # 说明这个 case 看完该记住哪一条规则
                what += f"｜{case.note}"
            print(f"{case.id}\t{what}")
        return
    asyncio.run(
        main(args.cases or None, Path(args.sessions_dir) if args.sessions_dir else None)
    )


if __name__ == "__main__":
    cli()
