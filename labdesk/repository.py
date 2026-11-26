"""报文仓储：读取《样品接收与仪器回报》快照，并保留所有回执版本。

快照里每个分组的 ``id`` 唯一，代表已经合并后的“当前事实”。同一试验编号
事后到达的更可靠回报通过 :meth:`Repository.apply_receipt` 写入：原始回执
不删除，``attempts`` 视图只呈现按可靠性/到达顺序合并出的当前事实。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

GROUPS = (
    "subsamples",
    "orders",
    "methods",
    "instruments",
    "attempts",
    "calibrations",
    "reports",
    "custody",
)

# 回执可靠性档位：数值越大越可靠。未标来源时，后到的合并结果优先。
RELIABILITY = {
    "source": 0,
    "manual": 1,
    "instrument": 2,
    "correction": 3,
    "audit": 4,
}


class Repository:
    def __init__(self, packet: Optional[Dict[str, List[dict]]] = None):
        self.packet: Dict[str, List[dict]] = {g: [] for g in GROUPS}
        if packet:
            for group, rows in packet.items():
                self.packet.setdefault(group, [])
                self.packet[group] = copy.deepcopy(rows)
        # 同一 attempt 的全部回执/版本（含初始快照），只追加不覆盖。
        self.receipts: Dict[str, List[dict]] = {}
        self._indexes: Dict[str, Dict[str, dict]] = {}
        self._rebuild_indexes()
        self._seed_receipts()

    # ---------- 基础读取 ----------
    def rows(self, group: str) -> List[dict]:
        return self.packet.setdefault(group, [])

    def by_id(self, group: str, key: str) -> dict:
        return self._indexes[group][key]

    def find(self, group: str, key: Optional[str]) -> Optional[dict]:
        if key is None:
            return None
        return self._indexes.get(group, {}).get(key)

    def has(self, group: str, key: str) -> bool:
        return key in self._indexes.get(group, {})

    def index(self, group: str) -> Dict[str, dict]:
        return self._indexes[group]

    def _rebuild_indexes(self) -> None:
        self._indexes = {}
        for group, rows in self.packet.items():
            idx: Dict[str, dict] = {}
            for row in rows:
                if "id" in row:
                    idx[row["id"]] = row
            self._indexes[group] = idx
        for group in GROUPS:
            self._indexes.setdefault(group, {})

    def _seed_receipts(self) -> None:
        for attempt in self.rows("attempts"):
            self.receipts.setdefault(attempt["id"], []).append(
                {
                    "attempt": attempt["id"],
                    "seq": 0,
                    "reliability": RELIABILITY["source"],
                    "source": "snapshot",
                    "fact": copy.deepcopy(attempt),
                }
            )

    # ---------- 写入 / 交换 ----------
    def add(self, group: str, row: dict, *, replace: bool = False) -> dict:
        """登记一条新记录；同 id 时默认报错，replace=True 才整体替换。"""
        rows = self.packet.setdefault(group, [])
        existing = self.find(group, row.get("id"))
        if existing is not None:
            if not replace:
                raise KeyError(f"{group} 中已存在 id={row.get('id')!r}")
            rows[rows.index(existing)] = copy.deepcopy(row)
        else:
            rows.append(copy.deepcopy(row))
        self._rebuild_indexes()
        return row

    def apply_receipt(
        self,
        attempt_id: str,
        changes: Dict[str, Any],
        *,
        reliability: Any = "instrument",
        source: Optional[str] = None,
        arrived_at: Optional[str] = None,
    ) -> dict:
        """把同一试验编号的新回报作为*新版本*登记并合并当前事实。

        - 原回执永久保留在 ``repo.receipts[attempt_id]``；
        - ``changes`` 只包含本次回报更正的字段（如 ``used``、``evidence_at``）；
        - 可靠性更高或同档后到的字段才会更正当前 attempt；
        - 迟到的 ``used`` 归到 **原次试验编号**，因此不会落到重测记录上。
        """
        attempt = self.find("attempts", attempt_id)
        if attempt is None:
            raise KeyError(f"未知试验编号 {attempt_id!r}")
        rank = RELIABILITY.get(reliability, reliability if isinstance(reliability, int) else 2)
        versions = self.receipts.setdefault(attempt_id, [])
        seq = len(versions)
        record = {
            "attempt": attempt_id,
            "seq": seq,
            "reliability": rank,
            "source": source or str(reliability),
            "arrived_at": arrived_at,
            "changes": copy.deepcopy(changes),
        }
        versions.append(record)

        current_rank = attempt.get("_reliability", RELIABILITY["source"])
        for key, value in changes.items():
            if key == "id":
                continue
            if rank >= current_rank or key not in attempt:
                attempt[key] = value
        attempt["_reliability"] = max(current_rank, rank)
        return attempt

    def add_custody(self, row: dict) -> dict:
        return self.add("custody", row)

    def custody_for(self, attempt_id: str) -> List[dict]:
        return [q for q in self.rows("custody") if q.get("attempt") == attempt_id]

    def attempts_for_order(self, order_id: str) -> List[dict]:
        return [a for a in self.rows("attempts") if a.get("order") == order_id]

    def reports_for_order(self, order_id: str) -> List[dict]:
        return [r for r in self.rows("reports") if r.get("order") == order_id]

    def method(self, method_id: str) -> dict:
        return self.by_id("methods", method_id)

    def to_dict(self) -> Dict[str, List[dict]]:
        return copy.deepcopy(self.packet)


def load_packet(source: "str | Path | dict") -> Repository:
    if isinstance(source, dict):
        return Repository(source)
    path = Path(source)
    data = json.loads(path.read_text(encoding="utf-8"))
    return Repository(data)
