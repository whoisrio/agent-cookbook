"""Stage 4 演示：事件离开 agent 之后要走多远。

01~03、05 打真实模型（本地 ollama，读仓库根 `.env`），04 只测总线不打模型；
与正文对齐分三组：

- **01~03 下行的意图**：turn 在飞时的新消息默认 followup（排队不插话）；用户
  promote 把排队消息升级成 steering，下一个 step 边界拼进当前轮；用户打断
  （redirect 旁路）让纠正先落地，本轮转向。
- **04 UI 缓冲消费**：stream 增量进 CoalescingBuffer——offer 只做缓冲追加，
  满格 / 帧界才刷屏，一条不丢。
- **05 工具审批**：工具声明 requires_approval，执行前发 approval_required，
  答复走旁路直接 resolve，工具拿到授权才执行。

行首标签：

    用户 │ 用户说了什么（含模拟的插话 / 打断 / 批准）
    LLM·思考 │ assistant 的 thinking（暗色流，按帧合并）
    LLM·回答 │ assistant 的可见输出（亮蓝流，按帧合并）
    LLM(要求执行工具) │ 模型要求调用的工具（绿色）
    执行工具 │ 工具真实执行与结果（绿色；未执行用亮红）
    系统 │ 生命周期事件（turn 边界、插话消费、审批请求与回执，统一橙色）
    实测 │ 用例证据
    说明 │ 旁白，只解释这一段在演示什么（灰色缩进）

    stage03b-demo                              # 跑全部（默认）
    stage03b-demo 02-promote-to-steering       # 只跑一个 case
    stage03b-demo --list                       # 列出所有 case
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import shutil
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import tools as tools_mod
from .agent import Agent
from .bus import EventBus
from .events import Event, Subscription, UserMessage
from .llm import RealLLM
from .outbound import StreamConsumer
from .tools import TOOLS

# 本 stage 只跑真模型（配置读仓库根 .env：本地 ollama / OpenAI 兼容）。不再有离线回放。


def setup_console() -> None:
    """Windows 控制台默认 GBK（代码页 936）：不切 UTF-8，中文输出全是乱码。

    三步：控制台输出/输入代码页切 65001；Python 的 stdout/stderr 改用 UTF-8
    写出；`os.system("")` 让老 conhost 启用 ANSI 转义序列（Windows Terminal
    不需要，无害）。
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001 - 非 Windows / 无控制台环境不拦启动
        pass
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    os.system("")

# ---------------------------------------------------------------- 屏幕上色
DIM = "\033[2m"
GREY = "\033[90m"
BLUE = "\033[94m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[91m"
ORANGE = "\033[38;5;208m"  # 系统提示（生命周期/审批）统一橙色
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

_KB = Path(__file__).resolve().parents[2] / "knowledge-base"


_LAST_LABEL: str | None = None  # 当前正在续打的那一对流 up


def _end_stream_line() -> None:
    global _LAST_LABEL
    if _LAST_LABEL is not None:
        print()
        _LAST_LABEL = None


def line(label: str, color: str, text: str) -> None:
    _end_stream_line()
    print(f"\n{BOLD}{color}[{label}] {RESET}{color}{text}{RESET}")


def note(text: str) -> None:
    _end_stream_line()
    print(f"{GREY}       说明 │ {text}{RESET}")


def stream_frame(label: str, color: str, text: str) -> None:
    """同一路 stream 的帧界输出：接着上一帧打，不重复标签（否则每帧一行刷屏）。"""
    global _LAST_LABEL
    if _LAST_LABEL != label:
        _end_stream_line()
        print(f"{BOLD}{color}[{label}] {RESET}{color}", end="")
        _LAST_LABEL = label
    print(f"{color}{text}{RESET}", end="", flush=True)


def banner(n: int, title: str, what: str) -> None:
    """`── <case 名> · 第 n 段：<这段在看什么> ──`。名字就是录制产物名。"""
    print(f"\n{BOLD}── {CASE_ORDER[n - 1]} · 第 {n} 段：{title} ──{RESET}")
    note(what)


