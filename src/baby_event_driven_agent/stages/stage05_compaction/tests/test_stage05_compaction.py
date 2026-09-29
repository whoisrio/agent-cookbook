"""Stage 05（压缩策略）真模型用例：骑在 04 的共享代码上，全部打真实本地 LLM。

用例按章归位在本目录（每章一条 test 入口，stage05-test）；实现代码与
04 共用一份（agent / session / tools / transport 都在 stage04_trajectory），
本文件的 import 指向那里——05 的增量（水位自动触发、cap+blob、滚动折叠、
分页工具）落地后，import 原地翻转到本包。已支持的部分先行落地，并且
**不用剧本替身**：压缩的行为断言（摘要保真、副作用不重做）只有真模型才算数。
ScriptedSummarizer / ScriptedLLM 只保留给离线单测（stage04_trajectory tests）。

前置：仓库根 .env 配好 OPENAI_API_BASE（本地 ollama）与 OPENAI_MODEL。
用例对应 05 章 demo / 验证清单：

1. 手动压缩端到端：真模型写摘要，视图收缩、消息序列合法；
2. 被压段的事实凭摘要可续：tool 结果原文已不在视图里，模型照样答得出规格；
3. 副作用不重做：写操作被压掉之后，模型不再重写（摘要保住副作用）；
4. resume：压缩视图从盘上还原，与关会话前一致（摘要落盘即事实）；
5. rewind 回归：branch 回压缩前，旧消息逐字回来，文件一个字节不动。

注意：真模型用例有固有波动（模型可能多查一次、措辞不同），断言只钉
机制事实（entry、事件、投影形状、写操作计数），措辞类断言放宽到关键词。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from baby_event_driven_agent.stages.stage04_trajectory import tools as tools_mod
from baby_event_driven_agent.stages.stage04_trajectory.agent import Agent, build_context
from baby_event_driven_agent.stages.stage04_trajectory.llm import RealLLM
from baby_event_driven_agent.stages.stage04_trajectory.session.compaction import CompactionPolicy
from baby_event_driven_agent.stages.stage04_trajectory.session.store import SessionStore
from baby_event_driven_agent.stages.stage04_trajectory.session.trajectory import COMPACTION
from baby_event_driven_agent.stages.stage04_trajectory.transport.bus import EventBus
from baby_event_driven_agent.stages.stage04_trajectory.transport.events import (
    OBSERVE,
    Event,
    Subscription,
)
from baby_event_driven_agent.stages.stage04_trajectory.transport.persistence import EventLog

# 单个 turn 的等待上限：真模型 + 压缩调用（摘要可能重试一次）都比普通 turn 慢。
# 本地思考型模型（qwen3.5 系）单次摘要 thinking 可达 1~2 分钟，重试翻倍，给足余量。
TIMEOUT = 420.0
# 一个用例的整体上限
TOTAL_TIMEOUT = 1800.0


@pytest.fixture()
def workdir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stage05_compact_live_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def data_copies(workdir: Path) -> None:
    """数据源换到工作目录副本；结束还原（写操作不碰包自带 data/）。"""
    attrs = ("_INVENTORY", "_RULES", "_TASKS")
    saved = {a: getattr(tools_mod, a) for a in attrs}
    for a in attrs:
        src: Path = saved[a]
        dst = workdir / src.name
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        setattr(tools_mod, a, dst)
    yield
    for a in attrs:
        setattr(tools_mod, a, saved[a])


@pytest.fixture()
def real_llm() -> Any:
    """真模型客户端（本地 ollama 也能跑）；没配 .env 就跳过本用例。"""
    try:
        return RealLLM()
    except RuntimeError as exc:
        pytest.skip(f"未配置真模型（.env），跳过：{exc}")


class Harness:
    """真模型 agent + 事件观测：turn_end / tool_result / agent_reply / 压缩事件。"""

    def __init__(self, workdir: Path, llm: Any, *, policy: Any | None = None) -> None:
        self.log = EventLog(str(workdir / "events"))
        self.bus = EventBus(self.log)
        self.store = SessionStore(workdir / "sessions")
        # 默认 keep_steps=1：保留窗最小，压缩收益看得见
        self.agent = Agent(
            self.bus,
            llm,
            store=self.store,
            compaction_policy=policy or CompactionPolicy(keep_steps=1),
        )
        self.traj = self.store.start(cwd=str(workdir))
        self.sid = self.agent.attach(self.traj)
        self.ended = asyncio.Event()
        self.tool_results: list[Event] = []
        self.replies: list[Event] = []
        self.compact_events: list[Event] = []
        self.bus.subscribe(
            Subscription("rec-end", ("turn_end",), self._on_end, mode=OBSERVE)
        )
        self.bus.subscribe(
            Subscription("rec-tool", ("tool_result",), self._on_tool, mode=OBSERVE)
        )
        self.bus.subscribe(
            Subscription("rec-reply", ("agent_reply",), self._on_reply, mode=OBSERVE)
        )
        self.bus.subscribe(
            Subscription(
                "rec-compact",
                ("context_compacted", "context_compact_failed"),
                self._on_compact,
                mode=OBSERVE,
            )
        )

    async def _on_end(self, event: Event) -> None:
        self.ended.set()

    async def _on_tool(self, event: Event) -> None:
        self.tool_results.append(event)

    async def _on_reply(self, event: Event) -> None:
        self.replies.append(event)

    async def _on_compact(self, event: Event) -> None:
        self.compact_events.append(event)

    def send(self, text: str) -> None:
        self.bus.publish(Event("user_input", self.sid, {"text": text}), to=self.agent.agent_id)

    def compact(self, reason: str = "manual") -> None:
        """手动压缩命令：下一个 step 边界生效（与 04 同一入口，仅 reason 不同）。"""
        self.bus.publish(
            Event("compact_request", self.sid, {"reason": reason}), to=self.agent.agent_id
        )

    async def wait_turn(self) -> None:
        await asyncio.wait_for(self.ended.wait(), TIMEOUT)
        self.ended.clear()
        # turn_end 在 _run_turn 的 finally 清理之前发出：等 _turn_active 落 False
        # （收尾真正完成）再返回，否则紧跟其后的 compact_request 可能被收尾的
        # pending 清理吞掉，压缩静默不触发。
        while self.agent._turn_active.get(self.sid):
            await asyncio.sleep(0.01)

    async def run_turns(self, texts: list[str]) -> None:
        for text in texts:
            self.send(text)
            await self.wait_turn()

    async def stop(self) -> None:
        await self.agent.stop()


# ------------------------------------------------------------------ 观测辅助


def _compaction_entries(traj: Any) -> list[Any]:
    return [e for e in traj.entries() if e.type == COMPACTION]


def _executed_tool_results(h: Harness, name: str) -> list[Event]:
    """真实执行过的某工具结果（排除被拦 / 未执行的合成占位）。"""
    return [
        e
        for e in h.tool_results
        if e.payload.get("name") == name
        and not e.payload.get("skipped")
        and not e.payload.get("blocked")
    ]


def _last_reply_text(h: Harness) -> str:
    """最后一条非合成的 assistant 回复文本。"""
    texts = [
        str(e.payload.get("message", {}).get("content") or "")
        for e in h.replies
        if not e.payload.get("synthetic")
    ]
    return texts[-1] if texts else ""


# ------------------------------------------------------------------ 用例


def test_real_compaction_shrinks_view_and_stays_legal(
    workdir: Path, real_llm: Any, data_copies: None
) -> None:
    """手动压缩端到端（真模型摘要）：entry 落盘、事件报账、新视图合法且更短。

    三轮查库存 → compact_request → 下一轮边界压缩。断言：compaction entry
    （reason=manual、非空摘要）；context_compacted 事件（消息数下降、无失败）；
    新视图 = [system, <摘要>, 保留窗…]；保留窗里 tool 配对原样合法。
    纯查询用例也换数据副本：真模型什么都可能干，不许碰包自带 data/。
    """
    h = Harness(workdir, real_llm)

    async def go() -> None:
        await h.run_turns(["保温杯还有库存吗", "玻璃杯呢", "马克杯还有吗"])
        h.compact()
        h.send("汇总一下三个品类的情况")
        await h.wait_turn()
        await h.bus.drain(timeout=60.0)
        await h.stop()

    asyncio.run(asyncio.wait_for(go(), TOTAL_TIMEOUT))

    comps = _compaction_entries(h.traj)
    assert len(comps) == 1
    assert comps[0].payload["reason"] == "manual"
    assert len(str(comps[0].payload["summary"])) > 20  # 真模型写出了非空摘要
    # 事件报账：恰好一次成功（无失败留痕），消息数下降
    assert [e.type for e in h.compact_events] == ["context_compacted"]
    payload = h.compact_events[0].payload
    assert payload["messages_after"] < payload["messages_before"]
    # 新视图：[system, <摘要>, 保留窗…]
    proj = build_context(h.traj)
    assert proj.messages[0]["role"] == "system"
    assert str(proj.messages[1]["content"]).startswith("<summary>")
    assert not any("保温杯还有库存吗" == str(m.get("content")) for m in proj.messages[1:])
    # 保留窗消息序列合法：每个 tool 结果都挂在带 tool_calls 的 assistant 下
    # （并行多调用时连续多个 tool 合法，用未应答调用计数验配对，不数相邻角色）
    pending = 0
    for m in proj.messages:
        if m.get("role") == "assistant":
            assert pending == 0, "assistant 出现在未回答完的 tool_calls 之后"
            pending = len(m.get("tool_calls") or [])
        elif m.get("role") == "tool":
            assert pending > 0, "tool 结果没有配对的 tool_calls"
            pending -= 1
    assert pending == 0, "tool_calls 悬空：结果不完整"


def test_fact_survives_compaction_via_summary(
    workdir: Path, real_llm: Any, data_copies: None
) -> None:
    """被压段的事实凭摘要可续；摘要失败走 fail-open，下一边界重试成功。

    用用户口述、无法重查的事实（VIP 客户取货电话）——工具结果可以被模型
    重新查一遍，堵不死"重查"这条路；口述事实只有摘要这一条延续通道。
    真模型的摘要偶尔返回空（推理 token 吃掉输出）：空摘要按失败处理——
    留痕（context_compact_failed）、不 append、不挡 turn，重发请求下一
    边界重试（05 章 demo 4 的 fail-open 纪律）。
    """
    fact = "记住一个信息：本店 VIP 客户是王先生，取货预留电话 13800001234。"
    h = Harness(workdir, real_llm)

    async def go() -> None:
        await h.run_turns([fact, "玻璃杯还有多少", "好的"])
        # 先把压缩做成功：失败就重发（fail-open 后视图原样，事实仍在）
        for _ in range(3):
            h.compact()
            h.send("继续")
            await h.wait_turn()
            if _compaction_entries(h.traj):
                break
        # 压缩成功后再问：事实只剩摘要这一条延续通道
        h.send("王先生的取货预留电话是多少？")
        await h.wait_turn()
        await h.bus.drain(timeout=60.0)
        await h.stop()

    asyncio.run(asyncio.wait_for(go(), TOTAL_TIMEOUT))

    assert len(_compaction_entries(h.traj)) == 1
    succeeded = [e for e in h.compact_events if e.type == "context_compacted"]
    failed = [e for e in h.compact_events if e.type == "context_compact_failed"]
    assert len(succeeded) == 1
    for e in failed:  # 失败留痕且不挡 turn：错误说明空摘要，视图原样未损
        assert "空摘要" in str(e.payload.get("error", ""))
    proj = build_context(h.traj)
    # 摘要在视图最前，且保住了关键事实（电话号码）
    assert str(proj.messages[1]["content"]).startswith("<summary>")
    assert "13800001234" in str(proj.messages[1]["content"])
    # 原文不在投影：被压段被摘要替代（不是原样保留）
    assert not any(fact == str(m.get("content")) for m in proj.messages)
    # 模型凭摘要答得出来
    reply = _last_reply_text(h)
    assert "13800001234" in reply, f"回答丢了摘要里的事实：{reply!r}"


def test_compacted_write_is_not_repeated(
    workdir: Path, real_llm: Any, data_copies: None
) -> None:
    """副作用不重做：写操作被压掉之后，模型凭摘要知道"做过就是做过"。

    第一轮改库存（小改动免审批）→ 后两轮把它推进被压段 → 压缩 → 问汇报。
    断言：全程恰好一次真实 update_inventory；落库值正确（设值语义）；
    压缩确实发生（写操作落在被压段）。
    """
    h = Harness(workdir, real_llm)

    async def go() -> None:
        await h.run_turns(["把保温杯的库存改成 10 件", "玻璃杯还有多少", "好的，先这样"])
        h.compact()
        h.send("汇报一下今天改了哪些库存。保温杯还需要再改吗？")
        await h.wait_turn()
        await h.bus.drain(timeout=60.0)
        await h.stop()

    asyncio.run(asyncio.wait_for(go(), TOTAL_TIMEOUT))

    # 压缩发生，且写操作落在被压段（刀口之前）
    comps = _compaction_entries(h.traj)
    assert len(comps) == 1
    summary = str(comps[0].payload["summary"])
    # 摘要保住副作用（写/改操作要显式标注，防重复执行）
    assert "10" in summary or "保温杯" in summary
    # 全程恰好一次真实写：被压掉的那次。模型没有"以为没做成功"再调一次
    writes = _executed_tool_results(h, "update_inventory")
    assert len(writes) == 1
    # 落库值正确（设值语义）：保温杯 10 件
    assert "保温杯：库存 10 件" in (workdir / "inventory.txt").read_text(encoding="utf-8")


def test_resume_after_real_compaction_restores_view(
    workdir: Path, real_llm: Any, data_copies: None
) -> None:
    """resume × 压缩（真模型摘要落盘即事实）：恢复出的投影与关会话前一致。

    三轮 + 压缩 + 续一轮 → 关会话 → 新 store resume。断言：投影逐字段一致；
    append-only（resume 前字节是 resume 后的前缀）。
    """
    h = Harness(workdir, real_llm)

    async def go() -> None:
        await h.run_turns(["保温杯还有库存吗", "玻璃杯呢", "帆布包呢"])
        h.compact()
        h.send("继续")
        await h.wait_turn()
        await h.bus.drain(timeout=60.0)
        await h.stop()

    asyncio.run(asyncio.wait_for(go(), TOTAL_TIMEOUT))

    assert len(_compaction_entries(h.traj)) == 1
    before = build_context(h.traj)
    assert str(before.messages[1]["content"]).startswith("<summary>")
    bytes_before = h.traj.log.raw_bytes()

    store2 = SessionStore(workdir / "sessions")
    resumed = store2.resume(h.sid, note="压缩后重启恢复演练")
    after = build_context(resumed)
    assert [m["content"] for m in after.messages if m.get("content")] == [
        m["content"] for m in before.messages if m.get("content")
    ]
    # append-only：只追加了 session_resumed，原文一个字节没动
    assert resumed.log.raw_bytes().startswith(bytes_before)


def test_rewind_before_compaction_restores_original(
    workdir: Path, real_llm: Any, data_copies: None
) -> None:
    """rewind 回归：branch 回刀口，压缩整个退出路径，旧消息逐字回来。

    压缩是当前路径上的视图，不是对数据的手术：branch 到 keep_from 本身，
    投影回到压缩前 full 原文（无摘要），文件一个字节不动。
    """
    h = Harness(workdir, real_llm)

    async def go() -> None:
        await h.run_turns(["保温杯还有库存吗", "玻璃杯呢", "马克杯还有吗"])
        h.compact()
        h.send("继续")
        await h.wait_turn()
        await h.bus.drain(timeout=60.0)
        await h.stop()

    asyncio.run(asyncio.wait_for(go(), TOTAL_TIMEOUT))

    comps = _compaction_entries(h.traj)
    assert len(comps) == 1
    keep_from = str(comps[0].payload["keep_from_id"])
    raw_before = h.traj.log.raw_bytes()

    h.traj.branch(keep_from)  # rewind：leaf 移到刀口，compaction 不在路径上
    proj = build_context(h.traj)
    contents = [str(m.get("content", "")) for m in proj.messages]
    assert not any(c.startswith("<summary>") for c in contents)  # 摘要退出视图
    assert "保温杯还有库存吗" in contents  # 第一轮原文逐字回来
    assert "马克杯还有吗" in contents  # 刀口所在的轮也原样在
    assert h.traj.log.raw_bytes() == raw_before  # branch 不写文件：一个字节不动
