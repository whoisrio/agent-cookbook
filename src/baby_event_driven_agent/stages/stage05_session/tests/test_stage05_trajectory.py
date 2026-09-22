"""Stage 5a 轨迹层用例：全部离线（不打模型）。

覆盖：
- 树操作：append O(1)、认父不认子、线性轨迹无分叉点、id 唯一
- 投影：路径遍历 + 类型分派 + sanitize；确定性（同文件同参数逐字节相同）；
  model_change 覆盖式提取；元数据跳过
- 压缩口子：keep_from 之前的跳过、摘要插在最前；rewind 到压缩之前旧消息原样回来
- rewind：只移指针，被抛弃分支留在文件里；回退后追加 = 分支
- branch_summary：遗言不是对话（<summary> 视图，被抛弃分支不进上下文）
- 落盘：CRC 框架下的残尾判定；fork 出的文件独立
- 修复：悬挂工具调用两档修复，且原文件字节不变（投影不写回事实层）
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from baby_event_driven_agent.stages.stage05_session.agent import build_context
from baby_event_driven_agent.stages.stage05_session.session.trajectory import (
    COMPACTION,
    LABEL,
    MESSAGE,
    MODEL_CHANGE,
    PROMPT_CHANGE,
    Trajectory,
    TrajectoryLog,
    message_payload,
)



@pytest.fixture()
def workdir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stage05_traj_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def new_traj(workdir: Path, name: str = "t.jsonl") -> Trajectory:
    return Trajectory.create(TrajectoryLog(workdir / name), sid="s" * 32, system_prompt="SYS")


def user(traj: Trajectory, text: str, **kw: Any):
    return traj.append(MESSAGE, message_payload({"role": "user", "content": text}, **kw))


def assistant(traj: Trajectory, content: str, calls: list[dict] | None = None, **kw: Any):
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = calls
    return traj.append(MESSAGE, message_payload(msg, **kw))


def tool(traj: Trajectory, call_id: str, content: str, **kw: Any):
    return traj.append(
        MESSAGE, message_payload({"role": "tool", "tool_call_id": call_id, "content": content}, **kw)
    )


def call(cid: str = "c1") -> list[dict]:
    return [
        {"id": cid, "type": "function", "function": {"name": "read", "arguments": "{}"}}
    ]


# ------------------------------------------------------------------ 树操作


def test_append_is_o1_and_linear(workdir: Path) -> None:
    """追加只创建新节点 + 移动 leaf；纯链轨迹没有分叉点；认父不认子。"""
    traj = new_traj(workdir)
    u = user(traj, "问")
    a = assistant(traj, "答")
    ids = [e.id for e in traj.entries()]
    assert traj.leaf.id == a.id
    assert a.parent_id == u.id
    assert traj.branch_points() == {}  # 无分叉：每行只被一个孩子认领
    # 认父不认子：文件里只有 parentId，没有 children 字段
    raw = traj.log.raw_bytes().decode()
    assert '"parentId"' in raw and '"children"' not in raw
    assert len(ids) == 2


def test_duplicate_id_rejected(workdir: Path) -> None:
    """id 冲突直接炸：轨迹的坐标系统不允许二义。"""
    traj = new_traj(workdir)
    u = user(traj, "问")
    with pytest.raises(ValueError, match="冲突"):
        traj._index(type(u)(u.id, None, MESSAGE, u.ts, {}))


def test_path_traversal_root_to_leaf(workdir: Path) -> None:
    """path() 从 leaf 沿 parentId 回根再 reverse：根→叶顺序。"""
    traj = new_traj(workdir)
    u = user(traj, "问")
    a = assistant(traj, "答")
    assert [e.id for e in traj.path()] == [u.id, a.id]


# ------------------------------------------------------------------ 投影


def test_projection_dispatch_and_determinism(workdir: Path) -> None:
    """消息进 messages、model_change 覆盖、元数据跳过；两次投影逐字节相同。"""
    traj = new_traj(workdir)
    traj.append(MODEL_CHANGE, {"model_id": "m1"})
    user(traj, "问")
    assistant(traj, "答")
    traj.append(LABEL, {"text": "书签"})  # 纯元数据：进轨迹不进上下文
    user(traj, "换个问法")

    p1 = build_context(traj)
    p2 = build_context(traj)
    assert json.dumps(p1.messages) == json.dumps(p2.messages)
    assert p1.model == "m1"  # 覆盖式提取：路径上最后一次生效
    assert [m["role"] for m in p1.messages] == ["system", "user", "assistant", "user"]
    assert p1.stats["skipped"] == 1  # label 被跳过（model_change 是状态提取，不算 skip）


def test_projection_model_from_facts_only(workdir: Path) -> None:
    """model 只从事实提取：路径上没有 model_change 就是 None，兜底是 agent 的事。"""
    traj = new_traj(workdir)
    assert build_context(traj).model is None  # 没记过就是没记过
    traj.append(MODEL_CHANGE, {"model_id": "m1"})
    u = user(traj, "问")
    assistant(traj, "答1")
    traj.branch(u.id)  # 回退掉 model_change 之后的输出，但 model_change 仍在路径上
    traj.append(MODEL_CHANGE, {"model_id": "m2"})
    assert build_context(traj).model == "m2"


def test_sanitize_dangling_tool_calls_get_placeholder(workdir: Path) -> None:
    """悬挂的工具调用（assistant 要了结果、结果没来）：补自描述占位。

    修复只改投影——原文件字节必须一个不变（修复不写回事实层）。
    """
    traj = new_traj(workdir)
    user(traj, "改库存")
    assistant(traj, None, calls=call("c1"))
    before = traj.log.raw_bytes()

    fixed = build_context(traj)
    tool_msgs = [m for m in fixed.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1 and "UNKNOWN" in tool_msgs[0]["content"]
    assert tool_msgs[0]["tool_call_id"] == "c1"
    assert fixed.stats["repaired"] == 1
    assert traj.log.raw_bytes() == before  # 修复没碰文件


def test_sanitize_drops_orphan_tool_and_keeps_system_first(workdir: Path) -> None:
    """落单的 tool 结果（配不上任何调用）跳过；system 永远第一条。"""
    traj = new_traj(workdir)
    user(traj, "问")
    tool(traj, "c-none", "孤儿结果")
    p = build_context(traj)
    assert [m["role"] for m in p.messages] == ["system", "user"]


def test_prompt_change_is_state_fact(workdir: Path) -> None:
    """prompt 变更是改状态事实：prompt_change 覆盖式提取（header 兜底），
    rewind 到变更之前视图回到旧 prompt——和 model_change 同一个模式。"""
    traj = new_traj(workdir)  # header: SYS
    u1 = user(traj, "问1")
    assert build_context(traj).messages[0]["content"] == "SYS"  # 没变更：回落 header

    traj.append(PROMPT_CHANGE, {"system_prompt": "SYS-v2", "by": "agent_attach"})
    user(traj, "问2")
    assert build_context(traj).messages[0]["content"] == "SYS-v2"

    traj.branch(u1.id)  # rewind 到 prompt_change 之前：视图回到 header 的旧 prompt
    assert build_context(traj).messages[0]["content"] == "SYS"


# ------------------------------------------------------------------ 压缩口子


def test_compaction_skips_before_keep_from(workdir: Path) -> None:
    """压缩口子：摘要插在最前，切割点之前的跳过，之后的照常。

    切割点必须选在序列合法的边界（这里选一轮的开头 u2）——选在 turn 中间
    会让保留段以孤儿 tool 结果开头，那会被 sanitize 丢掉（孤儿结果没有可
    配对的调用，发出去 provider 直接拒）。
    """
    traj = new_traj(workdir)
    user(traj, "u1")
    assistant(traj, "a1")
    tool(traj, "c1", "结果")
    assistant(traj, "a2")
    u2 = user(traj, "u2")
    assistant(traj, "a3")
    traj.append(COMPACTION, {"summary": "之前查过库存", "keep_from_id": u2.id})
    user(traj, "u3")

    p = build_context(traj)
    roles = [m["role"] for m in p.messages]
    assert roles == ["system", "user", "user", "assistant", "user"]
    assert p.messages[1]["content"] == "<summary>之前查过库存</summary>"  # 摘要在最前
    assert p.messages[2]["content"] == "u2"  # 切割点之后的照常保留
    assert "u1" not in json.dumps(p.messages)  # 切割点之前的被跳过
    assert p.stats["skipped"] == 4  # u1、a1、tool、a2 都在切割点之前


def test_rewind_before_compaction_restores_old_messages(workdir: Path) -> None:
    """回退到压缩节点之前：路径不含 compaction，旧消息原样回来——压缩是视图。"""
    traj = new_traj(workdir)
    u = user(traj, "u1")
    assistant(traj, "a1")
    comp = traj.append(
        COMPACTION, {"summary": "摘要", "keep_from_id": u.id}
    )
    user(traj, "u2")
    assert "<summary>" in json.dumps(build_context(traj).messages)

    traj.branch(comp.parent_id)  # 回到 u1（compaction 的父）
    p = build_context(traj)
    assert [m["content"] for m in p.messages] == ["SYS", "u1", "a1"]  # 旧消息回来了


# ------------------------------------------------------------------ rewind / 分支


def test_rewind_moves_pointer_only(workdir: Path) -> None:
    """branch 只移 leafId：文件一条不删、字节不变；被抛弃分支投影不可见。"""
    traj = new_traj(workdir)
    u = user(traj, "u1")
    assistant(traj, "a1")
    before = traj.log.raw_bytes()
    traj.branch(u.id)
    assert traj.log.raw_bytes() == before
    assert len(traj.entries()) == 2  # 数据还在
    assert build_context(traj).messages[-1]["content"] == "u1"
    traj.branch(u.id)  # 重复 rewind 到同一点：幂等


def test_append_after_rewind_creates_branch(workdir: Path) -> None:
    """回退后追加 = 分支：两个节点共享同一个 parent（grep parentId 可见）。"""
    traj = new_traj(workdir)
    u = user(traj, "u1")
    a1 = assistant(traj, "a1")
    traj.branch(u.id)
    user(traj, "u2")
    kids = traj.branch_points()[u.id]
    assert kids == [a1.id, traj.leaf.id]  # a1 和 u2 共享父 u1
    assert build_context(traj).messages[-1]["content"] == "u2"


def test_branch_summary_is_a_view_not_dialogue(workdir: Path) -> None:
    """带摘要的 rewind：摘要进上下文（<summary>），被抛弃分支不进。"""
    traj = new_traj(workdir)
    u = user(traj, "u1")
    assistant(traj, "a1")
    traj.branch(u.id)
    user(traj, "u2")
    s = traj.branch_with_summary(u.id, "试过 a1 和 u2，都不对。")
    assert s.parent_id == u.id  # 摘要与被抛弃分支同父
    msgs = build_context(traj).messages
    assert [m["content"] for m in msgs] == ["SYS", "u1", "<summary>试过 a1 和 u2，都不对。</summary>"]
    assert s.payload["discarded"] == 1  # 这次抛弃的是 u2；a1 在上一次 branch 就已出路径


# ------------------------------------------------------------------ 落盘与恢复


def test_torn_tail_stops_at_last_good_entry(workdir: Path) -> None:
    """崩在写一半：CRC 判定残尾，load 停在最后一条完好 entry，torn 留痕。

    残尾不构成事实；在它后面追加之前先被裁掉（否则残尾赖在文件中间，
    新记录永远读不到）。
    """
    traj = new_traj(workdir)
    user(traj, "u1")
    assistant(traj, "a1")
    path = traj.log.path
    raw = path.read_bytes()
    path.write_bytes(raw[:-11])
    header, entries, torn = TrajectoryLog(path).read()
    assert torn is True
    assert [e["payload"]["message"]["content"] for e in entries] == ["u1"]
    reloaded = Trajectory.load(TrajectoryLog(path))
    assert reloaded.torn and reloaded.leaf.payload["message"]["content"] == "u1"
    reloaded.append(MESSAGE, message_payload({"role": "user", "content": "接着来"}))
    header2, entries2, torn2 = TrajectoryLog(path).read()
    assert torn2 is False  # 残尾已被裁掉
    assert [e["payload"]["message"]["content"] for e in entries2] == [
        "u1",
        "接着来",
    ]  # a1（没写完的那条）不算已发生；后续记录可读


def test_fork_produces_independent_session(workdir: Path) -> None:
    """fork：新文件是完整合法轨迹（id/parentId 原样），两边互不影响。"""
    traj = new_traj(workdir)
    u = user(traj, "u1")
    assistant(traj, "a1")
    forked = traj.fork(TrajectoryLog(workdir / "f.jsonl"), sid="f" * 32)

    reloaded = Trajectory.load(TrajectoryLog(workdir / "f.jsonl"))
    # fork 会补一条 session_resumed（元数据），所以文件里是 3 条；路径上前两条同源
    assert [e.id for e in reloaded.entries()][:2] == [u.id, traj.entries()[1].id]
    assert reloaded.entries()[-1].type == "session_resumed"
    assert build_context(reloaded).messages[-1]["content"] == "a1"

    traj.append(MESSAGE, message_payload({"role": "user", "content": "旧会话继续"}))
    forked.append(MESSAGE, message_payload({"role": "user", "content": "新会话继续"}))
    assert build_context(traj).messages[-1]["content"] == "旧会话继续"
    assert build_context(forked).messages[-1]["content"] == "新会话继续"
    assert forked.entries()[0].id == u.id  # 共享历史 id，但文件各自独立
    assert traj.log.path != forked.log.path


def test_projection_is_pure_function_of_file(workdir: Path) -> None:
    """f(文件, 参数) 的另一面：两个 Trajectory 实例读同一份文件，投影相同。"""
    traj = new_traj(workdir)
    user(traj, "u1")
    assistant(traj, "a1")
    twin = Trajectory.load(TrajectoryLog(traj.log.path))
    assert json.dumps(build_context(traj).messages) == json.dumps(
        build_context(twin).messages
    )
