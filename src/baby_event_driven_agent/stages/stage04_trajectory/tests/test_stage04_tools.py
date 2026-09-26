"""工具层测试：任务域、判量审批边界、数据隔离、设值语义（全部离线）。

- 任务域：list_tasks / get_task 的返回形状；未知任务号的回落；只读不写
- 判量审批：贴线两侧（补 50 放行、补 51 问人）、大额问人、负差放行、
  参数判不了按 fail-closed 问人
- 数据隔离：写操作落在工作目录副本，包自带 data/ 与共享 knowledge-base 不动
- 设值语义：同一目标值重放两次，结果不叠加
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from baby_event_driven_agent.stages.stage04_trajectory import tools as tools_mod
from baby_event_driven_agent.stages.stage04_trajectory.tools import (
    get_task,
    list_tasks,
    query_inventory,
    update_inventory,
    _restock_approval,
)


@pytest.fixture()
def workdir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stage04_tools_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def data_copies(workdir: Path):
    """数据源换到工作目录副本；结束还原。写操作只落副本。"""
    attrs = ("_INVENTORY", "_RULES", "_TASKS")
    saved = {a: getattr(tools_mod, a) for a in attrs}
    kb_dir = saved["_INVENTORY"].parents[2] / "knowledge-base"
    kb_before = {p.name: p.read_bytes() for p in kb_dir.glob("*.txt")}
    data_before = {a: p.read_bytes() for a, p in saved.items()}
    for a in attrs:
        src: Path = saved[a]
        dst = workdir / src.name
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        setattr(tools_mod, a, dst)
    yield workdir
    for a in attrs:
        setattr(tools_mod, a, saved[a])
    # 包自带 data/ 与共享 knowledge-base 一个字节没动
    assert {a: p.read_bytes() for a, p in saved.items()} == data_before
    assert {p.name: p.read_bytes() for p in kb_dir.glob("*.txt")} == kb_before


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


# ------------------------------------------------------------------ 任务域


def test_list_tasks_shape() -> None:
    out = run(list_tasks({}))
    assert "T-101 补货核查（4 项）：按目标库存补货" in out
    assert "T-102 盘点差异（3 项）：按实物调整库存" in out


def test_get_task_returns_header_and_items() -> None:
    out = run(get_task({"task_id": "T-102"}))
    lines = out.splitlines()
    assert lines[0].startswith("T-102 盘点差异")
    assert "T-102 雨伞：实物 13" in out
    assert "T-102 帆布包：实物 30" in out
    assert "T-102 围巾：实物 2" in out
    assert not any(ln.startswith("T-101") for ln in lines)  # 不串单


def test_get_task_unknown_falls_back_to_ids() -> None:
    out = run(get_task({"task_id": "T-404"}))
    assert "未找到任务单：T-404" in out
    assert "T-101" in out and "T-102" in out


def test_task_tools_are_read_only(data_copies: Path) -> None:
    before = (data_copies / "tasks.txt").read_bytes()
    run(list_tasks({}))
    run(get_task({"task_id": "T-101"}))
    assert (data_copies / "tasks.txt").read_bytes() == before


# ------------------------------------------------------------------ 判量审批


def test_restock_approval_boundaries() -> None:
    # 马克杯 8 件：8 → 60 是补 52 件，超限问人
    assert _restock_approval({"category": "马克杯", "stock": 60}) is not None
    # 保温杯 3 件：贴线两侧——补 50（delta 50）放行，补 51（delta 51）问人
    assert _restock_approval({"category": "保温杯", "stock": 53}) is None
    assert _restock_approval({"category": "保温杯", "stock": 54}) is not None
    # 负差（盘点调小）放行
    assert _restock_approval({"category": "雨伞", "stock": 13}) is None
    # 判不了的参数 fail-closed：宁可问人
    assert _restock_approval({"category": "保温杯", "stock": "很多"}) is not None


def test_restock_approval_reads_current_data(data_copies: Path) -> None:
    """判量读的是当前数据源：副本里把保温杯改成 40，补 20 就不再超限。"""
    run(update_inventory({"category": "保温杯", "stock": 40}))
    assert _restock_approval({"category": "保温杯", "stock": 60}) is None  # delta 20
    assert _restock_approval({"category": "保温杯", "stock": 95}) is not None  # delta 55


# ------------------------------------------------------------------ 写语义与隔离


def test_update_inventory_set_semantics_replay(data_copies: Path) -> None:
    """设值语义：同一目标值重放两次，结果不叠加（rewind / 重放的安全前提）。"""
    run(update_inventory({"category": "保温杯", "stock": 50}))
    first = (data_copies / "inventory.txt").read_text(encoding="utf-8")
    run(update_inventory({"category": "保温杯", "stock": 50}))
    second = (data_copies / "inventory.txt").read_text(encoding="utf-8")
    assert first == second
    assert "保温杯：库存 50 件" in run(query_inventory({"category": "保温杯"}))


def test_update_inventory_preserves_other_lines(data_copies: Path) -> None:
    run(update_inventory({"category": "保温杯", "stock": 50}))
    out = (data_copies / "inventory.txt").read_text(encoding="utf-8")
    assert "马克杯：库存 8 件" in out  # 别的品类原样
    assert "围巾：库存 8 件" in out
