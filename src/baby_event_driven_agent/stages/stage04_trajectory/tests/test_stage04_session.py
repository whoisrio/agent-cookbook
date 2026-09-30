"""Stage 5a 会话层用例：全部离线。

覆盖：
- sid 由 store 分配；start 落 header（+ 初始 model_change）；生命周期不落账
- resume 重建树、从 leaf 继续（不重复事实）；正常 resume 字节不变；残尾补占位
- fork 的血缘记在 header 的 parent_session 里
- 悬挂审批闭合：孤立的 approval_required 补 abandoned 裁决，闭合过的不碰（幂等）
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from baby_event_driven_agent.stages.stage04_trajectory.transport.bus import EventBus
from baby_event_driven_agent.stages.stage04_trajectory.transport.events import Event
from baby_event_driven_agent.stages.stage04_trajectory.transport.persistence import EventLog
from baby_event_driven_agent.stages.stage04_trajectory.session.store import (
    SessionStore,
    sweep_hanging_approvals,
)
from baby_event_driven_agent.stages.stage04_trajectory.session.trajectory import (
    MESSAGE,
    Trajectory,
    message_payload,
)


@pytest.fixture()
def workdir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stage04_trajectory_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_sid_is_allocated_by_store(workdir: Path) -> None:
    """sid 由 store 分配（不是调用方随口给），文件名就是 sid。"""
    store = SessionStore(workdir / "sessions")
    traj = store.start(cwd="/tmp", model="fake-model")
    assert traj.sid and len(traj.sid) == 32
    assert store.path_of(traj.sid).exists()
    types = [e.type for e in traj.entries()]
    assert types == ["model_change"]  # 生命周期不落账：header 即开始
    assert traj.entries()[0].payload["model_id"] == "fake-model"


def test_start_without_model_has_no_model_change(workdir: Path) -> None:
    store = SessionStore(workdir / "sessions")
    traj = store.start()
    assert traj.entries() == []  # 没给 model：header 之外一条 entry 都没有


def test_header_records_system_prompt_verbatim(workdir: Path) -> None:
    """header 记 system prompt 原文（审计）：prompt 是参数不进消息树，
    但盘上要查得到"这个会话当时用的是哪个 prompt"，重放才核对得了。"""
    store = SessionStore(workdir / "sessions")
    traj = store.start(system_prompt="你是一个通过工具干活的通用 agent。")
    assert traj.header["system_prompt"] == "你是一个通过工具干活的通用 agent。"
    # 从盘上重建（模拟重启）后仍在——header 是文件第一行，不是内存里的东西
    resumed = store.resume(traj.sid)
    assert resumed.header["system_prompt"] == "你是一个通过工具干活的通用 agent。"


def test_projection_defaults_to_header_prompt(workdir: Path) -> None:
    """build_context 不传 system_prompt 时默认读 header——从盘上重放一段
    历史，用什么 prompt 记账上写着；显式传参（换 prompt 重放）才覆盖。"""
    import json

    from baby_event_driven_agent.stages.stage04_trajectory.agent import build_context

    store = SessionStore(workdir / "sessions")
    traj = store.start(system_prompt="SYS-header")
    traj.append(MESSAGE, message_payload({"role": "user", "content": "问"}))
    msgs = build_context(traj).messages
    assert msgs[0] == {"role": "system", "content": "SYS-header"}


def test_resume_rebuilds_tree_and_continues(workdir: Path) -> None:
    """resume：树从文件重建，接着能追加；正常 resume 一个 entry 都不追加（字节不变）。"""
    store = SessionStore(workdir / "sessions")
    traj = store.start(model="m")
    u = traj.append(MESSAGE, message_payload({"role": "user", "content": "第一句"}))
    traj.append(MESSAGE, message_payload({"role": "assistant", "content": "第一答"}))
    bytes_before = traj.log.raw_bytes()

    resumed = store.resume(traj.sid)  # 模拟重启：全新的 Trajectory 实例
    assert resumed.get(u.id).payload["message"]["content"] == "第一句"
    assert resumed.last_message_role() == "assistant"  # leaf 之前的消息链完整
    # 生命周期不落账：正常 resume 不追加任何东西，文件字节不变
    assert resumed.log.raw_bytes() == bytes_before
    resumed.append(MESSAGE, message_payload({"role": "user", "content": "第二句"}))

    again = store.resume(traj.sid)
    assert [e.payload["message"].get("content") for e in again.entries() if e.type == MESSAGE] == [
        "第一句",
        "第一答",
        "第二句",
    ]


def test_resume_marks_torn_tail(workdir: Path) -> None:
    """残尾在 resume 时被判定：裁掉残尾字节，补一条 synthetic 占位 entry 留痕。"""
    store = SessionStore(workdir / "sessions")
    traj = store.start()
    traj.append(MESSAGE, message_payload({"role": "user", "content": "u1"}))
    path = store.path_of(traj.sid)
    bytes_before = path.read_bytes()
    path.write_bytes(bytes_before[:-11])
    resumed = store.resume(traj.sid)
    # 实例上的 torn 标志在第一次追加（占位 entry）时消费掉：残尾已裁
    assert resumed.torn is False
    # 留痕 = synthetic assistant 占位（note 自述残尾），不是生命周期 entry
    leaf = resumed.leaf
    assert leaf is not None and leaf.type == MESSAGE
    assert leaf.payload["synthetic"] is True
    assert leaf.payload["message"]["content"].startswith("[UNKNOWN:")
    from baby_event_driven_agent.stages.stage04_trajectory.session.trajectory import TrajectoryLog

    assert TrajectoryLog(path).read()[2] is False  # 文件回到完好状态


def test_resume_unknown_sid_raises(workdir: Path) -> None:
    store = SessionStore(workdir / "sessions")
    with pytest.raises(KeyError):
        store.resume("no-such-session")


def test_fork_registers_new_session_in_store(workdir: Path) -> None:
    store = SessionStore(workdir / "sessions")
    traj = store.start()
    traj.append(MESSAGE, message_payload({"role": "user", "content": "u1"}))
    forked = store.fork(traj, note="分叉演练")
    assert traj.sid != forked.sid
    assert store.list_sessions() == sorted([traj.sid, forked.sid])
    # 血缘记在新 header 的 parent_session 里（审计留痕，不是引用）
    assert forked.header["parent_session"] == traj.sid
    assert traj.header.get("parent_session") is None  # 旧会话 header 原封不动
    # 旧会话原封不动：fork 之后原 leaf 仍是自己的
    assert traj.leaf.type == MESSAGE


# ------------------------------------------------------------------ 悬挂审批闭合


def test_sweep_closes_hanging_approvals(workdir: Path) -> None:
    """孤立的 approval_required → 补 abandoned 裁决；已闭合的不碰；再扫幂等。"""
    log = EventLog(str(workdir / "events"))
    bus = EventBus(log)
    sid = "S"

    bus.record(Event("approval_required", sid, {"request_id": "ap-1"}))  # 悬空
    bus.record(Event("approval_required", sid, {"request_id": "ap-2"}))
    bus.record(Event("approval_decided", sid, {"request_id": "ap-2", "action": "allow"}))
    bus.record(Event("approval_required", "OTHER", {"request_id": "ap-3"}))  # 别的 session

    closed = sweep_hanging_approvals(bus, sid)
    assert closed == ["ap-1"]
    assert sweep_hanging_approvals(bus, sid) == []  # 幂等

    decided = [
        r["payload"] for r in EventLog(str(workdir / "events")).read_since(0)
        if r["type"] == "approval_decided"
    ]
    abandoned = [d for d in decided if d["action"] == "abandoned"]
    assert [d["request_id"] for d in abandoned] == ["ap-1"]
    assert abandoned[0]["by"] == "session_resume"


def test_sweep_without_log_is_noop(workdir: Path) -> None:
    bus = EventBus(None)  # 没挂 EventLog：没账可查，直接空手而归
    assert sweep_hanging_approvals(bus, "S") == []


def test_twin_trajectories_same_file_projection(workdir: Path) -> None:
    """同一份文件的两个实例：投影一致（f(文件, 参数) 是纯函数）。"""
    import json

    from baby_event_driven_agent.stages.stage04_trajectory.agent import build_context
    from baby_event_driven_agent.stages.stage04_trajectory.session.trajectory import (
        Trajectory,
        TrajectoryLog,
    )

    store = SessionStore(workdir / "sessions")
    traj = store.start()
    traj.append(MESSAGE, message_payload({"role": "user", "content": "u1"}))
    twin = Trajectory.load(TrajectoryLog(store.path_of(traj.sid)))
    a = json.dumps(build_context(traj).messages, ensure_ascii=False)
    b = json.dumps(build_context(twin).messages, ensure_ascii=False)
    assert a == b
