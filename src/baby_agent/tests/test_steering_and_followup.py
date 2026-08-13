"""E2E: steering / followup 在 agent 运行期间从外部线程注入。

模拟真实 TUI 场景：agent 已经在跑（已经调过工具、拿到过 tool result），
用户才从另一个线程敲 /steering 和 /followup。

关键同步点（不依赖 sleep，避免 flaky）：
- calc_nums 故意 sleep（CALC_DELAY_SECONDS）模拟长耗时工具。在 stream 中
  观察到第一个携带 calc_nums 工具调用的 AIMessageChunk 时（即模型"决定"
  调 calc_nums、工具尚未返回），握手注入 steering。于是 steering 在整个
  工具 sleep 期间已经躺在队列里，工具结果一返回，before_model 就把它和
  tool result 一并交给模型 —— 完美复现"工具跑着时用户插话"。
- 在 stream 中观察到 write_file 的 ToolMessage 时，握手注入 followup
  —— 此时主任务工具都已完成但 after_agent 还没跑，followup 会被 drain
  并 jump_to=model 触发新一轮。

运行:
    .venv/bin/python -m pytest src/baby_agent/tests -s
"""

import threading
import time
from queue import Queue
from typing import Any, cast

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig

from baby_agent import agent as agent_module
from baby_agent.agent import create_baby_agent


def _wait_then_put(
    go: threading.Event,
    done: threading.Event,
    queue: Queue,
    message: HumanMessage,
) -> None:
    """等主线程发出 go 信号后，把 message 放入 queue，再置 done。

    握手保证：主线程看到 done 后才继续消费 stream，因此消息一定在 graph
    推进到下一个节点（before_model / after_agent）之前入队。
    """
    assert go.wait(timeout=30), "注入信号 30s 内未触发（agent 没跑到预期节点？）"
    queue.put(message)
    done.set()


