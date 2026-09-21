"""outbound 的落盘：段 + 稀疏索引 + 长度前缀 + CRC + 保留 + 脱敏 + 位点。

为什么不再用 stage01 那种“一行一条 json”：

进程崩在写一半的时候，最后一行是残的，而“这条记录完不完整”在 jsonl 里
没法判定——读的一方只能 try/except，炸了之后要么整段读不出来，要么人肉修。
长度前缀 + CRC 把它变成可判定的问题：长度对不上或 CRC 对不上就是残尾，
读到它为止，前面已落盘的一条不少（崩进程不丢已落盘的部分）。

段与索引：写满一段就滚动新段，`index.json` 记每段的 first/last seq（**稀疏
索引**，段内顺序扫描，和 Kafka 一个路子）。保留策略按段数滚动删除最老的段。

位点：消费者把“已消费到的最后一条 seq”存进 `offsets.json`，重启后从下一条
续读——这就是断点续放。
"""

from __future__ import annotations

import json
import re
import zlib
from pathlib import Path
from typing import Any, Callable

_SECRET_KEY = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|authorization|cookie|credential)",
    re.IGNORECASE,
)


def redact_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    """默认脱敏：只改**落盘**那份，内存里的事件不动。

    改内存里的会让治理看到的和落下的不一致——落盘是脱敏的最后一道，不是第一道。
    """

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: ("***" if _SECRET_KEY.search(str(k)) and isinstance(v, str) else walk(v))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    return walk(payload)  # type: ignore[return-value]


