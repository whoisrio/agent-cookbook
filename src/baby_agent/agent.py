"""Baby Agent — create_agent + middleware 示例。

展示三种 middleware 用法：
- tools middleware：通过 middleware 注册工具
- steering middleware：会话中紧急插队注入消息（共享队列，实时生效）
- followup middleware：agent 完成后自动追加任务
"""

import os
import subprocess
import time
from queue import Queue

from dotenv import dotenv_values
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState, AgentMiddleware, after_agent, before_model
from langchain_core.tools import tool as tool_decorator
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.runtime import Runtime

# .env 位于仓库根目录
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_config = dotenv_values(os.path.join(_REPO_ROOT, ".env"))

# 把 .env 里的键全量注入进程环境（真实环境变量优先）。
# 不能只注入 OPENAI_*：LangSmith tracer 在运行时读 LANGSMITH_* 环境变量，
# 缺了就不会上报 trace。必须在任何 agent.run/invoke 之前完成。
for _key, _val in _config.items():
    if _val is not None:
        os.environ.setdefault(_key, _val)


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------

@tool_decorator
def run_command(command: str) -> str:
    """Execute a shell command and return its output.

    Args:
        command: The shell command to run (e.g. "ls -la", "cat file.txt").
    """
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=30,
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr] {result.stderr}" if output else result.stderr
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: command timed out (30s)"
    except Exception as e:
        return f"Error: {e}"


@tool_decorator
def read_file(path: str) -> str:
    """Read a file and return its contents.

    Args:
        path: Path to the file to read.
    """
    try:
        return open(path, encoding="utf-8").read()
    except Exception as e:
        return f"Error: {e}"


@tool_decorator
def write_file(path: str, content: str) -> str:
    """Write content to a file.

    Args:
        path: Path to the file to write.
        content: Content to write.
    """
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

# 故意让 calc_nums 变慢，模拟真实的长耗时工具（构建、外部 API 等）。
# 这是演示 steering 价值的关键：工具跑着的时候用户插话，before_model 会在
# 工具结果返回的下一轮把队列里的 steering 消息一并交给模型。
CALC_DELAY_SECONDS = 5.0


@tool_decorator
def calc_nums(left: int, right: int, operator: str) -> str:
    """Compute a basic arithmetic operation on two integers.

    This tool intentionally takes a few seconds to simulate a long-running
    computation, so that steering messages injected mid-run can be observed.

    Args:
        left: The left operand.
        right: The right operand.
        operator: One of "add" (+), "subtract" (-), "multiply" (*),
            "divide" (/). Symbol aliases "+", "-", "*", "/" are also accepted.
    """
    if CALC_DELAY_SECONDS > 0:
        time.sleep(CALC_DELAY_SECONDS)
    ops = {
        "add": lambda a, b: a + b,
        "subtract": lambda a, b: a - b,
        "multiply": lambda a, b: a * b,
        "divide": lambda a, b: a / b,
        "+": lambda a, b: a + b,
        "-": lambda a, b: a - b,
        "*": lambda a, b: a * b,
        "/": lambda a, b: a / b,
    }
    key = operator.strip().lower()
    if key not in ops:
        return f"Error: unsupported operator {operator!r}. Use add/subtract/multiply/divide (or + - * /)."
    try:
        result = ops[key](left, right)
    except ZeroDivisionError:
        return "Error: division by zero."
    # 整数结果去掉无意义的 .0，其余保留浮点
    if isinstance(result, float) and result.is_integer():
        result = int(result)
    return f"{left} {operator} {right} = {result}"



# ---------------------------------------------------------------------------
# Middleware：Tools
# ---------------------------------------------------------------------------

class ToolsMiddleware(AgentMiddleware):
    """通过 middleware 注册工具。"""

    tools = [run_command, read_file, write_file, calc_nums]


# ---------------------------------------------------------------------------
# Middleware：FollowUp（任务追加）
# ---------------------------------------------------------------------------

def _drain_queue(queue: Queue) -> list:
    """非阻塞地把队列里所有消息取出来。"""
    items = []
    while not queue.empty():
        try:
            items.append(queue.get_nowait())
        except Exception:
            break
    return items


def make_followup_middleware(followup_queue: Queue):
    """构造 after_agent middleware，从外部共享队列读取追加任务。

    用闭包持有 followup_queue 而不是从 state 读——graph 运行期间外部无法
    回写 state，只有进程内共享 Queue 才能把运行中用户敲入的 /followup
    实时送进来。
    """

    @after_agent(can_jump_to=["model"])
    def followup_middleware(state: AgentState, runtime: Runtime) -> dict | None:
        """Agent 完成后检查 followUp 队列，有任务就重启循环。"""
        follow_ups = _drain_queue(followup_queue)
        if not follow_ups:
            return None
        return {
            "messages": follow_ups,
            "jump_to": "model",
        }

    return followup_middleware


# ---------------------------------------------------------------------------
# 创建 Agent
# ---------------------------------------------------------------------------

def create_baby_agent(
    model_name: str | None = None,
    steering_queue: Queue | None = None,
    followup_queue: Queue | None = None,
):
    """创建 baby agent 实例。

    Args:
        model_name: 模型名称，默认从 .env 读取 OPENAI_MODEL。
        steering_queue: 共享队列，外部线程往里放消息，
                        before_model 每轮循环自动消费。
                        传 None 则不启用 steering。
        followup_queue: 共享队列，外部线程往里放追加任务，
                        after_agent 每轮循环结束时消费并 jump_to=model。
                        传 None 则不启用 followup。

    Returns:
        编译好的 LangGraph agent。
    """
    resolved_model = model_name or _config.get("OPENAI_MODEL") or "gpt-4o-mini"
    model = ChatOpenAI(model=resolved_model, temperature=0.7)

    # steering middleware：从共享队列读取，不经过 checkpoint
    middleware_list: list[AgentMiddleware] = [ToolsMiddleware()]

    if steering_queue is not None:
        @before_model
        def steering_middleware(
            state: AgentState, runtime: Runtime
        ) -> dict | None:
            """每轮循环前检查共享队列，有消息就注入。"""
            msgs = _drain_queue(steering_queue)
            if not msgs:
                return None
            return {"messages": msgs}

        middleware_list.append(steering_middleware)

    if followup_queue is not None:
        middleware_list.append(make_followup_middleware(followup_queue))

    return create_agent(
        model=model,
        system_prompt="""You are an expert coding assistant operating inside a command-line coding agent. You help the user with software engineering tasks: reading, searching, editing, and running code, and explaining how things work.
Guidelines:
- Be concise in your responses; prefer actions over prose.
- Show file paths and line numbers clearly when referring to code.
- Do not guess. Read the file before editing it; verify before claiming something works.
- When unsure, inspect the project rather than assume.
- Explain trade-offs when you recommend an approach.""",
        middleware=middleware_list,
        tools=[calc_nums],
        checkpointer=MemorySaver(),
    )
