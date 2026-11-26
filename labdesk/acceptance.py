"""委托受理：材料需求、替代样、留样承诺、共享实物互斥与仪器窗口。"""
from __future__ import annotations

from typing import Dict, List, Optional, Union

from .inventory import Inventory
from .repository import Repository
from .timeutil import as_minutes, HM


class PlanStatus:
    SCHEDULABLE = "schedulable"                 # 可以安排
    NEED_SUPPLEMENT = "need_supplement"         # 缺合格留样/实物，需补件
    NO_INSTRUMENT_WINDOW = "no_instrument_window"
    AWAITING_INSTRUMENT = "awaiting_instrument"  # 在等仪器回报/交回
    ACCEPTED = "accepted"


def material_requirement(repo: Repository, order: dict) -> dict:
    """计算委托的最少实物单位需求。

    破坏试验每份需求都会终结为耗用；``retained_units`` 是事后必须留下的
    复测份额。非破坏试验只是临时持有、用完交回，可以搭在“将被耗用”或
    “将留作留样”的同一单位上顺序进行（数据中 T2 扫描后同一 A2 单位上 T1
    弯曲即为例证）。因此最少 distinct 单位为
    ``max(consumptive + retained, max_nonconsumptive_q)``。
    """
    consumptive = 0
    nonconsumptive_q: List[int] = []
    method_rows = []
    for mid in order.get("methods") or []:
        m = repo.find("methods", mid) or {"id": mid, "consumes": False, "units_per_attempt": 1}
        q = int(m.get("units_per_attempt", 1))
        if m.get("consumes"):
            consumptive += q
        else:
            nonconsumptive_q.append(q)
        method_rows.append({"method": mid, "consumes": bool(m.get("consumes")), "units": q})
    retained = int(order.get("retained_units") or 0)
    temp_peak = max(nonconsumptive_q, default=0)
    terminal = consumptive + retained
    required = max(terminal, temp_peak)
    return {
        "methods": method_rows,
        "consumptive_units": consumptive,
        "retained_units": retained,
        "temporary_peak_units": temp_peak,
        "terminal_units": terminal,
        "required_units": required,
    }


def check_applicability(repo: Repository, attempt: dict, order: Optional[dict] = None) -> dict:
    """判断一次试验所用样品与仪器对该委托是否适用。

    - 样品必须在委托的 ``allowed_samples``（含替代样）内；
    - 仪器必须登记为该方法的合格仪器，且读数时刻落在其资格/有效窗口内。
    校准更正造成的“读数失效”是另一维度，见 calibration 模块。
    """
    oid = attempt.get("order")
    order = order or (repo.find("orders", oid) if oid else None)
    sample_id = attempt.get("sample")
    sample_allowed = True
    if order is not None and order.get("allowed_samples") is not None:
        sample_allowed = sample_id in (order.get("allowed_samples") or [])

    ins = repo.find("instruments", attempt.get("instrument"))
    instrument_registered = ins is not None
    method_matches = bool(ins and ins.get("method") == attempt.get("method"))
    ev = as_minutes(attempt.get("evidence_at"))
    within_window = True
    if ins and ev is not None:
        within_window = as_minutes(ins.get("begin")) <= ev < as_minutes(ins.get("end"))
    qualified = bool(ins and ins.get("qualification"))
    applicable = sample_allowed and instrument_registered and method_matches and within_window
    return {
        "applicable": applicable,
        "sample_allowed": sample_allowed,
        "instrument_registered": instrument_registered,
        "method_matches": method_matches,
        "qualified_code": ins.get("qualification") if ins else None,
        "within_qualification_window": within_window,
    }


def candidate_windows(
    repo: Repository,
    method_id: str,
    *,
    at_minute: Optional[int] = None,
    deadline_minute: Optional[int] = None,
    slot_minutes: int = 0,
) -> List[dict]:
    """列出某方法当前仍可赶上的合格仪器窗口。"""
    out = []
    for ins in repo.rows("instruments"):
        if ins.get("method") != method_id:
            continue
        begin = as_minutes(ins.get("begin")) or 0
        end = as_minutes(ins.get("end")) or 24 * 60
        horizon = end if deadline_minute is None else min(end, deadline_minute)
        now = begin if at_minute is None else max(as_minutes(at_minute), begin)
        earliest_finish = now + slot_minutes
        feasible = earliest_finish <= horizon and now <= end
        out.append({
            "instrument": ins["id"],
            "qualification": ins.get("qualification"),
            "begin": ins.get("begin"),
            "end": ins.get("end"),
            "deadline": HM(deadline_minute),
            "earliest_start": HM(now),
            "latest_start": HM(horizon - slot_minutes),
            "feasible": feasible,
            "reason": None if feasible else (
                "已过仪器有效窗口" if (at_minute is not None and as_minutes(at_minute) > end)
                else "无法在委托时刻前完成"
            ),
        })
    return out


def _sample_capacity(inv: Inventory, sample_id: str, at_minute: Optional[int] = None) -> int:
    view = inv.sample_view(sample_id, at_minute=at_minute)
    return max(0, view.schedulable)