def brief(text: str, limit: int = 100) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


# ---------------------------------------------------------------- 脚手架

CASE_ORDER = (
    "01-followup-default",
    "02-promote-to-steering",
    "03-interrupt-redirect",
    "04-ui-coalescing-buffer",
    "05-approval-flow",
)
CASE_IDS = ("all", *CASE_ORDER)


# 已删除 ScriptedLLM：本 stage 只跑真模型（RealLLM），不再有离线回放。


class ThinkUI(StreamConsumer):
    """思考流消费者：和 UI 文本流同一个机制——offer 只做缓冲，帧界才打印。

    reasoning 动辄上千字，屏幕上只显示每轮的前 160 字，后面标"（略）"——
    机制照旧（缓冲 + 帧界合并刷屏），只是不把整段 reasoning 铺满终端。
    """

    def __init__(self) -> None:
        super().__init__()
        self._shown = 0

    def new_turn(self) -> None:
        self._shown = 0

    def on_flush(self, text: str) -> None:
        if self._shown >= 160:
            return
        room = 160 - self._shown
        shown = text[:room]
        self._shown += len(shown)
        stream_frame("LLM·思考", DIM, shown + ("…（略）" if len(text) > room else ""))


class TextUI(StreamConsumer):
    """回答流消费者：同样是邮箱型——delta 逐条缓冲，帧界才刷到屏幕上。"""

    def on_flush(self, text: str) -> None:
        stream_frame("LLM·回答", BLUE, text)


class Demo:
    """一个 case 一套总线 + agent + 常用订阅者；屏幕输出就是用例的证据。"""

    def __init__(self, llm: Any, *, approval_timeout: float = 10.0) -> None:
        self.bus = EventBus()
        self.agent = Agent(self.bus, llm, approval_timeout=approval_timeout)
        self.turn_ends: list[str] = []
        self._done = asyncio.Event()
        self._think_ui = ThinkUI()
        self._text_ui = TextUI()

    async def subscribe_default(self) -> None:
        """订阅这一 visibility 层：思考 / 回答两路 stream 各自缓冲，其余按事件打印。"""
        await self._think_ui.start()
        await self._text_ui.start()
        self.bus.subscribe(
            Subscription("d.think", ("agent_thinking",), self._think_ui)
        )
        self.bus.subscribe(Subscription("d.delta", ("agent_delta",), self._text_ui))
        async def on_input(event: Event) -> None:
            # 用户原文统一在 send 处打印（open_tool_window / 各 case 的
            # line("用户", …)），这里只负责每轮重置思考流的归属，不再重复打印。
            self._think_ui.new_turn()

        async def on_tool_result(event: Event) -> None:
            p = event.payload
            if p.get("skipped"):
                line("执行工具", RED, f"← {p['name']} 未执行（{brief(p['result'])}）")
            else:
                line("执行工具", GREEN, f"← {p['name']} 结果：{brief(p['result'])}")

        async def on_reply(event: Event) -> None:
            """assistant 消息成形时打印它发起的工具调用——流式文本已经由 UI 消费者刷屏了。

            先 flush 两路缓冲：这一轮剩下的字没到帧界的话，会滞后打印到工具调用
            甚至 turn 结束之后（屏幕上"回答被 turn 结束切成两半"）。
            """
            self.flush_streams()
            if event.payload.get("synthetic"):
                return  # 中断补位的合成消息：不是真发起的调用，不重复
            for call in (event.payload.get("message") or {}).get("tool_calls") or []:
                fn = call["function"]
                line(
                    "LLM(要求执行工具)",
                    GREEN,
                    f"→ {fn['name']}({brief(str(fn['arguments']), 140)})",
                )

        async def on_steer(event: Event) -> None:
            line("系统", ORANGE, "插话拼进本轮：" + "、".join(event.payload["texts"]))

        async def on_end(event: Event) -> None:
            self.flush_streams()  # 同上：turn 结束前把缓冲里的尾巴刷完
            reason = str(event.payload.get("reason"))
            self.turn_ends.append(reason)
            line("系统", ORANGE, f"turn 结束（reason={reason}）")
            self._done.set()

        self.bus.subscribe(Subscription("d.input", ("user_input",), on_input))
        self.bus.subscribe(Subscription("d.result", ("tool_result",), on_tool_result))
        self.bus.subscribe(Subscription("d.reply", ("agent_reply",), on_reply))
        self.bus.subscribe(Subscription("d.steer", ("steering_consumed",), on_steer))
        self.bus.subscribe(Subscription("d.end", ("turn_end",), on_end))

    def send(self, text: str) -> UserMessage:
        """发布一条用户消息（默认意图 followup），返回事件本身供 promote 用。"""
        msg = UserMessage("user_input", "A", {"text": text})
        self.bus.publish(msg, to=self.agent.agent_id)
        return msg

    def flush_streams(self) -> None:
        """把两路流缓冲区里没到帧界的尾巴立刻刷出来——打印顺序才对得上事件顺序。"""
        self._think_ui.flush()
        self._text_ui.flush()

    async def wait_turns(self, n: int, timeout: float = 10.0) -> None:
        while len(self.turn_ends) < n:
            await asyncio.wait_for(self._done.wait(), timeout)
            self._done.clear()
            # 多等一个帧界：让两路流消费者的尾巴刷完，免得打印滞到说明之后
            await asyncio.sleep(0.08)

    async def stop(self) -> None:
        await self._think_ui.stop()
        await self._text_ui.stop()
        await self.agent.stop()