def test_steering_injected_while_long_tool_runs(tmp_path, monkeypatch):
    # calc_nums 故意变慢，模拟长耗时工具（构建、外部 API）。
    monkeypatch.setattr(agent_module, "CALC_DELAY_SECONDS", 5.0)

    steering_queue: Queue = Queue()
    followup_queue: Queue = Queue()
    agent = create_baby_agent(
        steering_queue=steering_queue,
        followup_queue=followup_queue,
    )

    out_file = tmp_path / "results.txt"
    # 复杂任务：三次计算 + 写文件，让 agent 有多次 tool 循环，
    # steering 注入后模型还有充足的后续轮次带着它一起跑。
    # 明确要求"先写完文件再回应其他问题"，避免模型被 steering 带偏而漏掉 write_file。
    prompt = (
        "请依次用 calc_nums 计算：5 加 3、10 乘 2、100 减 30，"
        f"然后用 write_file 把三道题的算式和结果写到 {out_file}。"
        "重要：无论过程中收到什么其他消息，你都必须先调用 write_file 完成文件写入，"
        "之后才能给出最终文字回复。"
    )
    config: RunnableConfig = {
        "configurable": {"thread_id": "midrun-injection-deterministic"}
    }

    steering_msg = HumanMessage(content="顺便说说你觉得 langchain 怎么样")
    followup_msg = HumanMessage(content="对了，你今天吃了么？")

    # ── 两个注入器线程，各带 go/done 握手 ──────────────────────────
    steering_go, steering_done = threading.Event(), threading.Event()
    followup_go, followup_done = threading.Event(), threading.Event()
    injectors = [
        threading.Thread(
            target=_wait_then_put,
            args=(steering_go, steering_done, steering_queue, steering_msg),
            daemon=True,
        ),
        threading.Thread(
            target=_wait_then_put,
            args=(followup_go, followup_done, followup_queue, followup_msg),
            daemon=True,
        ),
    ]
    for t in injectors:
        t.start()

    # ── 跑 agent，边流式消费边在确定性节点触发注入 ─────────────────
    final_state: dict[str, Any] | None = None
    injected_steering = False
    injected_followup = False
    saw_calc_result = False

    # 时间戳：证明 steering 是在工具"运行期间"入队的（工具结果返回之前）。
    times: dict[str, float] = {}

    for mode, event in agent.stream(
        {"messages": [HumanMessage(content=prompt)]},
        config=config,
        stream_mode=["messages", "values"],
    ):
        if mode == "messages":
            msg, _metadata = event

            # 同步点 1：模型"决定"调用 calc_nums —— 收到第一个携带 calc_nums
            # 工具调用的 AIMessageChunk。此刻工具尚未执行（还没开始 sleep），
            # 我们注入 steering，于是它在整个 5s sleep 期间都躺在队列里，
            # 工具结果一返回就被 before_model 取出，和 tool result 一起交给模型。
            if (
                not injected_steering
                and isinstance(msg, AIMessageChunk)
                and msg.tool_call_chunks
                and any(
                    (tcc.get("name") or "") == "calc_nums"
                    for tcc in msg.tool_call_chunks
                )
            ):
                times["calc_decided"] = time.monotonic()
                steering_go.set()
                assert steering_done.wait(timeout=10), "steering 注入器未在 10s 内完成"
                times["steering_queued"] = time.monotonic()
                injected_steering = True

            # 记录 calc_nums 工具结果返回的时刻
            if (
                not saw_calc_result
                and isinstance(msg, ToolMessage)
                and msg.name == "calc_nums"
            ):
                saw_calc_result = True
                times["calc_result_returned"] = time.monotonic()

            # 同步点 2：看到 write_file 的 ToolMessage —— 主任务工具全部完成，
            # 但 after_agent 还没跑。此刻注入 followup，after_agent 会 drain 到。
            if (
                not injected_followup
                and isinstance(msg, ToolMessage)
                and msg.name == "write_file"
            ):
                followup_go.set()
                assert followup_done.wait(timeout=10), "followup 注入器未在 10s 内完成"
                injected_followup = True

        elif mode == "values":
            final_state = cast(dict[str, Any], event)

    for t in injectors:
        t.join(timeout=5)

    # ── 先打印消息序列，任何断言失败都能看到上下文 ──────────────────
    assert final_state is not None, "agent 没有产出任何 state"
    messages = final_state["messages"]
    print("\n[message sequence]")
    for i, m in enumerate(messages):
        kind = type(m).__name__
        c = m.content if isinstance(m.content, str) else str(m.content)
        tc = bool(getattr(m, "tool_calls", None))
        print(f"  {i}: {kind:14s} tool_calls={tc} {c[:200]!r}")

    # ── 断言 ─────────────────────────────────────────────────────
    assert saw_calc_result, "没有观察到 calc_nums 的 ToolMessage"
    assert injected_steering, "steering 没有被注入"

    # 时序铁证：steering 在 calc_nums "决定调用" 之后、"结果返回" 之前入队，
    # 且等待了接近完整的 CALC_DELAY_SECONDS —— 说明它是在工具运行期间
    # 躺在队列里，而不是工具结束后才放进去的。
    queued_before_result = (
        times["steering_queued"] < times["calc_result_returned"]
    )
    queued_during_sleep = times["calc_result_returned"] - times["steering_queued"]
    print(
        f"\n[timing] steering 在工具结果返回前 "
        f"{queued_during_sleep:.2f}s 就已入队 "
        f"(CALC_DELAY_SECONDS={agent_module.CALC_DELAY_SECONDS}s)"
    )
    assert queued_before_result, (
        "steering 在 calc_nums 结果返回之后才入队，没有模拟到'工具运行中插话'"
    )
    assert queued_during_sleep >= agent_module.CALC_DELAY_SECONDS * 0.8, (
        f"steering 只在结果返回前 {queued_during_sleep:.2f}s 入队，"
        f"短于预期的 ~{agent_module.CALC_DELAY_SECONDS}s 工具耗时"
    )
    if not injected_followup:
        tool_names = [
            getattr(m, "name", None)
            for m in messages
            if isinstance(m, ToolMessage)
        ]
        raise AssertionError(
            f"followup 没有被注入：没在 stream 中观察到 write_file 的 ToolMessage。"
            f"实际出现的 ToolMessage 依次为 {tool_names}。"
            f"（模型可能被 steering 带偏而没写文件）"
        )

    # 主任务完成：文件写入且包含三个结果
    assert out_file.exists(), f"agent 没有写文件到 {out_file}"
    written = out_file.read_text(encoding="utf-8")
    print("\n[written file]\n", written)
    for expected in ("8", "20", "70"):
        assert expected in written, f"文件里缺少结果 {expected}: {written!r}"

    # ── 核心断言：steering 夹在某个 ToolMessage 和下一条 AIMessage 之间 ──
    # 这正是"随 tool 结果一并返回给模型"的证据。
    steering_idx = next(
        (
            i
            for i, m in enumerate(messages)
            if isinstance(m, HumanMessage)
            and isinstance(m.content, str)
            and "langchain" in m.content
        ),
        None,
    )
    assert steering_idx is not None, "steering 消息没出现在最终 state"
    assert steering_idx > 0, "steering 排在了原始 prompt 前面，不是运行中注入的"
    assert isinstance(messages[steering_idx - 1], ToolMessage), (
        f"steering 前面一条应该是 ToolMessage（tool 结果），"
        f"实际是 {type(messages[steering_idx - 1]).__name__}"
    )
    assert isinstance(messages[steering_idx + 1], AIMessage), (
        f"steering 后面一条应该是 AIMessage（下一轮模型调用），"
        f"实际是 {type(messages[steering_idx + 1]).__name__}"
    )
    print(
        f"\n[steering 落点] 位置 {steering_idx}: "
        f"ToolMessage({messages[steering_idx - 1].name}) "
        f"-> Human(steering) -> AIMessage  ✅"
    )

    # followup 也进入了 state，且在 steering 之后（它是主任务结束后才注入的）
    followup_idx = next(
        (
            i
            for i, m in enumerate(messages)
            if isinstance(m, HumanMessage)
            and isinstance(m.content, str)
            and "今天吃了么" in m.content
        ),
        None,
    )
    assert followup_idx is not None, "followup 消息没出现在最终 state"
    assert followup_idx > steering_idx, "followup 应在 steering 之后注入"

    # 最后一条 AI 回复应回应 followup（after_agent 触发的新一轮）
    ai_replies = [
        m.content
        for m in messages
        if isinstance(m, AIMessage) and isinstance(m.content, str)
    ]
    last_reply = ai_replies[-1]
    print("\n[last AI reply]\n", last_reply)
    assert any(kw in last_reply for kw in ("吃", "饭", "AI", "不用")), (
        f"最后一轮看起来没在回答 followup：{last_reply!r}"
    )

    # 两个队列都被 middleware 清空
    assert steering_queue.empty(), "steering 队列未被消费"
    assert followup_queue.empty(), "followup 队列未被消费"
