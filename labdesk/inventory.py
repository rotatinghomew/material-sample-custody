"""实物库存、分取关系与占用口径。

数量分层（每份单位是可单独交接的最小单位）：

``received``
    该子样登记的 ``units``。父子之间不重复计库存：原样 A1=6 且已分出
    A2/A3/A4 各 2 时，A1 本级可动数量为 0，全批实物仍为 6。

``consumed``
    已有仪器回执 ``used`` 证实耗用的份数。``used`` 缺省表示损毁未确认，
    不计入已耗用，但会整份进入 ``unconfirmed`` 风险占用。

``book_available``
    账面可动用 = 本级基数 - 已证实耗用。交回时刻为 ``null`` 不会恢复它，
    而非破坏试验已 ``used=0`` 但实物未交回时，要在 ``in_instrument`` 中占用。

``schedulable``
    现在还能排新试验/新承诺的数量 = 账面可用 - 仪器内未交回的未耗用部分
    - 损毁未确认的风险份数 - 其他有效保留/占用承诺。

多份委托共同获准使用同一子样时，:meth:`Inventory.occupy` 在同一把锁内做
“先查后占”，只有一方能拿到指定份数，避免双方同时以为已占用。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .repository import Repository
from .timeutil import as_minutes


class ClaimError(RuntimeError):
    """占用/分取不满足可用量，或实物已被其他委托占用。"""

    def __init__(self, message: str, *, code: str = "unavailable", details: Any = None):
        super().__init__(message)
        self.code = code
        self.details = details


@dataclass
class Reservation:
    id: str
    sample: str
    units: int
    kind: str  # retention | booking | hold
    order: Optional[str] = None
    note: Optional[str] = None
    active: bool = True
    serials: List[int] = field(default_factory=list)
    holder: Optional[str] = None  # 实际持有方（仪器/委托）

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sample": self.sample,
            "units": self.units,
            "kind": self.kind,
            "order": self.order,
            "note": self.note,
            "active": self.active,
            "serials": self.serials,
            "holder": self.holder,
        }


@dataclass
class SampleView:
    id: str
    lot: Optional[str]
    parent: Optional[str]
    form: Optional[str]
    supplier: Optional[str]
    received: int
    child_units: int
    base_units: int
    consumed: int
    unconfirmed: int
    in_instrument: int
    reserved: int
    book_available: int
    schedulable: int
    overcommitted: bool
    open_attempts: List[str]
    serial_states: Dict[str, str]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "lot": self.lot,
            "parent": self.parent,
            "form": self.form,
            "supplier": self.supplier,
            "received": self.received,
            "child_units": self.child_units,
            "base_units": self.base_units,
            "consumed": self.consumed,
            "unconfirmed": self.unconfirmed,
            "in_instrument": self.in_instrument,
            "reserved": self.reserved,
            "book_available": self.book_available,
            "schedulable": self.schedulable,
            "overcommitted": self.overcommitted,
            "open_attempts": self.open_attempts,
            "serial_states": self.serial_states,
        }


class Inventory:
    def __init__(self, repo: Repository):
        self.repo = repo
        self.lock = threading.RLock()
        self.reservations: Dict[str, Reservation] = {}
        self._seq = 0

    # ---------- 树与总量 ----------
    def children(self, sample_id: str) -> List[dict]:
        return [s for s in self.repo.rows("subsamples") if s.get("parent") == sample_id]

    def parent_chain(self, sample_id: str) -> List[dict]:
        chain: List[dict] = []
        cur = self.repo.find("subsamples", sample_id)
        seen = set()
        while cur and cur["id"] not in seen:
            seen.add(cur["id"])
            chain.append(cur)
            cur = self.repo.find("subsamples", cur.get("parent")) if cur.get("parent") else None
        return chain

    def trace(self, sample_id: str) -> dict:
        """返回从子样到原样的归属链，拆分/换机后仍可追溯批次与供应商。"""
        chain = self.parent_chain(sample_id)
        node = self.repo.find("subsamples", sample_id)
        return {
            "sample": sample_id,
            "lot": node.get("lot") if node else None,
            "supplier": node.get("supplier") if node else None,
            "ancestry": [
                {"id": s["id"], "parent": s.get("parent"), "lot": s.get("lot"),
                 "supplier": s.get("supplier"), "units": s.get("units"), "form": s.get("form")}
                for s in chain
            ],
        }

    def lot_physical_units(self, lot: str) -> int:
        """批次实物总数：只累加根原样，避免父子重复计数。"""
        return sum(
            int(s.get("units", 0))
            for s in self.repo.rows("subsamples")
            if s.get("lot") == lot and not s.get("parent")
        )

    def base_units(self, sample_id: str) -> int:
        """该标识下本级可支配基数：登记数减去已命名分出的子样数。"""
        s = self.repo.by_id("subsamples", sample_id)
        child_sum = sum(int(c.get("units", 0)) for c in self.children(sample_id))
        return int(s.get("units", 0)) - child_sum

    # ---------- 单次试验的占用口径 ----------
    def _requirement(self, attempt: dict) -> int:
        method = self.repo.find("methods", attempt.get("method"))
        return int(method.get("units_per_attempt", 1)) if method else 1

    def _custody_open(self, attempt_id: str) -> bool:
        rows = self.repo.custody_for(attempt_id)
        if not rows:
            return False
        # 以最后一条交接记载为准
        last = rows[-1]
        return bool(last.get("handed")) and last.get("received_from_instrument") is None

    def attempt_deductions(self, attempt: dict) -> Dict[str, int]:
        """返回某次试验对账面耗用与可排占用的贡献。"""
        q = self._requirement(attempt)
        used = attempt.get("used", None)
        open_custody = self._custody_open(attempt["id"])
        if used is None:
            # 损毁未确认：整份需求都不能再排；不进已证实耗用
            return {"consumed": 0, "hold": q, "unconfirmed": q, "in_instrument": 0}
        used = int(used)
        residual_hold = max(0, q - used) if open_custody else 0
        return {
            "consumed": used,
            "hold": residual_hold,
            "unconfirmed": 0,
            "in_instrument": residual_hold,
        }

    def sample_view(self, sample_id: str, *, at_minute: Optional[int] = None) -> SampleView:
        """计算一个子样的全部分层数量。

        ``at_minute`` 给定时，只统计在该时刻已经发生（start/handed 已到）
        的试验与交接，用于回放“当时能排多少”。
        """
        s = self.repo.by_id("subsamples", sample_id)
        base = self.base_units(sample_id)
        consumed = 0
        unconfirmed = 0
        in_instrument = 0
        open_attempts: List[str] = []
        serial_states: Dict[str, str] = {}

        attempts = [a for a in self.repo.rows("attempts") if a.get("sample") == sample_id]
        if at_minute is not None:
            def started(a):
                st = as_minutes(a.get("start")) or as_minutes(a.get("received"))
                return st is not None and st <= at_minute
            attempts = [a for a in attempts if started(a)]
        # 按上机时间排序，顺序分配虚拟序号，便于追溯每份单位状态
        attempts.sort(key=lambda a: as_minutes(a.get("start")) or 0)
        cursor = 1
        for a in attempts:
            d = self.attempt_deductions(a)
            q = self._requirement(a)
            custody_open = self._custody_open(a["id"])
            consumed += d["consumed"]
            unconfirmed += d["unconfirmed"]
            in_instrument += d["in_instrument"]
            if d["hold"] > 0 or d["unconfirmed"] or custody_open:
                open_attempts.append(a["id"])
            for i in range(q):
                serial = cursor + i
                if i < d["consumed"]:
                    state = "consumed"
                elif a.get("used") is None:
                    state = "unconfirmed"
                elif self._custody_open(a["id"]):
                    state = "in_instrument"
                else:
                    state = "used_returned"
                serial_states[f"{sample_id}#{serial}"] = state
            cursor += q

        reserved = sum(
            r.units for r in self.reservations.values()
            if r.active and r.sample == sample_id and r.kind in ("retention", "booking", "hold")
        )
        book_available = base - consumed
        schedulable = book_available - unconfirmed - in_instrument - reserved
        overcommitted = schedulable < 0 or book_available < 0
        return SampleView(
            id=sample_id,
            lot=s.get("lot"),
            parent=s.get("parent"),
            form=s.get("form"),
            supplier=s.get("supplier"),
            received=int(s.get("units", 0)),
            child_units=sum(int(c.get("units", 0)) for c in self.children(sample_id)),
            base_units=base,
            consumed=consumed,
            unconfirmed=unconfirmed,
            in_instrument=in_instrument,
            reserved=reserved,
            book_available=book_available,
            schedulable=schedulable,
            overcommitted=overcommitted,
            open_attempts=open_attempts,
            serial_states=serial_states,
        )

    def views(self) -> List[SampleView]:
        return [self.sample_view(s["id"]) for s in self.repo.rows("subsamples")]

    def quantities(self) -> Dict[str, dict]:
        return {v.id: v.to_dict() for v in self.views()}

    # ---------- 原子占用 ----------
    def _new_reservation_id(self) -> str:
        self._seq += 1
        return f"R{self._seq:03d}"

    def occupy(
        self,
        sample_id: str,
        units: int,
        *,
        order: Optional[str] = None,
        kind: str = "booking",
        reservation_id: Optional[str] = None,
        note: Optional[str] = None,
        holder: Optional[str] = None,
    ) -> Reservation:
        """原子占用 ``units`` 份可排实物。

        容量判断与写入在同一把可重入锁内完成；容量不足时抛
        :class:`ClaimError`，``details`` 说明被谁占用，调用方不得把其他委托
        的有效占用强行腾挪给等待复测。
        """
        if units <= 0:
            raise ClaimError("占用份数必须为正整数", code="bad_units")
        with self.lock:
            view = self.sample_view(sample_id)
            if units > view.schedulable:
                holders = self._active_holders(sample_id)
                raise ClaimError(
                    f"{sample_id} 可排 {view.schedulable} 份，无法再占用 {units} 份",
                    code="insufficient_schedulable",
                    details={
                        "sample": sample_id,
                        "requested": units,
                        "schedulable": view.schedulable,
                        "book_available": view.book_available,
                        "consumed": view.consumed,
                        "unconfirmed": view.unconfirmed,
                        "in_instrument": view.in_instrument,
                        "active_holders": holders,
                    },
                )
            rid = reservation_id or self._new_reservation_id()
            if rid in self.reservations:
                raise ClaimError(f"占用编号 {rid!r} 已存在", code="duplicate_reservation")
            # 分配当前空闲的虚拟序号：避开事实占用与其他有效承诺
            fact_busy = {
                int(label.rsplit("#", 1)[1])
                for label, state in view.serial_states.items()
                if state in ("consumed", "unconfirmed", "in_instrument")
            }
            used_serials = set(fact_busy)
            for r in self.reservations.values():
                if r.active and r.sample == sample_id:
                    used_serials.update(r.serials)
            free_serials = [n for n in range(1, view.received + 1) if n not in used_serials]
            serials = free_serials[:units]
            rev = Reservation(
                id=rid, sample=sample_id, units=units, kind=kind, order=order,
                note=note, serials=serials, holder=holder or order,
            )
            self.reservations[rid] = rev
            return rev

    def _active_holders(self, sample_id: str) -> List[dict]:
        result = []
        for r in self.reservations.values():
            if r.active and r.sample == sample_id:
                result.append({"reservation": r.id, "order": r.order,
                               "kind": r.kind, "units": r.units, "holder": r.holder})
        return result

    def release(self, reservation_id: str) -> Reservation:
        with self.lock:
            r = self.reservations[reservation_id]
            r.active = False
            return r

    # ---------- 分取 ----------
    def split(
        self,
        parent_id: str,
        child_id: str,
        units: int,
        *,
        form: Optional[str] = None,
        migrate_reservations: Optional[List[str]] = None,
    ) -> dict:
        """从原样分取子样，保证总量守恒、归属可追溯、已承诺留样不消失。"""
        with self.lock:
            parent = self.repo.by_id("subsamples", parent_id)
            view = self.sample_view(parent_id)
            if units <= 0:
                raise ClaimError("分取份数必须为正", code="bad_units")
            if units > view.base_units:
                raise ClaimError(
                    f"{parent_id} 本级可分基数 {view.base_units}，不能分出 {units}",
                    code="exceeds_base",
                )
            # 已耗用/在仪器/未确认的部分不能借分取“复活”
            locked = view.consumed + view.unconfirmed + view.in_instrument
            free_physical = view.base_units - locked
            migrating = 0
            for rid in migrate_reservations or []:
                r = self.reservations[rid]
                if r.sample != parent_id or not r.active:
                    raise ClaimError(f"占用 {rid} 不能迁移", code="bad_migration")
                migrating += r.units
            if units > free_physical + migrating:
                raise ClaimError(
                    f"分出 {units} 会触及已用/在管实物；自由可分 {free_physical}，"
                    f"可随迁承诺 {migrating}",
                    code="encumbered_split",
                    details={"free_physical": free_physical, "migrating": migrating},
                )
            child = {
                "id": child_id,
                "lot": parent.get("lot"),
                "parent": parent_id,
                "units": units,
                "form": form or parent.get("form"),
                "supplier": parent.get("supplier"),
            }
            self.repo.add("subsamples", child)
            for rid in migrate_reservations or []:
                self.reservations[rid].sample = child_id
            return child