def make_llm() -> Any:
    """只跑真模型（OpenAI 兼容，配置读仓库根 .env）。不再有离线回放。"""
    return RealLLM()


async def open_tool_window(
    instruction: str, replies: list[str], seconds: float = 0.5
) -> tuple[Demo, Any] | None:
    """发一条指令，等模型调起任意工具——拿到 01~03 需要的"turn 在飞"窗口。

    不预设模型必须调某个特定工具：把所有工具都换成慢探针，模型调哪个、哪个
    就"在飞"，started 在第一个工具真正执行时置位。模型先查库存、先检索还是
    直接改，窗口都能拿到——demo 不替模型做"调了什么工具"的判断。返回
    (demo, 原工具 dict)，调用方负责在 finally 里还原并 stop。
    """
    note("用户先发一条正常指令，模型开始执行工具——这一刻就是 01~03 要演示的“在飞”窗口：等模型调起的任意工具真跑起来")
    started = asyncio.Event()
    ended = asyncio.Event()
    originals = slow_all_tools(seconds, started)
    demo = Demo(make_llm())
    await demo.subscribe_default()

    async def on_window_end(event: Event) -> None:
        ended.set()  # 这一轮 turn 结束了：工具有没有跑起来，此刻就能下结论

    demo.bus.subscribe(Subscription("d.window-end", ("turn_end",), on_window_end))
    line("用户", YELLOW, instruction)
    demo.send(instruction)
    # 等"工具开始执行"或"这一轮 turn 结束"，谁先到听谁的——不等超时：turn 都
    # 结束了还等着的 40s 没有任何意义。
    tasks = {asyncio.create_task(started.wait()), asyncio.create_task(ended.wait())}
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED, timeout=40.0)
    for task in pending:
        task.cancel()
    if started.is_set():
        return demo, originals
    # 先把思考 / 回答的尾巴刷出来：让"模型到底说了什么"出现在说明之前
    demo.flush_streams()
    note(
        "这一轮 turn 已经结束，却没有工具开始执行——窗口没拿到，"
        "本段到此为止（模型整轮只回了文本、没调任何工具）"
    )
    TOOLS.update(originals)
    await demo.stop()
    return None


async def wait_turns(demo: "Demo", n: int, timeout: float = 90.0) -> None:
    """等 n 个 turn 结束。等不到就说明模型自己跑完了，不抛异常。"""
    try:
        await demo.wait_turns(n, timeout=timeout)
    except asyncio.TimeoutError:
        note(f"没有等到第 {n} 个 turn 结束（真模型的回合数不确定）")


