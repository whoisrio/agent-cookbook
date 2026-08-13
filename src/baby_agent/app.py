"""Baby Agent TUI — 底部固定输入框 + 逐字 stream 输出。

Steering 实现：用共享队列，agent 运行中用户输入 /steering，
消息直接入队，before_model 下一轮立即消费。
"""

import uuid
from queue import Queue

from langchain_core.messages import AIMessageChunk, HumanMessage
from rich.markdown import Markdown
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, Static


class BabyAgentApp(App):
    """Baby Agent 的 Textual TUI。"""

    TITLE = "Baby Agent"
    CSS = """
    Screen {
        layout: vertical;
    }

    #chat {
        height: 1fr;
        overflow-y: auto;
        padding: 0 1;
    }

    #input-bar {
        dock: bottom;
        height: auto;
        max-height: 5;
        padding: 0 1;
    }

    #user-input {
        width: 100%;
    }
    """

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+d", "quit", "Quit"),
    ]

    def __init__(self, agent, steering_queue: Queue, followup_queue: Queue) -> None:
        super().__init__()
        self.agent = agent
        self.steering_queue = steering_queue  # 共享队列，before_model 消费
        self.followup_queue = followup_queue  # 共享队列，after_agent 消费
        self.thread_id: str = str(uuid.uuid4())[:8]
        self._config: dict = {"configurable": {"thread_id": self.thread_id}}
        self._history: list[str] = []
        self._streaming_reply: str = ""
        self._agent_running: bool = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Static("等待输入...  输入 `/help` 查看命令", id="chat")
        with Vertical(id="input-bar"):
            yield Input(placeholder="输入问题...  (/steering 可在运行中插队)", id="user-input")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#user-input", Input).focus()

    # ── 渲染 ────────────────────────────────────────────────────

    def _render(self) -> None:
        parts = list(self._history)
        if self._streaming_reply:
            parts.append(f"**Agent:** {self._streaming_reply}")
        text = "\n\n".join(parts) if parts else "等待输入..."
        chat = self.query_one("#chat", Static)
        chat.update(Markdown(text))
        chat.scroll_end(animate=False)

    # ── 输入处理 ─────────────────────────────────────────────────

    @on(Input.Submitted, "#user-input")
    def on_input(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.query_one("#user-input", Input).value = ""

        if not text:
            return

        if text == "/quit":
            self.exit()
            return

        if text == "/help":
            self._history.append(
                "**命令:**\n"
                "- `/steering <消息>` — 紧急插队（agent 运行中即时生效）\n"
                "- `/followup <消息>` — 任务追加\n"
                "- `/quit` — 退出"
            )
            self._render()
            return

        # ── steering：直接入共享队列，before_model 下一轮立即消费 ──
        if text.startswith("/steering "):
            msg = text[len("/steering "):]
            self.steering_queue.put(HumanMessage(content=msg))
            self._history.append(f"📌 steering 已注入: `{msg}`")
            self._render()
            return

        if text.startswith("/followup "):
            msg = text[len("/followup "):]
            self.followup_queue.put(HumanMessage(content=msg))
            self._history.append(f"📋 followup 队列: `{msg}`")
            self._render()
            return

        # ── 正常对话 ──
        self._history.append(f"**You:** {text}")
        self._streaming_reply = ""
        self._render()
        self._set_input_enabled(False)
        self._run_agent(text)

    def _set_input_enabled(self, enabled: bool) -> None:
        inp = self.query_one("#user-input", Input)
        inp.disabled = not enabled
        if enabled:
            inp.focus()

    # ── Agent 后台线程 ───────────────────────────────────────────

    @work(exclusive=True, thread=True)
    def _run_agent(self, user_text: str) -> None:
        self._agent_running = True
        input_state: dict = {"messages": [HumanMessage(content=user_text)]}

        self._streaming_reply = ""
        current_msg_id: str | None = None

        for event in self.agent.stream(
            input_state, config=self._config, stream_mode="messages"
        ):
            msg, _ = event

            if not isinstance(msg, AIMessageChunk):
                continue
            if getattr(msg, "reasoning_content", None):
                continue
            if msg.additional_kwargs.get("reasoning_content"):
                continue
            if not isinstance(msg.content, str) or not msg.content:
                continue

            if msg.id != current_msg_id:
                if self._streaming_reply:
                    self.call_from_thread(self._commit_reply)
                current_msg_id = msg.id
                self._streaming_reply = ""

            self._streaming_reply += msg.content
            self.call_from_thread(self._render)

        if self._streaming_reply:
            self.call_from_thread(self._commit_reply)
        elif current_msg_id is None:
            self.call_from_thread(self._commit_empty)

        self._agent_running = False
        self.call_from_thread(self._set_input_enabled, True)

    def _commit_reply(self) -> None:
        self._history.append(f"🤖 **Agent:** {self._streaming_reply}")
        self._streaming_reply = ""
        self._render()

    def _commit_empty(self) -> None:
        self._history.append("🤖 **Agent:** *(无回复)*")
        self._streaming_reply = ""
        self._render()
