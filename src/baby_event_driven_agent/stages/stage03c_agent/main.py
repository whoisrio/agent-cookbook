"""Stage 03c 能力升级演示——工具层、判量审批与长程任务（03c 配套）。

与 04 同包时代码共用（agent / tools / transport / session 实现在
stage04_trajectory 包里），演示入口独立成本包，互不掺和：
stage04-demo 是轨迹六段，本包是 03c 的三段。

需要仓库根 .env 里的 OPENAI_API_KEY / OPENAI_API_BASE / OPENAI_MODEL
（环境变量可覆盖）。01 不打模型；02 用 ScriptedLLM 离线可断言（03c 章
文档明确的设计：19 步长任务剧本，确定性、可反复跑）；03 打真模型。

三段（各自独立，跑哪个都行）：

1. **工具层直调**（离线，不打模型）：任务域两个工具的返回形状；判量审批
   的边界（补 50 放行、补 51 问人、负差放行）；写操作设值语义，重放不叠加；
   数据隔离——写落在工作目录副本，包自带 data/ 与共享 knowledge-base
   一个字节不动。
2. **长程任务**（离线）：五轮 19 步跑完整张任务单（补货核查 + 盘点差异，
   ScriptedLLM 确定性），审批判量在真实流程里触发（马克杯报备被拒）；
   上下文只增不减——04 要接手的现场。
3. **真模型长任务**：缩减版任务端到端，真工具、真审批（应答通道扮演
   店长拒绝马克杯报备）。不断言行为序——模型自己决定先查什么后查什么。

行首标签沿用前几章：

    用户 │ 用户说了什么
    工具 │ 工具调用与真实结果（绿色）；被治理拦下用亮红
    系统 │ 生命周期与统计（统一橙色）
    说明 │ 旁白，只解释这一段在演示什么（灰色缩进）

    python -m baby_event_driven_agent.stages.stage03c_agent
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

from ..stage04_trajectory import tools as tools_mod
from ..stage04_trajectory.agent import Agent
from ..stage04_trajectory.llm import RealLLM
from ..stage04_trajectory.transport.bus import EventBus
from ..stage04_trajectory.transport.events import Event, Subscription, OBSERVE
from ..stage04_trajectory.transport.persistence import EventLog
from ..stage04_trajectory.session.store import SessionStore

# ---------------------------------------------------------------- 屏幕上色
DIM = "\033[2m"
GREY = "\033[90m"
BLUE = "\033[94m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[91m"
ORANGE = "\033[38;5;208m"  # 系统提示统一橙色
BOLD = "\033[1m"
RESET = "\033[0m"

TIMEOUT = 20.0

CASE_ORDER = (
    "01-tools",
    "02-long-task",
    "03-live-task",
)
CASE_TITLES = {
    "01-tools": "第 1 段：工具层直调——任务域与判量审批（离线，不打模型）",
    "02-long-task": "第 2 段：长程任务——补货核查与盘点差异（离线，ScriptedLLM 可断言）",
    "03-live-task": "第 3 段：真模型跑缩减版长任务",
}
ALL_TITLE = "三段全跑（离线 1-2 + 真模型 3）"
CASE_IDS = ("all", *CASE_ORDER)


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


# ---------------------------------------------------------------- 脚手架


class ScriptedLLM:
    """离线脚本化 LLM：按调用次序吐脚本块——第 2 段可断言、可反复跑。

    这是 03c 章文档明确请回来的脚手架（长任务剧本要确定性）；
    轨迹六段的真模型演示在 stage04-demo，不用它。
    """

    def __init__(self, script: list[list[dict[str, Any]]]) -> None:
        self.script = script
        self.calls = 0
        self.contexts: list[list[dict[str, Any]]] = []

    async def stream_chat(self, messages: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
        self.contexts.append([dict(m) for m in messages])
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for chunk in self.script[idx]:
            yield chunk


def use_data_copies(workdir: Path) -> dict[str, Path]:
    """把包自带 data/ 拷进工作目录并换掉工具的数据源（写操作落副本）。

    返回原路径快照，用完 restore_data(saved) 还原——demo / 测试共用这一对。
    """
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


class Harness:
    """demo 台子：bus + EventLog + store + agent，外加一个 turn_end 信号。

    传 script 就是离线段（ScriptedLLM 驱动，确定性，不打模型）；
    live=True 打真模型（RealLLM）。
    """

    def __init__(
        self,
        workdir: Path,
        script: list[list[dict[str, Any]]] | None = None,
        *,
        live: bool = False,
    ) -> None:
        self.log = EventLog(str(workdir / "events"))
        self.bus = EventBus(self.log)
        self.store = SessionStore(workdir / "sessions")
        self.system_prompt = "你是一个通过工具干活的通用 agent。"
        llm = RealLLM() if live else ScriptedLLM(script or [])
        self.llm = llm
        self.timeout = 180.0 if live else TIMEOUT  # 真模型段放宽等待
        self.agent = Agent(self.bus, llm, store=self.store, system_prompt=self.system_prompt)
        self.traj = self.store.start(
            cwd=str(workdir), model=str(getattr(llm, "model", "")), system_prompt=self.system_prompt
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

    async def wait_turn(self) -> None:
        await asyncio.wait_for(self.ended.wait(), self.timeout)
        self.ended.clear()
        await self.bus.drain(timeout=self.timeout)

    async def stop(self) -> None:
        await self.agent.stop()
        await self.bus.drain(timeout=self.timeout)
        await self.bus.close()


# ---------------------------------------------------------------- 第 1 段：工具层直调


async def case_tools(workdir: Path) -> None:
    """工具层直调（不打模型）：任务域、判量审批边界、数据隔离、设值语义。"""
    banner(
        "01-tools",
        "工具层直调：任务域与判量审批（离线，不打模型）",
        "能力升级的交付物直接看：任务域两个工具的返回形状；判量审批的边界"
        "（补 50 放行、补 51 问人、负差放行）；写操作设值语义，重放不叠加；"
        "数据隔离——写落在工作目录副本，包自带 data/ 与共享 knowledge-base "
        "一个字节不动。",
    )
    attrs = ("_INVENTORY", "_RULES", "_TASKS")
    saved = {a: getattr(tools_mod, a) for a in attrs}
    kb_dir = saved["_INVENTORY"].parents[2] / "knowledge-base"
    kb_before = {p.name: p.read_bytes() for p in kb_dir.glob("*.txt")}
    data_before = {a: p.read_bytes() for a, p in saved.items()}
    try:
        use_data_copies(workdir)

        line("实测", GREEN, f"list_tasks →\n{await tools_mod.list_tasks({})}")
        line("实测", GREEN, f"get_task(T-101) →\n{await tools_mod.get_task({'task_id': 'T-101'})}")
        line(
            "实测",
            GREEN,
            f"get_task(T-404) → {brief(await tools_mod.get_task({'task_id': 'T-404'}))}",
        )

        over = tools_mod._restock_approval({"category": "马克杯", "stock": 60})
        edge = tools_mod._restock_approval({"category": "保温杯", "stock": 53})
        under = tools_mod._restock_approval({"category": "保温杯", "stock": 50})
        shrink = tools_mod._restock_approval({"category": "雨伞", "stock": 13})
        line("实测", GREEN, f"判量：马克杯 3→60（补 52 件）→ {over}")
        line("实测", GREEN, f"判量：保温杯 3→53（补 50 件，贴线）→ {edge or '放行'}")
        line("实测", GREEN, f"判量：保温杯 3→50（补 47 件）→ {under or '放行'}")
        line("实测", GREEN, f"判量：雨伞 15→13（负差，盘点调整）→ {shrink or '放行'}")

        await tools_mod.update_inventory({"category": "保温杯", "stock": 50})
        await tools_mod.update_inventory({"category": "保温杯", "stock": 50})
        again = await tools_mod.query_inventory({"category": "保温杯"})
        line("实测", GREEN, f"设值语义重放：补到 50 两次，仍是一行 → {brief(again)}")

        kb_after = {p.name: p.read_bytes() for p in kb_dir.glob("*.txt")}
        data_after = {a: p.read_bytes() for a, p in saved.items()}
        line(
            "实测",
            GREEN,
            f"数据隔离：包 data/ 未变：{data_after == data_before}；"
            f"共享 knowledge-base 未变：{kb_after == kb_before}",
        )
        note(
            "判量的线和 rules.txt 里的补货规则是同一条：业务规则告诉模型"
            "“超 50 要报备”，工具声明告诉 harness“超 50 要问人”。"
            "任务单是只读输入：没有状态字段，“做到哪了”住在会话里——"
            "这正是 04 压缩要保的东西。"
        )
    finally:
        restore_data(saved)


# ---------------------------------------------------------------- 第 2 段：长程任务


def _tool_step(cid: str, name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_call_delta",
            "index": 0,
            "id": cid,
            "name": name,
            "args_delta": json.dumps(args, ensure_ascii=False),
        }
    ]


def _text_step(text: str) -> list[dict[str, Any]]:
    return [{"type": "text_delta", "text": text}]


# 长程任务脚本（五轮 19 步，离线确定性）：T1 领任务+先干起来 → T2 纠偏先查规则
# → T3 规则路线（马克杯报备被拒）→ T4 换单（雨伞按实物调）→ T5 收尾（帆布包
# 不动、围巾挂起）。
LONG_TASK_SCRIPT = [
    _tool_step("c01", "list_tasks", {}),
    _tool_step("c02", "get_task", {"task_id": "T-101"}),
    _tool_step("c03", "query_inventory", {"category": "保温杯"}),
    _tool_step("c04", "update_inventory", {"category": "保温杯", "stock": 50}),
    _tool_step("c05", "search_rules", {"query": "补货"}),
    _tool_step("c06", "query_inventory", {"category": "玻璃杯"}),
    _tool_step("c07", "update_inventory", {"category": "玻璃杯", "stock": 20}),
    _tool_step("c08", "query_inventory", {"category": "马克杯"}),
    _tool_step("c09", "update_inventory", {"category": "马克杯", "stock": 60}),
    _tool_step("c10", "query_inventory", {"category": "保温壶"}),
    _tool_step("c11", "update_inventory", {"category": "保温壶", "stock": 20}),
    _text_step(
        "补货核查处理完：保温杯、玻璃杯、保温壶已按目标补足；"
        "马克杯需补 52 件超过 50 件上限，报备未获批准，未补。"
    ),
    _tool_step("c12", "get_task", {"task_id": "T-102"}),
    _tool_step("c13", "query_inventory", {"category": "雨伞"}),
    _tool_step("c14", "update_inventory", {"category": "雨伞", "stock": 13}),
    _text_step("雨伞系统 15 件、实物 13 件，差 2 件在 3 件以内，已按实物调整库存。"),
    _tool_step("c15", "query_inventory", {"category": "帆布包"}),
    _tool_step("c16", "query_inventory", {"category": "围巾"}),
    _text_step(
        "帆布包账实相符，不用动；围巾系统 8 件、实物 2 件，差 6 件超过 3 件，"
        "按规则挂起等人工复盘。今天的任务处理完毕。"
    ),
]

LONG_TASK_TURNS = [
    "今天仓库的补货核查和盘点差异，你处理一下。",
    "等等——先查补货规则，按规矩来。",
    "继续",
    "继续，处理盘点差异那张单。",
    "把剩下的处理完。",
]


async def case_long_task(workdir: Path) -> None:
    """长程任务端到端（离线）：工具链叠出长会话、审批判量触发、上下文只增不减。"""
    banner(
        "02-long-task",
        "长程任务：补货核查与盘点差异（离线）",
        "五轮 19 步跑完整张任务单（ScriptedLLM，确定性）。看三样：工具链怎么"
        "一轮轮叠出长会话；审批判量在真实流程里怎么触发（马克杯报备被拒）；"
        "以及无轨迹时代的痛——上下文只增不减。",
    )
    saved = use_data_copies(workdir)
    try:
        h = Harness(workdir, LONG_TASK_SCRIPT)
        approvals: list[Event] = []

        async def on_required(event: Event) -> None:
            approvals.append(event)
            p = event.payload
            line(
                "系统",
                ORANGE,
                f"？ {p['name']} 要执行：{brief(p['arguments'])}"
                f"（request_id={p['request_id']}，超时 {p['timeout']:g}s）",
            )
            await asyncio.sleep(0.05)  # 店长看了一眼
            line("用户", YELLOW, "→ 拒绝（数量过大，本次不补）")
            h.bus.publish(
                Event(
                    "user_approval",
                    h.sid,
                    {
                        "request_id": p["request_id"],
                        "approve": False,
                        "reason": "数量过大，本次不补",
                    },
                ),
                to=h.agent.agent_id,
            )

        h.bus.subscribe(Subscription("d.required", ("approval_required",), on_required))
        for text in LONG_TASK_TURNS:
            line("用户", YELLOW, text)
            h.send(text)
            await h.wait_turn()

        counts = [len(ctx) for ctx in h.llm.contexts]
        monotonic = all(b >= a for a, b in zip(counts, counts[1:]))
        line(
            "实测",
            GREEN,
            f"{len(LONG_TASK_TURNS)} 轮 {h.llm.calls} 步跑完；每步的上下文消息数 "
            f"{counts[0]}→{counts[-1]}，单调只增不减：{monotonic}",
        )
        decided = [e for e in h.events if e.type == "approval_decided"]
        line(
            "实测",
            GREEN,
            f"审批恰好一次：approval_required ×{len(approvals)}，decided ×{len(decided)}"
            f"（action={decided[0].payload.get('action') if decided else '-'}）",
        )
        line("系统", ORANGE, "任务单跑完后的库存副本：")
        for ln in tools_mod._INVENTORY.read_text(encoding="utf-8").splitlines():
            line("  ", GREY, ln)
        await h.stop()
        note(
            "这就是 04 要接手的现场：纠偏只能往前追加，试错的几轮永远留在上下文里；"
            "消息数只增不减压不下；进程一换全丢。同一张任务单的另一种命运，"
            "轨迹解法见 04 的轨迹演示。"
        )
    finally:
        restore_data(saved)


# ---------------------------------------------------------------- 第 3 段：真模型长任务


async def case_live_task(workdir: Path) -> None:
    """真模型跑缩减版长任务：T-101 整单，审批由应答通道扮演店长拒绝。"""
    banner(
        "03-live-task",
        "真模型跑缩减版长任务",
        "真模型 + 真工具，只跑补货核查那张单（T-101）。马克杯的报备由应答通道"
        "扮演店长拒绝。不断言行为序——模型自己决定先查什么后查什么，"
        "只看任务真的能跑完、审批真的进账。",
    )
    try:
        RealLLM()
    except RuntimeError as exc:
        line("系统", ORANGE, f"跳过真模型段：{exc}")
        return

    saved = use_data_copies(workdir)
    try:
        h = Harness(workdir, live=True)

        async def on_required(event: Event) -> None:
            p = event.payload
            line(
                "系统",
                ORANGE,
                f"？ {p['name']} 要执行：{brief(p['arguments'])}"
                f"（request_id={p['request_id']}，超时 {p['timeout']:g}s）",
            )
            line("用户", YELLOW, "→ 拒绝（数量过大，本次不补）")
            h.bus.publish(
                Event(
                    "user_approval",
                    h.sid,
                    {
                        "request_id": p["request_id"],
                        "approve": False,
                        "reason": "数量过大，本次不补",
                    },
                ),
                to=h.agent.agent_id,
            )

        async def ui_tool(event: Event) -> None:
            line("工具", GREEN, f"← {event.payload['name']} 结果：{brief(event.payload['result'])}")

        h.bus.subscribe(Subscription("d.required", ("approval_required",), on_required))
        h.bus.subscribe(Subscription("d.tool", ("tool_result",), ui_tool, mode=OBSERVE))

        async def on_decided(event: Event) -> None:
            p = event.payload
            line("系统", ORANGE, f"确认结果：{p['action']} by {p['by']}")

        h.bus.subscribe(Subscription("d.decided", ("approval_decided",), on_decided))
        line("用户", YELLOW, "处理一下补货核查任务单（T-101）")
        h.send("处理一下补货核查任务单（T-101）")
        for _ in range(4):  # 每轮最多 4 步，续几句“继续”让单子跑完
            await asyncio.wait_for(h.ended.wait(), 180.0)
            h.ended.clear()
            await h.bus.drain(timeout=30.0)
            line("用户", YELLOW, "继续")
            h.send("继续")
        await h.stop()
        line("系统", ORANGE, "库存副本终态：")
        for ln in tools_mod._INVENTORY.read_text(encoding="utf-8").splitlines():
            line("  ", GREY, ln)
        note(
            "真模型的任务单实测：能不能按单逐项、报备被拒不重试，"
            "取决于模型本身；审批的账目是断言的基准，行为序不是。"
        )
    finally:
        restore_data(saved)


CASES = {
    "01-tools": case_tools,
    "02-long-task": case_long_task,
    "03-live-task": case_live_task,
}


async def main(case_ids: list[str] | None = None, sessions_dir: Path | None = None) -> None:
    root = sessions_dir or (Path(__file__).resolve().parents[2] / "sessions" / "stage03c")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    picked = [c for c in CASE_ORDER if not case_ids or "all" in case_ids or c in case_ids]
    if not picked:
        print(f"{GREY}没有匹配的 case：{case_ids}；可选：{', '.join(CASE_IDS)}{RESET}")
        return

    print(f"{BOLD}Stage 03c 能力升级演示 —— 工具层、判量审批与长程任务{RESET}")
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


def cli() -> None:
    """[project.scripts] 入口：stage03c-demo。

        stage03c-demo                      # 三段全跑（默认；第 3 段打真模型）
        stage03c-demo 02-long-task         # 只跑指定段（离线段可当基准反复跑）
        stage03c-demo --list               # 列 case 及其说明（不加载模型配置）
    """
    parser = argparse.ArgumentParser(
        prog="stage03c-demo", description="Stage 03c 能力升级演示（工具层、判量审批与长程任务）。"
    )
    parser.add_argument("cases", nargs="*", metavar="CASE", help="要跑的 case（默认全部）")
    parser.add_argument("--list", action="store_true", help="列出所有 case 后退出")
    parser.add_argument("--sessions-dir", default=None, help="轨迹落点")
    args = parser.parse_args()
    if args.list:
        print(f"all\t{ALL_TITLE}")
        for cid in CASE_ORDER:
            print(f"{cid}\t{CASE_TITLES[cid]}")
        return
    asyncio.run(
        main(args.cases or None, Path(args.sessions_dir) if args.sessions_dir else None)
    )


if __name__ == "__main__":
    cli()
