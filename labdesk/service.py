"""实验室联调服务：把报文仓储、库存、受理与报告查询组合成一个有状态门面。

命令式方法对应《样品接收与仪器回报》的持续交接：

``receive_batch`` / ``split_sample``
    接收批次、分取子样（总量守恒、归属可追溯）。

``accept_order``
    受理委托：核算材料/留样/窗口，并把承诺的复测留样原子占用；共享实物
    不足时整体不受理，不会出现“双方都以为占用成功”。

``hand_to_instrument`` / ``instrument_receipt`` / ``return_from_instrument``
    上机托管、仪器回执（同编号可发更可靠更正，原回执保留）、实物交回。

``correct_calibration`` / ``issue_report``
    登记校准更正、签发报告（重测使用新试验编号）。

所有查询都返回可直接 JSON 序列化的 dict/list。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .acceptance import plan_order, reconcile_retentions, solve_units
from .calibration import calibration_impacts
from .inventory import ClaimError, Inventory
from .reports import report_status, sender_overview, sender_board, order_progress
from .repository import Repository, load_packet


class LabService:
    def __init__(self, packet: Optional[Union[dict, str, Path, Repository]] = None):
        if packet is None:
            self.repo = Repository()
        elif isinstance(packet, Repository):
            self.repo = packet
        else:
            self.repo = load_packet(packet)  # type: ignore[arg-type]
        self.inventory = Inventory(self.repo)
        self._accepted: Dict[str, dict] = {}

    # ---------- 报文/批次 ----------
    def receive_batch(self, rows: List[dict]) -> List[dict]:
        out = []
        for row in rows:
            out.append(self.repo.add("subsamples", dict(row)))
        return out

    def split_sample(self, parent_id: str, child_id: str, units: int, **kw) -> dict:
        return self.inventory.split(parent_id, child_id, units, **kw)

    def load_message(self, message: dict) -> dict:
        """接收一条交换报文。

        支持形如 ``{"group": "attempts", "row": {...}}`` 的普通登记，以及
        ``{"receipt": "T1", "changes": {...}, "reliability": "correction"}``
        的同试验编号更正回执（原回执保留）。
        """
        if "receipt" in message:
            attempt_id = message["receipt"]
            attempt = self.repo.apply_receipt(
                attempt_id,
                message.get("changes") or {},
                reliability=message.get("reliability", "instrument"),
                source=message.get("source"),
                arrived_at=message.get("arrived_at"),
            )
            return {"ok": True, "receipt": attempt_id, "current": attempt}
        group = message["group"]
        row = self.repo.add(group, message["row"])
        return {"ok": True, "group": group, "row": row}

    # ---------- 受理 ----------
    def preview_order(self, order: Union[str, dict], *, at: Optional[str] = None, **kw) -> dict:
        return plan_order(self.repo, self.inventory, order, at=at, **kw)

    def accept_order(
        self, order: Union[str, dict], *, at: Optional[str] = None, reserve_tests: bool = False
    ) -> dict:
        """受理委托并原子落下沉没的留样承诺。

        先做完整可行性规划；留样占用逐份在同一把库存锁内校验。任何一步
        失败都会释放本次已建占用并抛出 :class:`ClaimError`。
        """
        row = order if isinstance(order, dict) else self.repo.by_id("orders", order)
        oid = row["id"]
        if not self.repo.has("orders", oid):
            self.repo.add("orders", dict(row))
        plan = plan_order(self.repo, self.inventory, row, at=at)
        if plan["status"] != "schedulable":
            raise ClaimError(
                f"委托 {oid} 暂不具备受理条件：{plan['status']}",
                code=plan["status"], details=plan,
            )
        created: List[str] = []
        try:
            retained = int(row.get("retained_units") or 0)
            if retained:
                # 规划分配是“总需求”，这里只锁留样：按归属亲和求 retained 的解
                from .acceptance import _order_used_samples

                affinity = {s: i for i, s in enumerate(_order_used_samples(self.repo, oid))}
                ret_alloc = solve_units(
                    self.inventory, list(row.get("allowed_samples") or []), retained,
                    affinity=affinity,
                )
                if ret_alloc is None:
                    raise ClaimError("留样在受理瞬间被其他委托占用", code="retention_lost")
                for sid, n in ret_alloc.items():
                    rid = f"RET-{oid}-{sid}"
                    self.inventory.occupy(
                        sid, n, order=oid, kind="retention", reservation_id=rid,
                        note="承诺客户的复测留样",
                    )
                    created.append(rid)
            if reserve_tests:
                # 留样已单独锁定；这里只锁其余试验周转/耗用份数，避免重复占用
                book_units = max(0, int(plan["material"]["required_units"]) - retained)
                if book_units:
                    book_alloc = solve_units(
                        self.inventory, list(row.get("allowed_samples") or []), book_units)
                    if book_alloc is None:
                        raise ClaimError("试验份数在受理瞬间不足", code="booking_lost")
                    for sid, n in book_alloc.items():
                        rid = f"BOOK-{oid}-{sid}"
                        self.inventory.occupy(
                            sid, n, order=oid, kind="booking", reservation_id=rid)
                        created.append(rid)
        except ClaimError:
            for rid in created:
                self.inventory.release(rid)
            raise
        result = {"accepted": oid, "plan": plan, "reservations": created}
        self._accepted[oid] = result
        return result

    def reserve_all_retentions(self, create: bool = True) -> List[dict]:
        return reconcile_retentions(self.repo, self.inventory, create=create)

    # ---------- 试验/仪器交接 ----------
    def hand_to_instrument(
        self, attempt_id: str, *, custody_id: Optional[str] = None, at: Optional[str] = None
    ) -> dict:
        a = self.repo.by_id("attempts", attempt_id)
        cid = custody_id or f"Q-{attempt_id}"
        if not self.repo.has("custody", cid):
            self.repo.add("custody", {
                "id": cid, "attempt": attempt_id, "sample": a.get("sample"),
                "handed": at or a.get("start"),
                "received_from_instrument": None, "reported": None,
            })
        return self.repo.by_id("custody", cid)

    def instrument_receipt(self, attempt_id: str, changes: dict, **kw) -> dict:
        return self.repo.apply_receipt(attempt_id, changes, **kw)

    def return_from_instrument(self, custody_id: str, at: str) -> dict:
        q = self.repo.by_id("custody", custody_id)
        q["received_from_instrument"] = at
        return q

    def start_retest(
        self,
        original_attempt: str,
        new_attempt_id: str,
        *,
        instrument: str,
        start: str,
        sample: Optional[str] = None,
        evidence_at: Optional[str] = None,
        **extra,
    ) -> dict:
        """登记重测：必须使用新试验编号；实物占用仍走同一库存校验。"""
        old = self.repo.by_id("attempts", original_attempt)
        sid = sample or old.get("sample")
        method = extra.pop("method", old.get("method"))
        order_id = old.get("order")
        m = self.repo.find("methods", method) or {}
        q = int(m.get("units_per_attempt", 1))
        # 上机前确认有可排实物；非破坏试验只做短期 booking，回执后释放。
        res = self.inventory.occupy(
            sid, q, order=order_id, kind="booking",
            reservation_id=f"TRY-{new_attempt_id}", holder=instrument,
            note=f"重测 {new_attempt_id}（原 {original_attempt}）",
        )
        row = {
            "id": new_attempt_id, "order": order_id, "instrument": instrument,
            "method": method, "sample": sid, "start": start,
            "evidence_at": evidence_at, "received": extra.pop("received", None),
            "used": extra.pop("used", None), "retest_of": original_attempt, **extra,
        }
        self.repo.add("attempts", row)
        self.hand_to_instrument(new_attempt_id, at=start)
        return {"attempt": row, "reservation": res.to_dict()}

    # ---------- 校准/报告 ----------
    def correct_calibration(self, row: dict) -> dict:
        return self.repo.add("calibrations", dict(row))

    def issue_report(self, report_id: str, order_id: str, attempt_ids: List[str], issued: str) -> dict:
        row = {"id": report_id, "order": order_id, "issued": issued, "attempts": list(attempt_ids)}
        return self.repo.add("reports", row)

    # ---------- 查询 ----------
    def calibration_impacts(self) -> List[dict]:
        return calibration_impacts(self.repo)

    def arrange_for_calibration(self, calibration_id: str, *, at: Optional[str] = None) -> dict:
        """从一条校准更正找到真正受影响的委托，并按*当前实际留样*给出补测安排。"""
        from .calibration import calibration_hits, invalid_attempt_ids
        from .reports import classify_retest_action

        cal = self.repo.by_id("calibrations", calibration_id)
        invalid_map = invalid_attempt_ids(self.repo)
        arrangements = []
        for attempt in calibration_hits(self.repo, cal):
            order = self.repo.find("orders", attempt.get("order")) or {"id": attempt.get("order")}
            action = classify_retest_action(
                self.repo, self.inventory, order, attempt, invalid_map, at=at)
            arrangements.append({
                "attempt": attempt["id"],
                "order": attempt.get("order"),
                "method": attempt.get("method"),
                "original_sample": attempt.get("sample"),
                "arrangement": action,
            })
        return {"calibration": calibration_id, "at": at, "arrangements": arrangements}

    def report(self, report_id: str, *, at: Optional[str] = None) -> dict:
        return report_status(self.repo, self.inventory, report_id, at=at)

    def order(self, order_id: str, *, at: Optional[str] = None) -> dict:
        return order_progress(self.repo, self.inventory, order_id, at=at)

    def overview(self, *, at: Optional[str] = None) -> List[dict]:
        return sender_overview(self.repo, self.inventory, at=at)

    def board(self, *, at: Optional[str] = None) -> dict:
        return sender_board(self.repo, self.inventory, at=at)

    def quantities(self) -> Dict[str, dict]:
        return self.inventory.quantities()

    def state(self, *, at: Optional[str] = None) -> dict:
        """完整可序列化状态：数量、校准影响、委托总览。"""
        return {
            "quantities": self.quantities(),
            "calibration_impacts": self.calibration_impacts(),
            "orders": self.overview(at=at),
        }

    def save_state(self, path: Union[str, Path], *, at: Optional[str] = None) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.state(at=at), ensure_ascii=False, indent=2), encoding="utf-8")
        return p