def slow_all_tools(seconds: float, started: asyncio.Event) -> dict[str, Any]:
    """把所有工具都换成慢探针：模型调哪个、哪个就"在飞"，窗口不绑定某个特定工具。

    01~03 不演示审批，统一关掉 requires_approval，免得挂在等人那一步
    （审批只在 05 演示）。返回原工具 dict，供 finally 还原。
    """
    originals = dict(TOOLS)
    for name, tool in TOOLS.items():

        async def slow_spy(args: dict[str, Any], _fn: Any = tool.fn) -> str:
            started.set()
            await asyncio.sleep(seconds)
            return await _fn(args)

        TOOLS[name] = replace(tool, fn=slow_spy, requires_approval=False)
    return originals


# ---------------------------------------------------------------- 用例 1


# ---------------------------------------------------------------- 用例 4


async def ui_buffer_case() -> None:
    """UI 缓冲消费：offer 只做缓冲追加，满格 / 帧界才刷屏，一条不丢。"""
    banner(
        4,
        "UI 缓冲消费：满格 / 帧界才刷屏",
        "往总线灌 200 个 agent_delta。UI 是 StreamConsumer：offer 把增量攒进"
        "CoalescingBuffer——满 96 字立刻刷，到 50ms 帧界也刷；emit 不等刷屏。",
    )

    class FrameUI(StreamConsumer):
        def on_flush(self, text: str) -> None:
            line("UI", GREEN, f"刷一帧（{len(text)} 字）")

    bus = EventBus()
    ui = FrameUI()
    await ui.start()
    bus.subscribe(Subscription("ui", ("agent_delta",), ui))

    t0 = time.perf_counter()
    for _ in range(200):
        await bus.emit(Event("agent_delta", "A", {"text": "字"}))
    cost = time.perf_counter() - t0
    line(
        "实测",
        GREEN,
        f"200 个 delta 全部送达（merged={ui.merged}），emit 总耗时 {cost:.3f}s"
        "——emit 返回时事件只是进了缓冲",
    )

    await asyncio.sleep(0.12)  # 等两个帧界：不满一格的尾巴由 ticker 刷出去
    await ui.stop()
    line("实测", GREEN, f"200 次渲染合并成 {ui.flushes} 帧——刷屏节奏归消费者，不归总线")
    note("满了立刻刷（洪峰不攒着），到帧界也刷（尾巴不饿着），二者先到先刷。")


# ---------------------------------------------------------------- 用例 1


async def followup_case() -> None:
    """turn 在飞时的新消息：默认 followup，排队不插话。"""
    banner(
        1,
        "turn 在飞时的新消息：默认 followup",
        "agent 正在执行写工具。此刻用户再发一条消息——默认意图是 followup："
        "只排队，等当前 turn 结束后才作为新 turn 的主输入。",
    )
    opened = await open_tool_window(
        "把保温杯库存改成 3 件（category=保温杯, stock=3）",
        ["库存已更新。", "规则是：会议室提前一天预订。"],
        seconds=0.4,
    )
    if opened is None:
        return
    d, originals = opened
    try:
        line("用户", YELLOW, "顺便查一下规则")
        d.send("顺便查一下规则")
        note(
            "这条消息在 turn 在飞时发来：默认 followup 意图，进 followup inbox 排队，"
            "不插话、不打断，等当前 turn 结束后才作为新 turn 的主输入。"
        )

        await wait_turns(d, 2)
        note(
            "实测次序：turn 1（改库存）完整跑完 → turn 2（顺便查一下规则）才开始——"
            "排队没有丢，也没有插进第一轮。"
        )
    finally:
        TOOLS.update(originals)
        await d.stop()


# ---------------------------------------------------------------- 用例 2