class EventLog:
    """append-only 的事件落盘。seq 从这里出：落盘即编号，重启接着走。"""

    def __init__(
        self,
        path: str | Path,
        *,
        segment_bytes: int = 64 * 1024,
        keep_segments: int = 8,
        redact: Callable[[dict[str, Any]], dict[str, Any]] | None = redact_secrets,
    ) -> None:
        self._dir = Path(path)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._segment_bytes = segment_bytes
        self._keep_segments = keep_segments
        self._redact = redact
        self._index = self._load_index()
        self._offsets = self._load_offsets()
        # 段号只增不减：保留策略删掉老段之后，段数变少了，若按“段数 +1”命名
        # 就会撞上还在写的那个文件名（实测撞出来过 3 段：索引里多了一条指向
        # 同一文件的条目）。段号从现存最大号续，重启也不会重用旧名。
        self._seg_no = max(
            (int(p.stem.split("-")[1]) for p in self._dir.glob("evt-*.log")), default=0
        )
        current = self._index["segments"][-1]["file"] if self._index["segments"] else None
        if current is None:
            current = self._new_segment()
        self._current = self._dir / current
        self._fh = open(self._current, "ab")

    # ---------------------------------------------------------------- 写

    def append(self, record: dict[str, Any]) -> int:
        """落一条，返回它拿到的 seq。整段没有 await，单线程下是原子的。"""
        seq = int(self._index["last_seq"]) + 1
        record = dict(record)
        record["seq"] = seq
        if self._redact is not None:
            record["payload"] = self._redact(record.get("payload", {}))
        data = json.dumps(record, ensure_ascii=False).encode("utf-8")
        # 长度前缀 + CRC：崩进程时残尾可判定（flush 到 OS，不 fsync——
        # fsync 在 token 流的热路径上太贵；崩机器可能丢最后几行）
        line = b"%08x %08x " % (len(data), zlib.crc32(data)) + data + b"\n"
        self._fh.write(line)
        self._fh.flush()
        seg = self._index["segments"][-1]
        seg["bytes"] = int(seg["bytes"]) + len(line)
        seg["last_seq"] = seq
        self._index["last_seq"] = seq
        if int(seg["bytes"]) >= self._segment_bytes:
            self._roll()
        return seq

    def _new_segment(self) -> str:
        self._seg_no += 1
        name = f"evt-{self._seg_no:06d}.log"
        base = int(self._index["last_seq"]) + 1
        self._index["segments"].append(
            {"file": name, "first_seq": base, "last_seq": base - 1, "bytes": 0}
        )
        return name

    def _roll(self) -> None:
        self._fh.close()
        name = self._new_segment()  # 先接上新段，再按保留策略删老的
        while len(self._index["segments"]) > self._keep_segments:
            old = self._index["segments"].pop(0)
            (self._dir / old["file"]).unlink(missing_ok=True)
        self._current = self._dir / name
        self._fh = open(self._current, "ab")
        self._write_index()

    def close(self) -> None:
        self._fh.close()
        self._write_index()

    # ---------------------------------------------------------------- 读

    def read_since(self, seq: int = 0) -> list[dict[str, Any]]:
        """位点读：返回 seq 严格大于参数的所有记录；撞上残尾就停在那里。"""
        out: list[dict[str, Any]] = []
        for entry in self._index["segments"]:
            if int(entry["last_seq"]) < seq + 1:
                continue
            path = self._dir / entry["file"]
            if not path.exists():
                continue
            for record, ok in self._read_segment(path):
                if not ok:
                    return out  # 残尾：后面的不读（未 flush 的不算已发生）
                if int(record["seq"]) > seq:
                    out.append(record)
        return out

    @staticmethod
    def _read_segment(path: Path) -> list[tuple[dict[str, Any] | None, bool]]:
        out: list[tuple[dict[str, Any] | None, bool]] = []
        with open(path, "rb") as f:
            while True:
                line = f.readline()
                if not line:
                    return out
                parts = line.split(b" ", 2)
                if len(parts) != 3:
                    out.append((None, False))
                    return out
                try:
                    length = int(parts[0], 16)
                    crc = int(parts[1], 16)
                except ValueError:
                    out.append((None, False))
                    return out
                data = parts[2][:-1] if parts[2].endswith(b"\n") else parts[2]
                if len(data) != length or zlib.crc32(data) != crc:
                    out.append((None, False))  # 长度/CRC 对不上 = 崩在写一半
                    return out
                out.append((json.loads(data.decode("utf-8")), True))
        return out

    # ---------------------------------------------------------------- 位点

    def commit(self, name: str, seq: int) -> None:
        """记下“这个消费者已消费到 seq”。"""
        self._offsets[name] = int(seq)
        self._write_offsets()

    def offset(self, name: str) -> int:
        return int(self._offsets.get(name, 0))

    # ---------------------------------------------------------------- 元信息

    @property
    def last_seq(self) -> int:
        return int(self._index["last_seq"])

    @property
    def oldest_seq(self) -> int:
        """现存最老的一条（保留策略删掉的老段不在其中）。位点落在它之前时，
        只能从它开始续——这是保留与位点之间唯一的冲突点，如实暴露。"""
        segs = self._index["segments"]
        return int(segs[0]["first_seq"]) if segs else 0

    def stats(self) -> dict[str, Any]:
        segs = self._index["segments"]
        return {
            "dir": str(self._dir),
            "segments": len(segs),
            "bytes": sum(int(s["bytes"]) for s in segs),
            "last_seq": self.last_seq,
            "oldest_seq": self.oldest_seq,
            "offsets": dict(self._offsets),
        }

    # ---------------------------------------------------------------- 内部

    def _index_path(self) -> Path:
        return self._dir / "index.json"

    def _offsets_path(self) -> Path:
        return self._dir / "offsets.json"

    def _load_index(self) -> dict[str, Any]:
        path = self._index_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("segments"):
                    return data
            except (ValueError, TypeError):
                pass
        # 索引丢了就扫描重建：段文件自己是自描述的
        segs: list[dict[str, Any]] = []
        last = 0
        for p in sorted(self._dir.glob("evt-*.log")):
            first: int | None = None
            last_in = last
            for record, ok in self._read_segment(p):
                if not ok:
                    break
                if first is None:
                    first = int(record["seq"])
                last_in = int(record["seq"])
            segs.append(
                {
                    "file": p.name,
                    "first_seq": first if first is not None else last + 1,
                    "last_seq": last_in,
                    "bytes": p.stat().st_size,
                }
            )
            last = last_in
        return {"segments": segs, "last_seq": last}

    def _write_index(self) -> None:
        self._index_path().write_text(
            json.dumps(self._index, ensure_ascii=False), encoding="utf-8"
        )

    def _load_offsets(self) -> dict[str, int]:
        path = self._offsets_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}

    def _write_offsets(self) -> None:
        self._offsets_path().write_text(
            json.dumps(self._offsets, ensure_ascii=False), encoding="utf-8"
        )
