"""轨迹层：每个 session 一棵 append-only 的 entry 树（pi 式）+ 投影。

形状参考 pi coding agent 的 session 设计（对照 books/event-driven-agent/05a 的解读），
落盘工程沿用 Stage 4 的长度前缀 + CRC（不用 pi 的裸 jsonl——"残尾可判定"是
恢复的前提，这个工程在 persistence.py 已经造好并测过，这里直接复用同一套框架）。

两个事实层各司其职，不冲突：

- EventLog（总线侧，本包 persistence.py）：全局事件流，多 session，含 token 流
  与治理审计，坐标是 seq——**传输层**的账。
- Trajectory（agent 侧，本模块）：单 session 的对话结构，坐标是 entry id——
  **会话**的账。可 rewind、可 fork。怎么从树 build 上下文不是这里的事——
  那是 agent 的活（agent.py 的 build_context），本模块只提供 path()。

10 种 entry，按"对 LLM 调用的影响"分三组（分类轴就是消费方式）：

- 进上下文：message / branch_summary / compaction
- 改状态：model_change / prompt_change（覆盖式提取，回退天然正确）
- 纯元数据：session_started / session_resumed / session_end / label / custom

prompt_change 是本项目对 pi 的偏离（pi 的 system prompt 在 harness，变了
不落盘）：prompt 变更要审计，就得是事实。初始值在 header，变更以本类型
追加，覆盖式提取——和 model_change 同一个模式，路径上最后一次生效。

树的三条铁律（与 pi 同构）：

1. **认父不认子**：节点只带 parentId，父节点不知道孩子——追加永远是新增，
   从不修改。
2. **append-only**：文件只追加。rewind 不删任何东西，只是移动 leaf 指针。
3. **投影是 agent 侧从树算出来的视图**：messages = f(轨迹, 参数)，可以有损
   （压缩、修复），但每次有损变换都要在事实层留痕；修复只作用于喂给模型的
   副本，**绝不写回文件**。轨迹层不关心上下文怎么 build。

压缩视图（compaction entry）——触发逻辑归 5b，这里把语义钉死：

压缩不能删任何东西（append-only），它只能是一个**视图标记**：

- payload：`summary`（被压缩段的摘要）+ `keep_from_id`（边界指针——
  从那条 entry 起原样保留，之前的跳过、用 summary 代替）；
- 投影规则（实现在 agent.py 的 build_context）：
  1. **只认当前路径上的 compaction**。rewind 到它之前 = 它不在路径上 =
     压缩没发生过，旧消息逐字回来——压缩是视图，不是对数据的手术；
  2. **摘要插在视图最前**。compaction 节点永远在路径末尾（压完才追加），
     但它代表的是最前面那段被压掉的历史——树上位置和视图位置相反，
     `insert(0)` 修正这一点，最终顺序是 [summary, 保留段...]；
  3. **keep_from_id 必须在当前路径上**，不在（比如被 rewind 掉）则压缩
     节点按元数据跳过；且切割点要选在**序列合法的边界**（一轮的开头）——
     选在 turn 中间，保留段以孤儿 tool 结果开头，会被 sanitize 丢弃；
  4. **多次压缩：当前只认路径上第一条**，其后 compaction 的摘要被跳过
     （保留段原文都在，不丢信息，只是该压的没压掉）。折叠语义——新摘要
     必须吞掉旧摘要、投影取最后一刀——归 5b 定义，在此之前不要触发
     第二次压缩。
"""

from __future__ import annotations

import json
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---- entry 类型：按"对 LLM 调用的影响"分三组 ----

# 第一组：进上下文（最终变成 messages 里的一项）
MESSAGE = "message"  # user / assistant / tool；一条 assistant 是一个节点
BRANCH_SUMMARY = "branch_summary"  # 被抛弃分支的摘要：遗言，不是真实对话
COMPACTION = "compaction"  # 压缩摘要 + 切割点（触发逻辑归 5b，这里留口子）

