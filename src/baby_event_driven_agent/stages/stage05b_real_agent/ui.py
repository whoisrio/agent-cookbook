"""CLI UI（prompt_toolkit 全屏对话应用）：agent 之上的薄壳。

布局与网页对话应用一致：消息区在上方、从上往下生长、可翻页回看
（PageUp / PageDown / End，鼠标滚轮跟随），**输入框永远钉在底部**——
turn 进行中输入框不被占用：Enter 直接把新输入作为转向（steering）
插进当前轮，/stop 或 Ctrl+C 发打断，/quit 收尾退出。

四类内容四种颜色（prompt_toolkit style class，不再手写 ANSI）：
用户输入=黄（你 > ...）、模型思考=暗灰（▏思考 流式）、模型回复=洋红
（▏回答 流式）、工具执行=蓝（┌─ 名字+参数）/ 工具结果=青（└─ 全文，
不截断）；审批与裁决=橙，被拦 / 未执行如实标红 / 灰。

UI 只订阅总线事件做呈现，不参与任何机制；渲染是**事件驱动的单一来源**
（用户输入也由 user_input 事件上屏，accept 不重复打印）。测试与脚本
demo 注入 pipe input + DummyOutput 走同一个 Application——测试打的就是
真 UI。数据隔离沿用 04 纪律：会话与写操作落工作目录副本，包自带
data/ 一个字节不动。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea

from baby_event_driven_agent.stages.stage04_trajectory import tools as base_tools
from baby_event_driven_agent.stages.stage04_trajectory.agent import Agent
from baby_event_driven_agent.stages.stage04_trajectory.session.store import SessionStore
from baby_event_driven_agent.stages.stage04_trajectory.transport.bus import EventBus
from baby_event_driven_agent.stages.stage04_trajectory.transport.events import (
    OBSERVE,
    STEERING,
    Event,
    Subscription,
    UserMessage,
)
from baby_event_driven_agent.stages.stage04_trajectory.transport.persistence import EventLog

from . import knowledge as kb
from . import tools as tools_mod
from .tools import TOOL_SCHEMAS, TOOLS, RealLLM, build_system_prompt

# ---------------------------------------------------------------- 屏幕上色
# ANSI 常量留给 demo 的表格打印；ChatUI 的渲染用下面的 style class。

DIM = "\033[2m"
GREY = "\033[90m"
BLUE = "\033[94m"
CYAN = "\033[96m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
MAGENTA = "\033[95m"
RED = "\033[91m"
ORANGE = "\033[38;5;208m"
BOLD = "\033[1m"
RESET = "\033[0m"

APP_STYLE = Style.from_dict(
    {
        "prompt": "ansiyellow",
        "user": "ansiyellow",
        "thinking": "ansibrightblack",
        "reply": "ansimagenta",
        "tool": "ansiblue",
        "tool_result": "ansicyan",
        "tool_blocked": "ansired",
        "tool_skipped": "ansibrightblack",
        "approval": "ansiyellow",
        "granted": "ansigreen",
        "denied": "ansired",
        "notice": "ansibrightblack",
        "separator": "ansibrightblack",
        "frame.label": "ansibrightblack",
    }
)

UI_HELP = "Enter 发送 · 处理中 Enter=转向插话 · /stop 打断 · /quit 退出"


def brief(text: str, limit: int = 100) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def line(label: str, color: str, text: str) -> None:
    print(f"\n{BOLD}{color}[{label}] {RESET}{color}{text}{RESET}")


# ---------------------------------------------------------------- 数据隔离


def use_data_copies(workdir: Path) -> dict[str, Path]:
    """包自带 data/ 拷进工作目录并换掉两个工具模块的数据源（真模型可能写）。"""
    attrs = ("_INVENTORY", "_RULES", "_TASKS")
    saved = {f"stage04.{a}": getattr(base_tools, a) for a in attrs}
    saved["stage05b._KNOWLEDGE_DIR"] = tools_mod._KNOWLEDGE_DIR
    saved["stage05b._INDEX_PATH"] = tools_mod._INDEX_PATH
    for a in attrs:
        src: Path = saved[f"stage04.{a}"]
        dst = workdir / src.name
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        setattr(base_tools, a, dst)
    kb_dir = workdir / "knowledge"
    kb_dir.mkdir(parents=True, exist_ok=True)
    for src in saved["stage05b._KNOWLEDGE_DIR"].glob("*.md"):
        (kb_dir / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    tools_mod._KNOWLEDGE_DIR = kb_dir
    tools_mod._INDEX_PATH = workdir / "knowledge_index.json"
    return saved


def restore_data(saved: dict[str, Path]) -> None:
    for a in ("_INVENTORY", "_RULES", "_TASKS"):
        setattr(base_tools, a, saved[f"stage04.{a}"])
    tools_mod._KNOWLEDGE_DIR = saved["stage05b._KNOWLEDGE_DIR"]
    tools_mod._INDEX_PATH = saved["stage05b._INDEX_PATH"]


# ---------------------------------------------------------------- ChatUI


class ChatUI:
    """全屏对话 UI：bus + EventLog + store + agent（05b 工具表）+ 可滚动消息区 +
    底部常驻输入框。

    input / output 注入点：测试与脚本 demo 传 pipe + DummyOutput 走同一个
    Application；交互模式两者皆 None → prompt_toolkit 用真实控制台。
    """

    def __init__(
        self,
        workdir: Path,
        llm: Any = None,
        *,
        input: Any = None,  # noqa: A002 - 与 prompt_toolkit 参数同名
        output: Any = None,
        timeout: float = 180.0,
    ) -> None:
        self.log = EventLog(str(workdir / "events"))
        self.bus = EventBus(self.log)
        self.store = SessionStore(workdir / "sessions")
        self.system_prompt = build_system_prompt(TOOL_SCHEMAS)
        self.llm = llm if llm is not None else RealLLM()
        self.timeout = timeout
        self.agent = Agent(
            self.bus,
            self.llm,
            store=self.store,
            system_prompt=self.system_prompt,
            tools=TOOLS,
            approval_timeout=3.0,  # demo / 测试里没人值守：快速超时按拒绝
        )
        # 数据隔离：写操作落工作目录副本，包自带 data/ 一个字节不动
        self._saved_data = use_data_copies(workdir)
        self.traj = self.store.start(
            cwd=str(workdir),
            model=str(getattr(self.llm, "model", "")),
            system_prompt=self.system_prompt,
        )
        self.agent.attach(self.traj)
        self.sid = self.traj.sid
        # 消息区：每行 = [(style_class, text), ...]；流式输出改写最后一行
        self._lines: list[list[tuple[str, str]]] = [[]]
        self._stream_kind: str | None = None  # 正在流式上屏的内容：thinking / reply
        self._follow = True  # 跟随底部：翻页回看时置 False，回到底部恢复
        self._busy = False  # turn 在飞：此时新输入走 steering
        self._saw_reply = False  # 本轮是否有过可见回复（收尾时如实提示空轮）
        self.ended = asyncio.Event()
        self.events: list[Event] = []
        # io 注入：测试 / demo 传 pipe + DummyOutput；交互模式走真实控制台
        self._io_kwargs: dict[str, Any] = {}
        if input is not None:
            self._io_kwargs["input"] = input
        if output is not None:
            self._io_kwargs["output"] = output
        self._app: Application[None] | None = None
        self.bus.subscribe(Subscription("ui-end", ("turn_end",), self._on_end, mode=OBSERVE))
        self.bus.subscribe(Subscription("ui-any", ("*",), self._render, mode=OBSERVE))

    # ------------------------------------------------------------ 生命周期

    async def ensure_index(self) -> None:
        """首次对话前把知识索引备好（有索引用索引，没有就全量重建）。"""
        index = kb.KnowledgeIndex(
            tools_mod._KNOWLEDGE_DIR, tools_mod._INDEX_PATH, kb.get_embedder()
        )
        await index.ensure()

    async def close(self) -> None:
        await self.agent.stop()
        await self.bus.drain(timeout=self.timeout)
        await self.bus.close()
        restore_data(self._saved_data)

    def transcript(self) -> str:
        """消息区纯文本（测试断言用：样式剥掉，内容与上屏一致）。"""
        return "\n".join("".join(t for _, t in ln) for ln in self._lines)

    # ------------------------------------------------------------ 对话

    def send(self, text: str) -> None:
        """一条常规消息（FOLLOWUP）：进收件箱排队。"""
        self.bus.publish(Event("user_input", self.sid, {"text": text}), to=self.agent.agent_id)

    def steer(self, text: str) -> None:
        """turn 在飞时的插话（STEERING）：下一个 step 边界拼进当前上下文。"""
        self.bus.publish(
            UserMessage("user_input", self.sid, {"text": text}, intent=STEERING),
            to=self.agent.agent_id,
        )

    def interrupt(self) -> None:
        """打断当前 turn（不附新消息）：收尾占位由 agent 负责。"""
        self.bus.publish(Event("user_interrupt", self.sid, {}), to=self.agent.agent_id)

    async def say(self, text: str) -> None:
        """一条用户消息走完整 turn（渲染走同一份订阅回调，与 chat_loop 无异）。"""
        self.ended.clear()
        self._stream_kind = None
        self._busy = True  # 同步置位：accept 判"转向还是新消息"不用等渲染回调
        first_event = len(self.events)
        self.send(text)
        try:
            await asyncio.wait_for(self.ended.wait(), self.timeout)
            while self.agent._turn_active.get(self.sid):
                await asyncio.sleep(0.01)
            await self.bus.drain(timeout=self.timeout)
            if not any(
                e.type == "agent_reply" and not e.payload.get("synthetic")
                for e in self.events[first_event:]
            ):
                self._append_line(
                    "notice", "（这一轮模型没有产出可见回复——重发一次通常就好）"
                )
        except asyncio.TimeoutError:
            self._append_line("denied", f"[超时] {self.timeout:g}s 内没有等到 turn 结束")
        finally:
            self._busy = False

    # ------------------------------------------------------------ 全屏应用

    def _build_app(self) -> None:
        """组装全屏 Application：消息区在上滚动生长，输入框钉在底部。"""
        self._output_win = Window(
            FormattedTextControl(self._get_text, show_cursor=False),
            wrap_lines=True,
        )
        input_area = TextArea(
            prompt=[("class:prompt", "你 > ")],
            multiline=False,
            accept_handler=self._accept,
        )
        kb = self._key_bindings()

        @kb.add("c-c")
        def _on_ctrl_c(event: Any) -> None:  # pragma: no cover - 交互路径
            self.interrupt()
            self._append_line("notice", "（已发送打断，等待本轮收尾……）")

        self._app = Application(
            layout=Layout(
                HSplit(
                    [
                        self._output_win,
                        Window(height=1, char="─", style="class:separator"),
                        Frame(
                            input_area,
                            title=f" {UI_HELP} ",
                        ),
                    ]
                ),
                focused_element=input_area,
            ),
            style=APP_STYLE,
            full_screen=True,
            mouse_support=True,
            key_bindings=kb,
            **self._io_kwargs,
        )

    def _key_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("pageup")
        def _pageup(event: Any) -> None:
            self._follow = False  # 回看模式：新消息不再把视图拽到底部
            info = self._output_win.render_info
            step = info.window_height if info else 10
            self._output_win.vertical_scroll = max(0, self._output_win.vertical_scroll - step)

        @kb.add("pagedown")
        def _pagedown(event: Any) -> None:
            self._follow = True
            self._pin_bottom()

        @kb.add("end")
        def _end(event: Any) -> None:
            self._follow = True
            self._pin_bottom()

        return kb

    def _get_text(self) -> FormattedText:
        frags: list[tuple[str, str]] = []
        for ln in self._lines:
            frags.extend(ln)
            frags.append(("", "\n"))
        return FormattedText(frags)

    def _pin_bottom(self) -> None:
        """跟随底部：把滚动位置压到最大，渲染时被夹进合法区间。"""
        if self._follow and getattr(self, "_output_win", None) is not None:
            self._output_win.vertical_scroll = 10**9

    def _invalidate(self) -> None:
        self._pin_bottom()
        if self._app is not None:
            self._app.invalidate()

    # ---- 消息区的行操作（_render 与流式共用的唯一写入口） ----

    def _append(self, style: str, text: str) -> None:
        if not text:
            return
        self._lines[-1].append((style, text))
        self._invalidate()

    def _newline(self) -> None:
        self._lines.append([])
        self._invalidate()

    def _append_line(self, style: str, text: str) -> None:
        self._append(style, text)
        self._newline()

    def _close_stream(self) -> None:
        """结束一段流式上屏（思考 / 回答）：收掉当前行。"""
        if self._stream_kind is not None:
            self._newline()
            self._stream_kind = None

    async def _on_end(self, event: Event) -> None:
        self.ended.set()

    # ------------------------------------------------------------ 渲染（事件驱动）

    async def _render(self, event: Event) -> None:
        self.events.append(event)
        t = event.type
        if t == "user_input":
            if not event.payload.get("synthetic"):
                # 用户输入的唯一上屏点：accept 只发事件，不直接画
                self._saw_reply = False
                self._close_stream()
                self._append_line("class:user", f"你 > {event.payload['text']}")
        elif t == "agent_thinking":
            # 模型思考：暗灰流式——前缀开行，后续增量接在同一行
            if self._stream_kind != "thinking":
                self._close_stream()
                self._append("class:thinking", "▏思考 ")
                self._stream_kind = "thinking"
            self._append("class:thinking", str(event.payload["text"]))
        elif t == "agent_delta":
            # 模型回复：洋红流式——同一条消息内连续增量共用一行
            if self._stream_kind != "reply":
                self._close_stream()
                self._append("class:reply", "▏回答 ")
                self._stream_kind = "reply"
            self._append("class:reply", str(event.payload["text"]))
        elif t == "before_tool_call":
            # 工具执行：名字 + 模型给的完整参数——执行了什么、按什么参数执行
            self._close_stream()
            args_text = str(event.payload.get("arguments", "")).strip()
            self._append_line("class:tool", f"┌─ {event.payload.get('name', '')} {args_text}")
        elif t == "approval_required":
            self._close_stream()
            self._append_line(
                "class:approval",
                f"【审批】{event.payload.get('name')}：{event.payload.get('reason')}",
            )
        elif t == "approval_decided":
            self._close_stream()
            action = str(event.payload.get("action"))
            cls = "class:granted" if action == "allow" else "class:denied"
            self._append_line(
                cls,
                f"【裁决】{action}（{event.payload.get('by')}）："
                f"{brief(event.payload.get('reason', ''), 60)}",
            )
        elif t == "tool_result":
            # 工具结果=青（└─），完整呈现不截断；被拦 / 未执行如实标红 / 灰
            self._close_stream()
            blocked = bool(event.payload.get("blocked"))
            skipped = bool(event.payload.get("skipped"))
            cls = (
                "class:tool_blocked"
                if blocked
                else ("class:tool_skipped" if skipped else "class:tool_result")
            )
            result = str(event.payload.get("result", ""))
            lines = result.splitlines() or [""]
            self._append(cls, f"└─ {lines[0]}")
            self._newline()
            for ln in lines[1:]:
                self._append(cls, f"   {ln}")
                self._newline()
        elif t == "agent_reply":
            # 回复已流式上屏：只收行；没流过且带文本才整行补出
            if event.payload.get("synthetic"):
                return
            streamed = self._stream_kind == "reply"
            self._close_stream()
            content = str(event.payload.get("message", {}).get("content") or "")
            if not streamed and content.strip():
                self._append_line("class:reply", f"▏回答 {content}")
            self._saw_reply = True
        elif t == "turn_end":
            self._close_stream()
            self._busy = False
            if not self._saw_reply:
                self._append_line(
                    "class:notice", "（这一轮模型没有产出可见回复——重发一次通常就好）"
                )
            self.ended.set()

    # ------------------------------------------------------------ 输入框

    def _accept(self, buffer: Any) -> None:
        """Enter：空闲 = 常规消息；turn 在飞 = 转向插话；/stop = 打断；/quit = 退出。

        输入框始终可打字——处理中的窗口不是阻占的（steering / interrupt 走
        各自旁路，不进收件箱队列）。
        """
        text = buffer.text.strip()
        buffer.reset()
        if not text:
            return
        if text in {"/quit", "/exit", "quit", "exit"}:
            self._quit()
            return
        if text == "/stop":
            self.interrupt()
            self._append_line("class:notice", "（已发送打断，等待本轮收尾……）")
            return
        if self._busy or bool(self.agent._turn_active.get(self.sid)):
            self.steer(text)
            self._append_line("class:user", f"（转向）你 > {text}")
            return
        assert self._app is not None
        self._busy = True  # 同步置位：不等 say 任务真正跑起来
        self._app.create_background_task(self.say(text))

    def _quit(self) -> None:
        """退出：turn 在飞时等它收尾（先 /stop 可立即打断），再关应用。"""

        async def _wait_then_exit() -> None:
            while self._busy:
                await asyncio.sleep(0.05)
            assert self._app is not None
            self._app.exit()

        assert self._app is not None
        self._app.create_background_task(_wait_then_exit())

    async def chat_loop(self) -> None:
        """全屏应用主循环：跑直到 /quit。"""
        self._build_app()
        model = str(getattr(self.llm, "model", "")) or "unknown"
        self._append_line("class:notice", f"知识库运营助手（{model}）  {UI_HELP}")
        assert self._app is not None
        await self._app.run_async()
