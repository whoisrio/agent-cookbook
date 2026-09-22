"""会话身份与生命周期：sid 从这里出，start / resume / close 三个入口。

Stage 4 结束时的困境（04 章末尾的预告）：事件留下来了、也能重放了，但
"会话"本身还不存在——进程一重启，同一个 session_id 会静默变成一段新历史，
而磁盘上躺着上一段。本章的回答：

- **sid 由 store 分配，不是调用方随口给**。谁分配谁负责"这个 id 指哪段历史"。
- **start / resume / close 的判据是"store 里有没有这个 sid"**，不猜。
- **生命周期事件进轨迹**（session_started / session_resumed / session_end）——
  否则一个文件里两段进程的历史首尾相接，回放时看不出中间断过（和 Stage 2
  "排队的消息连 log 里都没痕迹"是同一类坑）。
- **同 session 单写者**：一个 sid 同时只该有一个 Trajectory 在写（一个 agent）。

resume 的两条恢复路径（都以"轨迹是唯一真相"为基准）：

1. 字节级：撞残尾就停在最后一条完好 entry（CRC 判定，TrajectoryLog 负责），
   resumed 事件里带 torn_tail 标记留痕；
2. 语义级：崩在半路的轨迹尾部可能是"悬挂的工具调用"（assistant 要了结果没等到）
   ——修复发生在投影层（trajectory._sanitize），原始 entry 原封不动。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from ..transport.bus import EventBus
from ..transport.events import Event
from .trajectory import MODEL_CHANGE, SESSION_END, SESSION_RESUMED, SESSION_STARTED, Trajectory, TrajectoryLog


class SessionStore:
    """一个目录管一批会话：<root>/<sid>.jsonl。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 三个入口

    def start(
        self, *, cwd: str = "", model: str | None = None, note: str = "", system_prompt: str = ""
    ) -> Trajectory:
        """开一个新会话：分配 sid，落 header（含 system_prompt 原文，审计用）
        + session_started（+ 初始 model_change）。"""
        sid = uuid.uuid4().hex
        traj = Trajectory.create(
            TrajectoryLog(self.root / f"{sid}.jsonl"), sid=sid, cwd=cwd, note=note, system_prompt=system_prompt
        )
        traj.append(SESSION_STARTED, {"by": "store"})
        if model:
            # 树根的状态节点：会话还没说第一句话，轨迹已经记下用哪个模型
            traj.append(MODEL_CHANGE, {"model_id": model, "by": "store"})
        return traj

    def resume(self, sid: str, *, note: str = "") -> Trajectory:
        """恢复会话：读文件重建树，撞残尾停在最后一条完好 entry，然后留痕继续。"""
        path = self.root / f"{sid}.jsonl"
        if not path.exists():
            raise KeyError(f"store 里没有这个 session：{sid!r}（{path}）")
        traj = Trajectory.load(TrajectoryLog(path))
        traj.append(SESSION_RESUMED, {"torn_tail": traj.torn, "note": note})
        return traj

    def close(self, traj: Trajectory, *, reason: str = "") -> None:
        traj.append(SESSION_END, {"reason": reason})

    def fork(self, traj: Trajectory, *, note: str = "") -> Trajectory:
        """从当前路径分叉出一份新会话文件（session 切换）。旧会话原封不动。"""
        sid = uuid.uuid4().hex
        return traj.fork(
            TrajectoryLog(self.root / f"{sid}.jsonl"), sid=sid, cwd=str(traj.header.get("cwd", "")), note=note
        )

    # ------------------------------------------------------------ 观测

    def list_sessions(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.jsonl"))

    def path_of(self, sid: str) -> Path:
        return self.root / f"{sid}.jsonl"


# ---------------------------------------------------------------- 悬挂审批闭合


def sweep_hanging_approvals(bus: EventBus, sid: str, *, by: str = "session_resume") -> list[str]:
    """把 EventLog 里"只有请求没有回执"的确认按未授权闭合（补 abandoned 裁决）。

    "一问必有一答"的唯一例外是进程被硬杀——那时和落盘的残尾一个道理，没写完的
    不算已发生。但回放的人需要知道"看到孤立的请求 = 那次没走完"：resume 时扫一遍，
    每个悬空的 approval_required 补一条 approval_decided（action=abandoned），
    走 bus.record 留痕。已经闭合的（有配对回执）一个不碰。
    """
    if bus.log is None:
        return []
    records = bus.log.read_since(0)
    asked: dict[str, bool] = {}  # request_id -> 是否已闭合
    for r in records:
        if r.get("session") != sid:
            continue
        p = r.get("payload", {})
        rid = str(p.get("request_id", ""))
        if r["type"] == "approval_required" and rid:
            asked.setdefault(rid, False)
        elif r["type"] == "approval_decided" and rid:
            asked[rid] = True
    closed: list[str] = []
    for rid, done in asked.items():
        if done:
            continue
        bus.record(
            Event(
                "approval_decided",
                sid,
                {
                    "request_id": rid,
                    "action": "abandoned",
                    "by": by,
                    "reason": "进程重启，等待没有走完：按未授权闭合",
                },
            )
        )
        closed.append(rid)
    return closed


def session_facts(traj: Trajectory) -> list[dict[str, Any]]:
    """轨迹里的生命周期事实（demo / 测试用）：type + 时间 + 关键 payload。"""
    return [
        {"type": e.type, "ts": e.ts, "payload": e.payload}
        for e in traj.entries()
        if e.type in (SESSION_STARTED, SESSION_RESUMED, SESSION_END, MODEL_CHANGE)
    ]