# 第二组：改状态（不产生消息，覆盖式提取）
MODEL_CHANGE = "model_change"
PROMPT_CHANGE = "prompt_change"  # system prompt 变更（不进消息，进审计）

# 第三组：纯元数据（不进上下文、不改参数，给 UI / 扩展 / 回放的人看）
SESSION_STARTED = "session_started"
SESSION_RESUMED = "session_resumed"
SESSION_END = "session_end"
LABEL = "label"
CUSTOM = "custom"

METADATA_TYPES = frozenset({SESSION_STARTED, SESSION_RESUMED, SESSION_END, LABEL, CUSTOM})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _short_id() -> str:
    """8 位短 id（pi 同款）：比完整 UUID 省空间，会话内冲突概率足够低。"""
    return uuid.uuid4().hex[:8]


def message_payload(msg: dict[str, Any], *, synthetic: bool = False, note: str = "") -> dict:
    """message entry 的 payload：`message` 键下是喂给模型的原样 dict，
    synthetic / note 是轨迹自己的注脚（投影时跟随 message 一起留痕，但不进上下文）。"""
    return {"message": msg, "synthetic": synthetic, "note": note}


@dataclass(frozen=True)
class Entry:
    """树上的一个节点。五件套与 pi 对齐：type / id / parentId / timestamp / payload。"""

    id: str
    parent_id: str | None
    type: str
    ts: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "id": self.id,
            "parentId": self.parent_id,
            "timestamp": self.ts,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Entry":
        return cls(
            id=str(data["id"]),
            parent_id=data.get("parentId"),
            type=str(data["type"]),
            ts=str(data.get("timestamp", "")),
            payload=dict(data.get("payload", {})),
        )


# ---------------------------------------------------------------- 落盘


class TrajectoryLog:
    """单会话轨迹文件：第一行是 session header（不是树节点），其后 entry 逐行追加。

    框架与 persistence.EventLog 相同：长度前缀 + CRC。崩在写一半时最后一行
    判不出来（长度/CRC 对不上），读到它为止，前面已落盘的一条不少。
    没有分段、没有位点、没有保留策略——会话轨迹是永久事实，删了就不是
    append-only 了。
    """

    def __init__(self, path: str | Path, *, header: dict[str, Any] | None = None) -> None:
        self.path = Path(path)
        self._good_size: int | None = None  # read() 时记下：最后一条完好记录的边界
        self._fh = open(self.path, "ab")
        if header is not None and self.path.stat().st_size == 0:
            self.append(dict(header))

    def append(self, record: dict[str, Any]) -> None:
        """追加一条（header 或 entry）。整段没有 await，单线程下是原子的。"""
        data = json.dumps(record, ensure_ascii=False).encode("utf-8")
        line = b"%08x %08x " % (len(data), zlib.crc32(data)) + data + b"\n"
        self._fh.write(line)
        self._fh.flush()

    def read(self) -> tuple[dict[str, Any] | None, list[dict[str, Any]], bool]:
        """读整个文件：(header, entries, 是否有残尾)。撞上残尾就停在那里。"""
        header: dict[str, Any] | None = None
        entries: list[dict[str, Any]] = []
        torn = False
        good = 0
        with open(self.path, "rb") as f:
            while True:
                pos = f.tell()
                line = f.readline()
                if not line:
                    good = f.tell()
                    break
                parts = line.split(b" ", 2)
                data = parts[2].rstrip(b"\n") if len(parts) == 3 else b""
                try:
                    length = int(parts[0], 16)
                    crc = int(parts[1], 16)
                    ok = len(parts) == 3 and len(data) == length and zlib.crc32(data) == crc
                except ValueError:
                    ok = False
                if not ok:
                    torn = True  # 崩在写一半：后面的不读（没写完的不算已发生）
                    good = pos  # 完好边界停在坏记录之前
                    break
                record = json.loads(data.decode("utf-8"))
                if record.get("type") == "session":
                    header = record
                else:
                    entries.append(record)
        self._good_size = good
        return header, entries, torn

    def truncate_torn(self) -> bool:
        """把残尾字节裁掉，文件回到最后一条完好记录的边界。

        残尾不是事实（没写完的不算已发生），裁掉它不是改历史——它从来没成过
        历史。不裁的话，残尾字节赖在文件中间，后续追加的记录永远读不到。
        """
        if self._good_size is None:
            self.read()
        assert self._good_size is not None
        if self.path.stat().st_size == self._good_size:
            return False
        self._fh.close()
        with open(self.path, "r+b") as f:
            f.truncate(self._good_size)
        self._fh = open(self.path, "ab")
        return True

    def raw_bytes(self) -> bytes:
        return self.path.read_bytes()

    def close(self) -> None:
        self._fh.close()