def solve_units(
    inv: Inventory,
    allowed_samples: List[str],
    need: int,
    *,
    affinity: Optional[Dict[str, int]] = None,
    at_minute: Optional[int] = None,
) -> Optional[Dict[str, int]]:
    """在允许的子样间求一个整数分配；不足返回 None。

    ``affinity`` 给每个子样一个优先级（越小越优先），用于优先使用委托已经
    动过/独有的实物，使结论稳定且符合归属。
    """
    caps = {s: _sample_capacity(inv, s, at_minute) for s in allowed_samples
            if inv.repo.has("subsamples", s)}
    if sum(caps.values()) < need:
        return None
    order = sorted(allowed_samples, key=lambda s: ((affinity or {}).get(s, 999), s))
    alloc = {s: 0 for s in order}
    remain = need
    for s in order:
        take = min(caps[s], remain)
        alloc[s] = take
        remain -= take
        if remain == 0:
            break
    return {s: n for s, n in alloc.items() if n}


def plan_order(
    repo: Repository,
    inv: Inventory,
    order: Union[str, dict],
    *,
    at: Optional[str] = None,
    extra_consumptive_units: int = 0,
    retain_override: Optional[int] = None,
    slot_minutes: int = 0,
) -> dict:
    """评估一份委托（已登记 id 或草稿 dict）现在能否接/安排。

    ``extra_consumptive_units`` 用于“在校准更正后追加一次重测”的场合：
    首轮耗用已是历史事实，这里只追加尚未发生的耗用与仍须保住的留样。
    """
    if isinstance(order, str):
        order_row = repo.by_id("orders", order)
    else:
        order_row = order
    oid = order_row.get("id")
    at_minute = as_minutes(at)
    needed_by = as_minutes(order_row.get("needed_by"))
    req = material_requirement(repo, order_row)
    retained = req["retained_units"] if retain_override is None else retain_override

    # 新委托的完整需求；追加复测时，调用方通过 extra_consumptive_units 表达
    # 额外耗用，同时仍要求 retained 份留样在终态保留。
    if extra_consumptive_units:
        need = extra_consumptive_units + retained
    else:
        need = req["required_units"]

    allowed = list(order_row.get("allowed_samples") or [])
    affinity = {s: i for i, s in enumerate(_order_used_samples(repo, oid))}
    alloc = solve_units(inv, allowed, need, affinity=affinity, at_minute=at_minute)

    windows: Dict[str, List[dict]] = {}
    window_ok = True
    for mid in order_row.get("methods") or []:
        wins = candidate_windows(
            repo, mid, at_minute=at_minute, deadline_minute=needed_by,
            slot_minutes=slot_minutes,
        )
        windows[mid] = wins
        # 追加复测只关心被追加的方法；这里全量给出，可行性按“至少一个窗口”
        if not any(w["feasible"] for w in wins):
            window_ok = False

    gap = 0 if alloc is not None else max(
        0, need - sum(_sample_capacity(inv, s, at_minute) for s in allowed))
    blockers = _occupation_blockers(inv, allowed, need, at_minute)

    if alloc is None:
        status = PlanStatus.NEED_SUPPLEMENT
    elif not window_ok:
        status = PlanStatus.NO_INSTRUMENT_WINDOW
    else:
        status = PlanStatus.SCHEDULABLE

    return {
        "order": oid,
        "at": at,
        "needed_by": order_row.get("needed_by"),
        "allowed_samples": allowed,
        "material": req,
        "planning_need_units": need,
        "allocation": alloc,
        "available_in_allowed": {s: _sample_capacity(inv, s, at_minute) for s in allowed},
        "supplement_units": gap,
        "blockers": blockers,
        "windows": windows,
        "status": status,
        "label": STATUS_LABELS[status],
    }


STATUS_LABELS = {
    PlanStatus.SCHEDULABLE: "可以安排",
    PlanStatus.NEED_SUPPLEMENT: "缺合格留样，需补件",
    PlanStatus.NO_INSTRUMENT_WINDOW: "赶不上合格仪器窗口",
    PlanStatus.AWAITING_INSTRUMENT: "在等仪器回报/交回",
    PlanStatus.ACCEPTED: "已受理",
}


