"""Stage 5 压缩策略演示（[project.scripts] 入口：stage05-demo）。

与 05 章 demo 对应。实现与 04 共享一份（import 指向 stage04_trajectory），
压缩机制（水位计量、step 刀口、滚动折叠）已落地，本文件五段：

  01-compact-before-after     压缩前后对照：entry、摘要全文、投影换视图
  02-compact-fail-open        摘要失败 fail-open：留痕、不 append、下一边界重试
  03-side-effect-not-repeated 副作用不重做：写被压掉之后恰好一次真实写
  04-watermark-trigger        触发策略对照：token 计量决定“压不压”（demo 1）
  05-fold-twice               二次折叠：新摘要吞旧摘要、投影认最后一切（demo 4）

“其他影响上下文的一些策略”一章的段落（分页与批次句柄、cap + blob）
不在本书 demo 范围内，对应章节原计划的 demo 2 / 3 不实现。
全部真模型（读仓库根 .env，本地 ollama 也行），数据落工作目录副本。
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
from pathlib import Path
from typing import Any

from baby_event_driven_agent.stages.stage04_trajectory import tools as tools_mod
from baby_event_driven_agent.stages.stage04_trajectory.agent import Agent, build_context
from baby_event_driven_agent.stages.stage04_trajectory.llm import RealLLM
from baby_event_driven_agent.stages.stage04_trajectory.session.compaction import (
    CompactionPolicy,
    LiveSummarizer,
    Summarizer,
    trigger_tokens,
)
from baby_event_driven_agent.stages.stage04_trajectory.session.store import SessionStore
from baby_event_driven_agent.stages.stage04_trajectory.session.trajectory import (
    COMPACTION,
    TrajectoryLog,
)
from baby_event_driven_agent.stages.stage04_trajectory.transport.bus import EventBus
from baby_event_driven_agent.stages.stage04_trajectory.transport.events import (
    OBSERVE,
    Event,
    Subscription,
)
from baby_event_driven_agent.stages.stage04_trajectory.transport.persistence import EventLog

# ---------------------------------------------------------------- 屏幕上色

DIM = "\033[2m"
GREY = "\033[90m"
BLUE = "\033[94m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[91m"
ORANGE = "\033[38;5;208m"
BOLD = "\033[1m"
RESET = "\033[0m"


def line(label: str, color: str, text: str) -> None:
    print(f"\n{BOLD}{color}[{label}] {RESET}{color}{text}{RESET}")


def note(text: str) -> None:
    print(f"{GREY}       说明 │ {text}{RESET}")


def banner(name: str, title: str, what: str) -> None:
    print(f"\n{BOLD}── {name} · {title} ──{RESET}")
    note(what)


def brief(text: str, limit: int = 110) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def use_data_copies(workdir: Path) -> dict[str, Path]:
    """包自带 data/ 拷进工作目录并换掉工具的数据源（真模型可能写，不许碰原件）。"""
    attrs = ("_INVENTORY", "_RULES", "_TASKS")
    saved = {a: getattr(tools_mod, a) for a in attrs}
    for a in attrs:
        src: Path = saved[a]
        dst = workdir / src.name
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        setattr(tools_mod, a, dst)
    return saved


def restore_data(saved: dict[str, Path]) -> None:
    for attr, path in saved.items():
        setattr(tools_mod, attr, path)


# ---------------------------------------------------------------- 脚手架


class EmptyOnceSummarizer:
    """第一次返回空摘要（推理模型思考 token 吃掉输出时的真实姿态），之后透传。"""

    def __init__(self, inner: Summarizer) -> None:
        self.inner = inner
        self.calls = 0

    async def summarize(
        self, segment: list[dict[str, Any]], previous: str | None = None
    ) -> str:
        self.calls += 1
        if self.calls == 1:
            return ""
        return await self.inner.summarize(segment, previous=previous)


class RecordingSummarizer:
    """透传包装：记下每次传入的 previous（上一刀摘要）——观测滚动折叠用。"""

    def __init__(self, inner: Summarizer) -> None:
        self.inner = inner
        self.previous: list[str | None] = []

    async def summarize(
        self, segment: list[dict[str, Any]], previous: str | None = None
    ) -> str:
        self.previous.append(previous)
        return await self.inner.summarize(segment, previous=previous)


class Harness:
    """demo 台子：bus + EventLog + store + agent（真模型），外加压缩入口。"""

    def __init__(
        self,
        workdir: Path,
        *,
        summarizer: Summarizer | None = None,
        policy: CompactionPolicy | None = None,
        llm: Any = None,
    ) -> None:
        self.log = EventLog(str(workdir / "events"))
        self.bus = EventBus(self.log)
        self.store = SessionStore(workdir / "sessions")
        self.system_prompt = "你是一个通过工具干活的通用 agent。"
        self.llm = llm or RealLLM()
        self.timeout = 180.0  # 真模型段放宽等待
        self.policy = policy or CompactionPolicy(keep_steps=1)
        self.agent = Agent(
            self.bus,
            self.llm,
            store=self.store,
            system_prompt=self.system_prompt,
            summarizer=summarizer,
            compaction_policy=self.policy,
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
        self.ended = asyncio.Event()
        self.events: list[Event] = []

        async def on_end(event: Event) -> None:
            self.ended.set()

        async def on_any(event: Event) -> None:
            self.events.append(event)

        self.bus.subscribe(Subscription("rec-end", ("turn_end",), on_end, mode=OBSERVE))
        self.bus.subscribe(Subscription("rec-any", ("*",), on_any, mode=OBSERVE))

    def send(self, text: str) -> None:
        self.bus.publish(Event("user_input", self.sid, {"text": text}), to=self.agent.agent_id)

    def compact(self, reason: str = "manual") -> None:
        """手动压缩命令：下一个 step 边界生效（与 04 同一入口，仅 reason 不同）。"""
        self.bus.publish(
            Event("compact_request", self.sid, {"reason": reason}), to=self.agent.agent_id
        )

    async def wait_turn(self) -> None:
        await asyncio.wait_for(self.ended.wait(), self.timeout)
        self.ended.clear()
        # turn_end 在收尾清理之前发出：等 _turn_active 落 False，紧跟其后的
        # compact_request 才不会被收尾的 pending 清理吞掉
        while self.agent._turn_active.get(self.sid):
            await asyncio.sleep(0.01)
        await self.bus.drain(timeout=self.timeout)

    async def stop(self) -> None:
        await self.agent.stop()
        await self.bus.drain(timeout=self.timeout)
        await self.bus.close()
        restore_data(self._saved_data)

    # ------------------------------------------------------------ 观测辅助

    def compaction_entry(self) -> Any | None:
        return next((e for e in self.traj.entries() if e.type == COMPACTION), None)

    def compact_events(self, type: str) -> list[Event]:
        return [e for e in self.events if e.type == type]

    def projection_brief(self) -> str:
        proj = build_context(self.traj)
        roles = [m.get("role") for m in proj.messages]
        return (
            f"{len(proj.messages)} 条消息（"
            + "、".join(f"{r}×{roles.count(r)}" for r in dict.fromkeys(roles))
            + "）"
        )


# ---------------------------------------------------------------- demo 1：压缩前后对照


async def case_compact_before_after(workdir: Path) -> None:
    banner(
        "01-compact-before-after",
        "压缩前后对照（真模型摘要）",
        "三轮查库存 → compact_request → 下一轮边界压缩。看四样：compaction entry"
        "（摘要全文 + 刀口）、context_compacted 事件报账、压前压后投影对比、"
        "原文一个字节没删。",
    )
    try:
        h = Harness(workdir)
    except RuntimeError as exc:
        line("系统", ORANGE, f"跳过真模型段：{exc}")
        return
    for text in ("保温杯还有库存吗", "玻璃杯呢", "马克杯还有吗"):
        line("用户", YELLOW, text)
        h.send(text)
        await h.wait_turn()

    line("压缩前投影", BLUE, h.projection_brief())
    h.compact()
    h.send("汇总一下三个品类的情况")
    await h.wait_turn()
    await h.stop()

    entry = h.compaction_entry()
    assert entry is not None, "压缩应已发生"
    line(
        "entry",
        GREEN,
        f"{entry.id}  reason={entry.payload['reason']}  "
        f"keep_from={entry.payload['keep_from_id']}",
    )
    print(f"{GREY}  摘要全文：{brief(entry.payload['summary'], 220)}{RESET}")
    evt = h.compact_events("context_compacted")[0]
    line(
        "事件",
        GREEN,
        f"context_compacted：消息 {evt.payload['messages_before']} → "
        f"{evt.payload['messages_after']} 条",
    )
    line("压缩后投影", BLUE, h.projection_brief())
    proj = build_context(h.traj)
    line("新视图头部", BLUE, f"{proj.messages[0]['role']} | {brief(proj.messages[1]['content'], 80)}")
    total = len(h.traj.entries())
    note(f"全树 {total} 条 entry 原样都在——压缩只换视图，不动事实层。")
    note("摘要插在 system 之后、保留窗之前；刀口之前的消息由摘要替代。")


# ---------------------------------------------------------------- demo 2：fail-open


async def case_compact_fail_open(workdir: Path) -> None:
    banner(
        "02-compact-fail-open",
        "摘要失败 fail-open：留痕、不 append、下一边界重试",
        "摘要器第一次被注入空返回（推理模型思考 token 吃掉输出时的真实姿态）。"
        "空摘要比长摘要危害大：append 空摘要等于把被压段从视图里抹掉，"
        "所以按失败处理——context_compact_failed 留痕、不 append、不挡 turn，"
        "重发请求下一边界重试成功。",
    )
    try:
        llm = RealLLM()
    except RuntimeError as exc:
        line("系统", ORANGE, f"跳过真模型段：{exc}")
        return
    h = Harness(workdir, llm=llm, summarizer=EmptyOnceSummarizer(LiveSummarizer(llm)))
    for text in ("保温杯还有库存吗", "好的"):
        line("用户", YELLOW, text)
        h.send(text)
        await h.wait_turn()

    before = h.projection_brief()
    line("压缩前投影", BLUE, before)

    line("用户", YELLOW, "继续（第一次压缩：摘要器返回空）")
    h.compact()
    h.send("继续")
    await h.wait_turn()
    failed = h.compact_events("context_compact_failed")
    line(
        "失败留痕",
        RED,
        f"context_compact_failed × {len(failed)}；错误：{brief(failed[0].payload.get('error', ''), 60)}"
        if failed
        else "（这次真模型自己写出了非空摘要，未触发注入——重跑可复现）",
    )
    line("失败后", BLUE, f"compaction entry：{'无（不 append）' if h.compaction_entry() is None else '有'}；投影 {h.projection_brief()}")

    line("用户", YELLOW, "继续（重发：下一边界重试）")
    h.compact()
    h.send("继续")
    await h.wait_turn()
    await h.stop()

    entry = h.compaction_entry()
    line(
        "重试成功",
        GREEN,
        f"entry {entry.id}；context_compacted × {len(h.compact_events('context_compacted'))}"
        if entry is not None
        else "仍未成功（真模型波动，重跑）",
    )
    note("失败与成功都留痕在 EventLog；turn 都没有被挡住——压缩救不了自己，但也不拖垮会话。")


# ---------------------------------------------------------------- demo 3：副作用不重做


async def case_side_effect_not_repeated(workdir: Path) -> None:
    banner(
        "03-side-effect-not-repeated",
        "副作用不重做：写被压掉之后，模型不再重写",
        "改库存（免审批量）→ 两轮把它推进被压段 → 压缩 → 问汇报。"
        "摘要按'副作用'分段保住写操作，模型凭摘要知道'做过就是做过'——"
        "全程恰好一次真实 update_inventory。",
    )
    try:
        h = Harness(workdir)
    except RuntimeError as exc:
        line("系统", ORANGE, f"跳过真模型段：{exc}")
        return
    for text in ("把保温杯的库存改成 10 件", "玻璃杯还有多少", "好的，先这样"):
        line("用户", YELLOW, text)
        h.send(text)
        await h.wait_turn()

    h.compact()
    line("用户", YELLOW, "汇报一下今天改了哪些库存。保温杯还需要再改吗？")
    h.send("汇报一下今天改了哪些库存。保温杯还需要再改吗？")
    await h.wait_turn()
    await h.stop()

    entry = h.compaction_entry()
    assert entry is not None, "压缩应已发生（写操作落在被压段）"
    writes = [
        e
        for e in h.events
        if e.type == "tool_result"
        and e.payload.get("name") == "update_inventory"
        and not e.payload.get("skipped")
        and not e.payload.get("blocked")
    ]
    line("写操作计数", GREEN, f"真实 update_inventory × {len(writes)}（被压掉的那次；没有第二次）")
    inv = (workdir / "inventory.txt").read_text(encoding="utf-8")
    therma = [ln for ln in inv.splitlines() if ln.startswith("保温杯")]
    line("落库值", GREEN, therma[0] if therma else "（未找到保温杯行）")
    line("摘要副作用段", BLUE, brief(entry.payload["summary"], 160))
    reply = [
        str(e.payload.get("message", {}).get("content") or "")
        for e in h.events
        if e.type == "agent_reply" and not e.payload.get("synthetic")
    ]
    texts = [r for r in reply if r.strip()]  # 最后一步可能是纯 tool_calls（无文本）
    line("模型汇报", BLUE, brief(texts[-1], 160) if texts else "（无文本回复）")
    note("写操作原文在轨迹和 blob 里随时可查；摘要只负责'别重复做'，不负责事实查询。")


# ---------------------------------------------------------------- demo 4：触发策略对照


async def case_watermark_trigger(workdir: Path) -> None:
    """同样跑 4 轮：大结果会话 token 越水位自动压缩，闲聊会话全程不压。

    两个会话都用同一套组合策略（ratio 水位 + step 刀口）：触发看 token
    （计量 = 真实 usage 锚点 + 轨迹增量估算），下刀看 step（保留窗 tool 配对
    完整）。纯按步数会把闲聊也误压；纯按 token 会切在消息中间。
    """
    try:
        llm = RealLLM()
    except RuntimeError as exc:
        line("系统", ORANGE, f"跳过真模型段：{exc}")
        return
    # 真实 usage 为锚（含 system prompt 的基线 ~800 token）：阈值要落在基线
    # 之上、数轮工具往返能到的地方——纯闲聊到不了，工具会话几轮就越线
    policy = CompactionPolicy(mode="ratio", window_tokens=2200, watermark=0.7, keep_steps=2)
    trigger = trigger_tokens(policy)

    # —— 会话 A：多品类查询（每轮并行多个工具往返，token 涨得快）——
    print(f"\n{BOLD}会话 A：工具往返的查询会话（触发线 {trigger} token）{RESET}")
    hA = Harness(workdir / "big", llm=llm, policy=policy)
    compacted_at = None
    for i, text in enumerate(
        (
            "保温杯和玻璃杯的库存都查一下",
            "马克杯和雨伞呢",
            "帆布包和围巾呢",
            "保温壶和不锈钢碗呢",
            "再把陶瓷餐具查一下",
        ),
        1,
    ):
        line("用户", YELLOW, text)
        hA.send(text)
        await hA.wait_turn()
        est = hA.agent._meter(hA.sid).estimate(hA.traj)
        line("计量", BLUE, f"第 {i} 轮后估算 {est} token（触发线 {trigger}）")
        if hA.compact_events("context_compacted"):
            compacted_at = i
            evt = hA.compact_events("context_compacted")[-1]
            line(
                "自动压缩",
                GREEN,
                f"水位越线，reason={evt.payload['reason']}，"
                f"消息 {evt.payload['messages_before']} → {evt.payload['messages_after']} 条",
            )
            break
    await hA.stop()
    if compacted_at is None:
        line("实测", RED, "未触发（真模型回答偏短，可重跑或调低 window_tokens）")

    # —— 会话 B：闲聊（同样轮数，token 低）——
    print(f"\n{BOLD}会话 B：闲聊会话（同样的轮数）{RESET}")
    hB = Harness(workdir / "chat", llm=llm, policy=policy)
    for text in ("你好呀", "今天店里忙吗", "好的谢谢你"):
        line("用户", YELLOW, text)
        hB.send(text)
        await hB.wait_turn()
        est = hB.agent._meter(hB.sid).estimate(hB.traj)
        line("计量", BLUE, f"估算 {est} token（触发线 {trigger}）")
    await hB.stop()
    compacted_b = hB.compact_events("context_compacted")
    b_est = hB.agent._meter(hB.sid).estimate(hB.traj)
    line(
        "实测",
        GREEN if not compacted_b else RED,
        f"闲聊会话全程未压缩（{'正确' if not compacted_b else '误压了'}，"
        f"最终估算 {b_est} < 触发线 {trigger}）："
        "token 计量分辨了大小会话，不会像纯按步数那样误压闲聊",
    )
    note(
        "触发与下刀是两步：token 越水位才触发；触发后刀口落在倒数第 N 个 step "
        "起点，保留窗 tool 配对完整。两步组合替代了纯步数（误压闲聊）与 "
        "纯 token（截断消息）两种单一策略。"
    )


# ---------------------------------------------------------------- demo 5：二次折叠


async def case_fold_twice(workdir: Path) -> None:
    """两刀折叠：摘要 B 的输入 = 摘要 A + 增量 step（previous 传递），
    投影认最后一切——视图里只剩摘要 B，摘要 A 被吞（原文仍在轨迹）。"""
    try:
        llm = RealLLM()
    except RuntimeError as exc:
        line("系统", ORANGE, f"跳过真模型段：{exc}")
        return
    recorder = RecordingSummarizer(LiveSummarizer(llm))
    h = Harness(workdir, llm=llm, summarizer=recorder, policy=CompactionPolicy(keep_steps=1))
    for text in ("保温杯还有库存吗", "玻璃杯呢"):
        line("用户", YELLOW, text)
        h.send(text)
        await h.wait_turn()

    line("用户", YELLOW, "（第一刀）上下文有点长了，压一下")
    h.compact("manual-1")
    h.send("继续")
    await h.wait_turn()
    comps = [e for e in h.traj.entries() if e.type == COMPACTION]
    if not comps:
        line("实测", RED, "第一刀未成功（真模型波动，重跑）")
        await h.stop()
        return
    summary_a = str(comps[0].payload["summary"])
    line("第一刀", GREEN, f"compaction {comps[0].id}，摘要开头：{brief(summary_a, 90)}")

    for text in ("马克杯还有吗", "雨伞呢"):
        line("用户", YELLOW, text)
        h.send(text)
        await h.wait_turn()

    line("用户", YELLOW, "（第二刀）再压一次")
    h.compact("manual-2")
    h.send("继续")
    await h.wait_turn()
    await h.stop()

    comps = [e for e in h.traj.entries() if e.type == COMPACTION]
    if len(comps) < 2:
        line("实测", RED, f"第二刀未成功（共 {len(comps)} 刀，真模型波动，重跑）")
        return
    summary_b = str(comps[1].payload["summary"])
    line("第二刀", GREEN, f"compaction {comps[1].id}，摘要开头：{brief(summary_b, 90)}")
    folded = recorder.previous[-1] == summary_a if recorder.previous else False
    line(
        "折叠",
        GREEN if folded else RED,
        f"摘要 B 的输入带着上一刀摘要（previous=摘要A）：{folded}——"
        "旧摘要被吞，原始全文一次都不重读",
    )
    proj = build_context(h.traj)
    heads = [str(m.get("content", ""))[:30] for m in proj.messages[:2]]
    only_b = summary_a not in str([m.get("content") for m in proj.messages])
    line(
        "投影",
        BLUE if only_b else RED,
        f"视图头部：{heads}；视图里只剩摘要 B：{only_b}（认最后一切）",
    )
    note(
        f"全树 {len(h.traj.entries())} 条 entry、两刀 compaction 都在轨迹里："
        "branch 回第一刀还能回到摘要 A 的视图；投影只认当前路径上最后一刀。"
    )


CASE_ORDER = (
    "01-compact-before-after",
    "02-compact-fail-open",
    "03-side-effect-not-repeated",
    "04-watermark-trigger",
    "05-fold-twice",
)
CASE_TITLES = {
    "01-compact-before-after": "压缩前后对照：entry + 摘要全文 + 投影换视图",
    "02-compact-fail-open": "摘要失败 fail-open：留痕、不 append、重试成功",
    "03-side-effect-not-repeated": "副作用不重做：写被压掉之后恰好一次真实写",
    "04-watermark-trigger": "触发策略对照：token 计量决定压不压，闲聊不误压",
    "05-fold-twice": "二次折叠：新摘要吞旧摘要，投影认最后一切",
}
PENDING_NOTE = (
    "“其他影响上下文的一些策略”一章的配套段落（分页与批次句柄、cap + blob）"
    "不在本书 demo 范围内，不实现。"
)

CASES = {
    "01-compact-before-after": case_compact_before_after,
    "02-compact-fail-open": case_compact_fail_open,
    "03-side-effect-not-repeated": case_side_effect_not_repeated,
    "04-watermark-trigger": case_watermark_trigger,
    "05-fold-twice": case_fold_twice,
}


async def main(case_ids: list[str] | None = None, sessions_dir: Path | None = None) -> None:
    root = sessions_dir or (Path(__file__).resolve().parents[2] / "sessions" / "stage05")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    picked = [c for c in CASE_ORDER if not case_ids or "all" in case_ids or c in case_ids]
    if not picked:
        print(f"{GREY}没有匹配的 case：{case_ids}；可选：all, {', '.join(CASE_ORDER)}{RESET}")
        return

    print(f"{BOLD}Stage 05 压缩策略演示 —— 与 05 章 demo 对应（真模型）{RESET}")
    if case_ids and "all" not in case_ids:
        print(f"{GREY}  （只跑：{', '.join(picked)}）{RESET}")

    for i, cid in enumerate(picked):
        workdir = root / cid.split("-", 1)[1]
        workdir.mkdir(parents=True, exist_ok=True)
        await CASES[cid](workdir)
        if i < len(picked) - 1:
            print()

    print(f"\n{BOLD}── 收尾 ──{RESET}")
    line("系统", ORANGE, "demo 结束")
    print(f"{GREY}  session log: {root}{RESET}")
    note(PENDING_NOTE)


def cli() -> None:
    """[project.scripts] 入口：stage05-demo。

        stage05-demo                       # 五段全跑（全部真模型）
        stage05-demo 02-compact-fail-open  # 只跑指定段
        stage05-demo --list                # 列 case 及其说明
    """
    parser = argparse.ArgumentParser(
        prog="stage05-demo", description="Stage 05 压缩策略演示（与 05 章 demo 对应）。"
    )
    parser.add_argument("cases", nargs="*", metavar="CASE", help="要跑的 case（默认全部）")
    parser.add_argument("--list", action="store_true", help="列出所有 case 后退出")
    parser.add_argument("--sessions-dir", default=None, help="轨迹落点")
    args = parser.parse_args()
    if args.list:
        print("all\t五段全跑（全部真模型）")
        for cid in CASE_ORDER:
            print(f"{cid}\t{CASE_TITLES[cid]}")
        return
    asyncio.run(
        main(args.cases or None, Path(args.sessions_dir) if args.sessions_dir else None)
    )


if __name__ == "__main__":
    cli()