async def promote_case() -> None:
    """插话：用户 promote 把排队的消息升级成 steering，step 边界拼进本轮。"""
    banner(
        2,
        "插话：把排队的消息升级成 steering（promote）",
        "消息已进 followup 队列。用户点名“这条别等了”——promote 把它精确移动到 "
        "steering 队列，下一个 step 边界按优先级拼进当前轮；本轮不断。",
    )
    opened = await open_tool_window(
        "帮我把保温杯的库存改成 3 件（category=保温杯, stock=3）",
        ["好的，库存已更新；插话看到的规则是：会议室提前一天预订。"],
        seconds=0.5,
    )
    if opened is None:
        return
    d, originals = opened
    try:
        # 模型正在执行工具时，用户又发来一条正常消息——它进 followup 队列等着
        line("用户", YELLOW, "等下，顺便也帮我查一下会议室预订规则")
        queued = d.send("等下，顺便也帮我查一下会议室预订规则")
        # 这条“等待中的消息”就是被 promote 的对象：用户正常输入，只是来得不是时候
        line("用户", YELLOW, "→ 等不了了，插话！（promote）")
        ok = d.agent.promote(queued)
        note(
            f"promote 返回 {ok}：用户这条“等待中的正常消息”被精确移动到 steering 队列，"
            "别的消息不动（同步操作，与 worker 取件天然互斥）。"
        )

        await wait_turns(d, 1)
        note(
            "实测：steering 在工具结果回来后、下一次与 LLM 交互前被消费——"
            "同一个 turn 没有断，这条等待消息直接插进本轮上下文。"
        )
    finally:
        TOOLS.update(originals)
        await d.stop()


# ---------------------------------------------------------------- 用例 3


async def redirect_case() -> None:
    """打断：redirect 旁路让纠正先落地，本轮转向；排队的消息不受影响。"""
    banner(
        3,
        "打断：redirect 让纠正先落地",
        "消息在 followup 队列里排队，但用户等不了——打断（user_interrupt 旁路）"
        "带着纠正文本进来：本轮在 step 边界转向，纠正第一时刻落进上下文；排队的"
        "消息不受影响，仍然等下一轮。",
    )
    opened = await open_tool_window(
        "把保温杯库存改成 3 件（category=保温杯, stock=3）",
        ["好的，先去订会议室，库存稍后再说。", "规则是：会议室提前一天预订。"],
        seconds=0.6,
    )
    if opened is None:
        return
    d, originals = opened
    try:
        line("用户", YELLOW, "顺便查一下规则")
        d.send("顺便查一下规则")

        line("用户", YELLOW, "→ 等不了：打断！先别改库存了，去订会议室（redirect）")
        d.bus.publish(
            Event(
                "user_interrupt",
                "A",
                {"intent": "redirect", "text": "先别改库存了，去订会议室"},
            ),
            to=d.agent.agent_id,
        )

        await wait_turns(d, 2)
        note(
            "实测次序：在飞的工具跑完这一步 → 纠正文本作为 user 消息落进本轮 → "
            "本轮回答新指令（turn 没有断）→ 排队的“顺便查一下规则”这才作为新 turn 开始。"
        )
    finally:
        TOOLS.update(originals)
        await d.stop()


# ---------------------------------------------------------------- 用例 5