def retest_allocation(
    inv: Inventory,
    order: dict,
    *,
    consumptive_units: int,
    retained_units: int,
    at_minute: Optional[int] = None,
) -> Optional[dict]:
    """把“留样承诺”和“追加耗用”分开落位的复测分配。

    先按归属亲和保住该委托的留样，再从剩余可排量中安排重测耗用，避免把
    已答应客户的留样又排进试验。若服务里已存在该委托的 retention 占用，
    会先抵扣，不重复计算。返回带标签的分配；不足返回 ``None`` 并在
    ``reason`` 中说明卡在留样还是耗用。
    """
    oid = order.get("id")
    allowed = list(order.get("allowed_samples") or [])
    affinity = {s: i for i, s in enumerate(_order_used_samples(inv.repo, oid))}
    order_samples = sorted(allowed, key=lambda s: ((affinity or {}).get(s, 999), s))

    already_retention = sum(
        r.units for r in inv.reservations.values()
        if r.active and r.kind == "retention" and r.order == oid
    )
    need_retention = max(0, int(retained_units) - already_retention)

    caps = {s: max(0, inv.sample_view(s, at_minute=at_minute).schedulable)
            for s in order_samples if inv.repo.has("subsamples", s)}
    retention_alloc: Dict[str, int] = {}
    for s in order_samples:  # 留样优先落位
        if need_retention == 0:
            break
        take = min(caps[s], need_retention)
        if take:
            retention_alloc[s] = take
            caps[s] -= take
            need_retention -= take
    existing_alloc: Dict[str, int] = {}
    for r in inv.reservations.values():
        if r.active and r.kind == "retention" and r.order == oid:
            existing_alloc[r.sample] = existing_alloc.get(r.sample, 0) + r.units
    if need_retention > 0:
        return {"ok": False, "reason": "retention_short",
                "retention_allocation": {**existing_alloc, **retention_alloc},
                "consumptive_allocation": {}, "remaining_capacity": caps,
                "short_retention": need_retention}

    need_consume = int(consumptive_units)
    consume_alloc: Dict[str, int] = {}
    # 重测耗用优先使用“非留样亲和”的剩余份：直接按剩余容量顺序分配
    for s in order_samples:
        if need_consume == 0:
            break
        take = min(caps[s], need_consume)
        if take:
            consume_alloc[s] = take
            caps[s] -= take
            need_consume -= take
    if need_consume > 0:
        return {"ok": False, "reason": "consumptive_short",
                "retention_allocation": {**existing_alloc, **retention_alloc},
                "consumptive_allocation": consume_alloc,
                "remaining_capacity": caps, "short_consumptive": need_consume}

    merged: Dict[str, int] = {}
    for part in (existing_alloc, retention_alloc, consume_alloc):
        for s, n in part.items():
            merged[s] = merged.get(s, 0) + n
    return {
        "ok": True,
        "retention_allocation": {**existing_alloc, **retention_alloc},
        "new_retention_allocation": retention_alloc,
        "consumptive_allocation": consume_alloc,
        "allocation": {s: n for s, n in merged.items() if n},
    }


def _order_used_samples(repo: Repository, order_id: Optional[str]) -> List[str]:
    if not order_id:
        return []
    seen = []
    for a in repo.attempts_for_order(order_id):
        s = a.get("sample")
        if s and s not in seen:
            seen.append(s)
    return seen


def _occupation_blockers(inv: Inventory, allowed: List[str], need: int,
                         at_minute: Optional[int] = None) -> List[dict]:
    """说明需求无法满足时，是哪些其他委托的有效占用挡在前面。"""
    blockers = []
    for s in allowed:
        view = inv.sample_view(s, at_minute=at_minute)
        holders = inv._active_holders(s)  # noqa: SLF001 - 同域协作
        if holders or view.consumed or view.in_instrument or view.unconfirmed:
            blockers.append({
                "sample": s,
                "schedulable": view.schedulable,
                "consumed": view.consumed,
                "in_instrument": view.in_instrument,
                "unconfirmed": view.unconfirmed,
                "holders": holders,
            })
    return blockers


def reconcile_retentions(repo: Repository, inv: Inventory, *, create: bool = False) -> List[dict]:
    """为所有已登记委托求解互不冲突的留样归属。

    优先使用各委托已经实际动用过的子样（归属连续），并按委托 id 稳定排序。
    ``create=True`` 时在库存中建立原子 retention 占用；共享子样只剩一份时
    只有一个委托能拿到，另一个返回 ``need_supplement``，系统不会让双方都
    以为留样已占用。
    """
    results = []
    for order in sorted(repo.rows("orders"), key=lambda o: o["id"]):
        r = int(order.get("retained_units") or 0)
        if r == 0:
            results.append({"order": order["id"], "retained_units": 0,
                            "assignment": {}, "status": PlanStatus.SCHEDULABLE})
            continue
        allowed = list(order.get("allowed_samples") or [])
        affinity = {s: i for i, s in enumerate(_order_used_samples(repo, order["id"]))}
        alloc = solve_units(inv, allowed, r, affinity=affinity)
        if alloc is None:
            cap = sum(max(0, inv.sample_view(s).schedulable) for s in allowed)
            results.append({
                "order": order["id"], "retained_units": r, "assignment": None,
                "status": PlanStatus.NEED_SUPPLEMENT,
                "supplement_units": r - cap,
                "available": {s: inv.sample_view(s).schedulable for s in allowed},
            })
            continue
        rid = f"RET-{order['id']}"
        if create:
            for sample_id, units in alloc.items():
                token = f"{rid}-{sample_id}"
                if token in inv.reservations and inv.reservations[token].active:
                    continue
                inv.occupy(sample_id, units, order=order["id"], kind="retention",
                           reservation_id=token, note="承诺客户的复测留样")
        results.append({"order": order["id"], "retained_units": r,
                        "assignment": alloc, "status": PlanStatus.SCHEDULABLE})
    return results