# ---------------------------------------------------------------- 树


class Trajectory:
    """一棵已加载的 entry 树。追加 O(1)；rewind 是移动指针；path() 供投影取当前路径。"""

    def __init__(
        self,
        log: TrajectoryLog,
        header: dict[str, Any],
        entries: list[dict[str, Any]],
        *,
        torn: bool = False,
    ) -> None:
        self.log = log
        self.header = dict(header)
        self.torn = torn  # 加载时撞上残尾：tail 之后的字节在文件里但不构成事实
        self._by_id: dict[str, Entry] = {}
        self.leaf_id: str | None = None
        for data in entries:
            self._index(Entry.from_dict(data))

    # ------------------------------------------------------------ 构造

    @classmethod
    def create(
        cls,
        log: TrajectoryLog,
        *,
        sid: str,
        cwd: str = "",
        note: str = "",
        system_prompt: str = "",
    ) -> "Trajectory":
        header = {
            "type": "session",
            "version": 1,
            "id": sid,
            "cwd": cwd,
            "created": _now(),
            "note": note,
            # 开-session 时的 system prompt 原文：审计用。prompt 是参数不进消息树，
            # 但盘上得查得到"这个会话当时用的是哪个 prompt"，否则重放核对不了。
            "system_prompt": system_prompt,
        }
        log.append(header)
        return cls(log, header, [])

    @classmethod
    def load(cls, log: TrajectoryLog) -> "Trajectory":
        header, entries, torn = log.read()
        if header is None:
            raise ValueError(f"轨迹文件没有 header：{log.path}")
        return cls(log, header, entries, torn=torn)

    # ------------------------------------------------------------ 树操作

    @property
    def sid(self) -> str:
        return str(self.header["id"])

    def _index(self, entry: Entry) -> None:
        if entry.id in self._by_id:
            raise ValueError(f"entry id 冲突：{entry.id}")
        if entry.parent_id is not None and entry.parent_id not in self._by_id:
            raise ValueError(f"entry {entry.id} 的父节点不存在：{entry.parent_id}")
        self._by_id[entry.id] = entry
        self.leaf_id = entry.id  # 追加即移动 leaf：树末端永远指向最新事实

    def append(self, etype: str, payload: dict[str, Any]) -> Entry:
        """追加 O(1) 三步：建节点（认父）→ 落盘 → byId + 移 leaf。不修改任何旧节点。

        例外：加载时撞上过残尾的话，第一次追加前先把残尾字节裁掉——
        残尾不构成事实，不裁的话它赖在文件中间，新记录永远读不到。
        """
        if self.torn:
            self.log.truncate_torn()
            self.torn = False
        entry = Entry(
            id=_short_id(),
            parent_id=self.leaf_id,
            type=etype,
            ts=_now(),
            payload=payload,
        )
        self.log.append(entry.to_dict())
        self._index(entry)
        return entry

    def get(self, entry_id: str) -> Entry:
        return self._by_id[entry_id]

    @property
    def leaf(self) -> Entry | None:
        return self._by_id.get(self.leaf_id) if self.leaf_id else None

    def path(self) -> list[Entry]:
        """当前路径：leaf 沿 parentId 走回根，再 reverse 成根→叶顺序。

        只有这条线上的 entry 会进投影——其他分支的数据不是"被过滤"，是
        遍历根本不经过它们。
        """
        out: list[Entry] = []
        cur = self.leaf
        while cur is not None:
            out.append(cur)
            cur = self._by_id.get(cur.parent_id) if cur.parent_id else None
        out.reverse()
        return out

    def branch(self, to_id: str) -> None:
        """rewind：核心就一行——leaf 指针移过去。没有任何节点被删除。

        被抛弃的分支还在 byId 里、还在文件里，只是不在"当前路径"上。
        """
        if to_id not in self._by_id:
            raise KeyError(f"entry 不存在：{to_id}")
        self.leaf_id = to_id

    def discarded_after(self, keep_from_id: str) -> list[Entry]:
        """当前路径上 keep_from 之后的部分（要被抛弃的那段，根→叶顺序）。"""
        entries = self.path()
        try:
            i = entries.index(self._by_id[keep_from_id])
        except ValueError as exc:
            raise KeyError(f"keep_from 不在当前路径上：{keep_from_id}") from exc
        return entries[i + 1 :]

    def branch_with_summary(
        self, keep_from_id: str, summary: str, *, by: str = "user"
    ) -> Entry:
        """带摘要的 rewind：摘要节点挂在 keep_from 下，leaf 移到摘要上。

        效果：新分支的投影里有一份"之前试过 X，结论是 Y"的 <summary> user 消息
        ——知道历史，不被旧分支细节淹没。摘要是遗言不是对话：它是投影成
        <summary> 消息的视图，不是真实发生的消息。
        """
        discarded = self.discarded_after(keep_from_id)
        note = f"抛弃了 {len(discarded)} 个 entry（{discarded[0].id}..{discarded[-1].id}）" if discarded else "无可抛弃的 entry"
        self.branch(keep_from_id)
        return self.append(
            BRANCH_SUMMARY,
            {"summary": summary, "by": by, "discarded": len(discarded), "note": note},
        )

    def fork(
        self,
        new_log: TrajectoryLog,
        *,
        sid: str,
        cwd: str = "",
        note: str = "",
    ) -> "Trajectory":
        """session 切换：把当前路径克隆进一份新文件（id 与 parentId 原样保留）。

        新文件是完整合法的轨迹，可以独立继续生长；本文件原封不动。两条轨迹
        就此分道扬镳，各自的 leaf 各自走。
        """
        new = Trajectory.create(new_log, sid=sid, cwd=cwd, note=note or f"forked from {self.sid}")
        for entry in self.path():
            new.log.append(entry.to_dict())
            new._index(entry)
        new.append(
            SESSION_RESUMED,
            {"forked_from": self.sid, "note": note or f"forked from {self.sid}"},
        )
        return new

    # ------------------------------------------------------------ 投影

    def last_message(self) -> dict[str, Any] | None:
        """当前路径上最后一条真实消息（跳过尾部的元数据节点，如 session_resumed）。

        agent 判断"history 尾巴是什么"用：resume / fork 之后 leaf 可能是元数据，
        但对投影和消息序列而言，元数据是透明的。
        """
        for e in reversed(self.path()):
            if e.type == MESSAGE:
                return dict(e.payload.get("message", {}))
        return None

    def last_message_role(self) -> str | None:
        msg = self.last_message()
        return str(msg.get("role")) if msg else None

    # ------------------------------------------------------------ 观测

    def entries(self) -> list[Entry]:
        """全树（不只当前路径）：按文件顺序。审计 / 统计 / demo 用。"""
        header, data, _ = self.log.read()
        return [Entry.from_dict(d) for d in data]

    def branch_points(self) -> dict[str, list[str]]:
        """分叉点：parentId 相同的节点组（grep '"parentId":"x"' 的程序版）。"""
        by_parent: dict[str | None, list[str]] = {}
        for e in self._by_id.values():
            by_parent.setdefault(e.parent_id, []).append(e.id)
        return {k: v for k, v in by_parent.items() if k is not None and len(v) > 1}