async def approval_case(sessions_dir: Path) -> None:
    """工具审批：requires_approval → approval_required → 人批准 → 工具执行。"""
    banner(
        5,
        "工具审批：要等人的那一半",
        "update_inventory 自己声明 requires_approval。agent 发 approval_required"
        "（带 request_id），答复由人给——这里用订阅者模拟人点了一下批准。答复走"
        "旁路直接 resolve，工具拿到授权才执行。为了让 demo 反复跑，写工具落在 "
        "sessions/stage03b/ 的副本上。",
    )
    demo_inventory = sessions_dir / "inventory-demo.txt"
    demo_inventory.write_text(
        (_KB / "inventory.txt").read_text(encoding="utf-8"), encoding="utf-8"
    )
    original_path = tools_mod._INVENTORY
    tools_mod._INVENTORY = demo_inventory
    arrived = asyncio.Event()
    d = Demo(make_llm())
    await d.subscribe_default()

    async def on_required(event: Event) -> None:
        p = event.payload
        arrived.set()
        line(
            "系统",
            ORANGE,
            f"？ {p['name']} 要执行：{brief(p['arguments'])}"
            f"（request_id={p['request_id']}，超时 {p['timeout']:g}s）",
        )
        await asyncio.sleep(0.2)  # 人去点了一下
        line("用户", YELLOW, "→ 批准")
        d.bus.publish(
            Event(
                "user_approval",
                "A",
                {
                    "request_id": p["request_id"],
                    "approve": True,
                    "reason": "demo 里代替人点了一下",
                },
            ),
            to=d.agent.agent_id,
        )

    async def on_decided(event: Event) -> None:
        p = event.payload
        line("系统", ORANGE, f"确认结果：{p['action']} by {p['by']}")

    d.bus.subscribe(Subscription("d.required", ("approval_required",), on_required))
    d.bus.subscribe(Subscription("d.decided", ("approval_decided",), on_decided))
    try:
        line("用户", YELLOW, "把保温杯库存改成 45 件")
        d.send("把保温杯库存改成 45 件")
        try:
            await asyncio.wait_for(arrived.wait(), 40.0)
        except asyncio.TimeoutError:
            d.flush_streams()
            note(
                "没有等到 approval_required：模型没有发起工具调用，本段到此为止"
            )
            return
        await wait_turns(d, 1)
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
            "请求和结果都是事件：approval_required → 人批准 → approval_decided → "
            "工具执行。等不到答复时按拒绝处理（fail-closed），不会是“没人管就放行”。"
        )
    finally:
        tools_mod._INVENTORY = original_path
        await d.stop()


# ---------------------------------------------------------------- main

CASE_HANDLERS: dict[str, Any] = {
    "01-followup-default": lambda sessions_dir: followup_case(),
    "02-promote-to-steering": lambda sessions_dir: promote_case(),
    "03-interrupt-redirect": lambda sessions_dir: redirect_case(),
    "04-ui-coalescing-buffer": lambda sessions_dir: ui_buffer_case(),
    "05-approval-flow": approval_case,
}


async def main(
    case_ids: list[str] | None = None,
    sessions_dir: Path | None = None,
) -> None:
    sessions_dir = sessions_dir or (
        Path(__file__).resolve().parents[2] / "sessions" / "stage03b"
    )
    shutil.rmtree(sessions_dir, ignore_errors=True)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    picked = [c for c in CASE_ORDER if not case_ids or "all" in case_ids or c in case_ids]
    if not picked:
        print(f"{GREY}没有匹配的 case：{case_ids}；可选：{', '.join(CASE_IDS)}{RESET}")
        return
    print(f"{BOLD}Stage 4：消息机制 —— 事件离开 agent 之后要走多远{RESET}")
    print(
        f"{GREY}  01~03、05 的回答来自：真模型（本地 ollama，读仓库根 .env）；"
        f"04 只测总线，不打模型{RESET}"
    )
    if case_ids and "all" not in case_ids:
        print(f"{GREY}  （只跑：{', '.join(picked)}）{RESET}")
    for cid in picked:
        await CASE_HANDLERS[cid](sessions_dir)
    _end_stream_line()
    print(f"\n{GREY}  sessions 目录: {sessions_dir}{RESET}")


def cli() -> None:
    """[project.scripts] 入口：stage03b-demo。"""
    setup_console()
    parser = argparse.ArgumentParser(
        prog="stage03b-demo", description="Stage 4 演示：事件离开 agent 之后要走多远。"
    )
    parser.add_argument(
        "cases", nargs="*", metavar="CASE", help="要跑的 case（默认全部）"
    )
    parser.add_argument("--list", action="store_true", help="列出所有 case 后退出")
    parser.add_argument("--sessions-dir", default=None, help="session log 落点")
    args = parser.parse_args()
    if args.list:
        for cid in CASE_ORDER:
            print(f"{cid}")
        return
    asyncio.run(
        main(
            args.cases or None,
            Path(args.sessions_dir) if args.sessions_dir else None,
        )
    )


if __name__ == "__main__":
    cli()
