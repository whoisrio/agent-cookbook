"""Stage 5a 会话层用例：全部离线。

覆盖：
- sid 由 store 分配；start 落 header + session_started（+ 初始 model_change）
- resume 重建树、从 leaf 继续（不重复事实）；torn_tail 留痕
- close 落 session_end；生命周期事件都能从轨迹里读出来
- 悬挂审批闭合：孤立的 approval_required 补 abandoned 裁决，闭合过的不碰（幂等）
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from baby_event_driven_agent.stages.stage05_session.transport.bus import EventBus
from baby_event_driven_agent.stages.stage05_session.transport.events import Event
from baby_event_driven_agent.stages.stage05_session.transport.persistence import EventLog
from baby_event_driven_agent.stages.stage05_session.session.store import (
    SessionStore,
    session_facts,
    sweep_hanging_approvals,
)
from baby_event_driven_agent.stages.stage05_session.session.trajectory import (
    MESSAGE,
    Trajectory,
    message_payload,
)


@pytest.fixture()
def workdir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stage05_session_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_sid_is_allocated_by_store(workdir: Path) -> None:
    """sid 由 store 分配（不是调用方随口给），文件名就是 sid。"""
    store = SessionStore(workdir / "sessions")
    traj = store.start(cwd="/tmp", model="fake-model")
    assert traj.sid and len(traj.sid) == 32
    assert store.path_of(traj.sid).exists()
    facts = session_facts(traj)
    assert [f["type"] for f in facts] == ["session_started", "model_change"]
    assert facts[1]["payload"]["model_id"] == "fake-model"


def test_start_without_model_has_no_model_change(workdir: Path) -> None:
    store = SessionStore(workdir / "sessions")
    traj = store.start()
    assert [f["type"] for f in session_facts(traj)] == ["session_started"]


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

    from baby_event_driven_agent.stages.stage05_session.agent import build_context

    store = SessionStore(workdir / "sessions")
    traj = store.start(system_prompt="SYS-header")
    traj.append(MESSAGE, message_payload({"role": "user", "content": "问"}))
    msgs = build_context(traj).messages
    assert msgs[0] == {"role": "system", "content": "SYS-header"}


def test_resume_rebuilds_tree_and_continues(workdir: Path) -> None:
    """resume：树从文件重建，接着能追加（resumed 事件本身也在轨迹里留痕）。"""
    store = SessionStore(workdir / "sessions")
    traj = store.start(model="m")
    u = traj.append(MESSAGE, message_payload({"role": "user", "content": "第一句"}))
    traj.append(MESSAGE, message_payload({"role": "assistant", "content": "第一答"}))

    resumed = store.resume(traj.sid)  # 模拟重启：全新的 Trajectory 实例
    assert resumed.get(u.id).payload["message"]["content"] == "第一句"
    assert resumed.last_message_role() == "assistant"  # leaf 之前的消息链完整
    resumed.append(MESSAGE, message_payload({"role": "user", "content": "第二句"}))

    again = store.resume(traj.sid)
    assert [e.payload["message"].get("content") for e in again.entries() if e.type == MESSAGE] == [
        "第一句",
        "第一答",
        "第二句",
    ]
    facts = [f["type"] for f in session_facts(again)]
    assert facts == ["session_started", "model_change", "session_resumed", "session_resumed"]


def test_resume_marks_torn_tail(workdir: Path) -> None:
    """残尾在 resume 时被判定：裁掉残尾字节，resumed 事件带 torn_tail=true 留痕。"""
    store = SessionStore(workdir / "sessions")
    traj = store.start()
    traj.append(MESSAGE, message_payload({"role": "user", "content": "u1"}))
    path = store.path_of(traj.sid)
    path.write_bytes(path.read_bytes()[:-11])
    resumed = store.resume(traj.sid)
    # 实例上的 torn 标志在第一次追加（session_resumed）时消费掉：残尾已裁
    assert resumed.torn is False
    assert session_facts(resumed)[-1]["payload"]["torn_tail"] is True
    from baby_event_driven_agent.stages.stage05_session.session.trajectory import TrajectoryLog

    assert TrajectoryLog(path).read()[2] is False  # 文件回到完好状态


def test_resume_unknown_sid_raises(workdir: Path) -> None:
    store = SessionStore(workdir / "sessions")
    with pytest.raises(KeyError):
        store.resume("no-such-session")


def test_close_appends_session_end(workdir: Path) -> None:
    store = SessionStore(workdir / "sessions")
    traj = store.start()
    store.close(traj, reason="demo 结束")
    facts = session_facts(traj)
    assert facts[-1]["type"] == "session_end"
    assert facts[-1]["payload"]["reason"] == "demo 结束"


def test_fork_registers_new_session_in_store(workdir: Path) -> None:
    store = SessionStore(workdir / "sessions")
    traj = store.start()
    traj.append(MESSAGE, message_payload({"role": "user", "content": "u1"}))
    forked = store.fork(traj, note="分叉演练")
    assert traj.sid != forked.sid
    assert store.list_sessions() == sorted([traj.sid, forked.sid])
    assert session_facts(forked)[-1]["payload"]["forked_from"] == traj.sid
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

    from baby_event_driven_agent.stages.stage05_session.agent import build_context
    from baby_event_driven_agent.stages.stage05_session.session.trajectory import (
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
