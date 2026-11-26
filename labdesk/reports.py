"""报告当前业务效力、复测进度，以及送检方查询时的三类可办状态。

状态口径：

``schedulable``
    可以安排复测/试验：存在未被其他委托占用的合格实物，且有赶得上
    ``needed_by`` 的合格仪器窗口。

``need_supplement``
    缺合格留样：允许样中没有可排份数（被耗用、被其他委托有效占用或
    份数不足），必须补送料；返回 ``supplement_units``。

``awaiting_instrument``
    在等仪器回报/实物交回：试验已上机器但读数/上送回执未到，或非破坏
    实物尚未实际交回。此状态不会把别的委托已占用的实物腾给它。

另有 ``valid`` / ``retest_done_pending_report`` / ``ready_to_issue_report``
等进度态，用于完整表达“当前业务效力及复测进度”。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .acceptance import candidate_windows, solve_units, STATUS_LABELS, PlanStatus, check_applicability
from .calibration import invalid_attempt_ids
from .inventory import Inventory
from .repository import Repository
from .timeutil import as_minutes


def _visible_attempt(a: dict, at_minute: Optional[int]) -> bool:
    """回放时刻下该试验是否已经发生：以上机实际时间 start 为准。"""
    if at_minute is None:
        return True
    start = as_minutes(a.get("start"))
    if start is not None:
        return start <= at_minute
    return (as_minutes(a.get("evidence_at")) or 10**9) <= at_minute


def _retest_attempts(
    repo: Repository, order_id: str, method: str, original_id: str,
    *, at_minute: Optional[int] = None,
) -> List[dict]:
    """找出某失效项目的重测：优先显式 retest_of，否则按同委托同方法的新编号推断。"""
    explicit, inferred = [], []
    for a in repo.attempts_for_order(order_id):
        if a.get("method") != method or a["id"] == original_id:
            continue
        if not _visible_attempt(a, at_minute):
            continue
        if a.get("retest_of") == original_id or a.get("retests") == original_id:
            explicit.append(a)
        elif not a.get("retest_of") and not a.get("retests"):
            inferred.append(a)
    chosen = explicit or inferred
    chosen.sort(key=lambda a: as_minutes(a.get("evidence_at")) or as_minutes(a.get("start")) or 0)
    return chosen


def _report_citing(repo: Repository, attempt_id: str) -> List[str]:
    return [r["id"] for r in repo.rows("reports") if attempt_id in (r.get("attempts") or [])]


def attempt_receipt_state(repo: Repository, attempt: dict, *, at_minute: Optional[int]) -> dict:
    """判断一次试验是否还在等仪器数据回报或实物交回。"""
    ev_min = as_minutes(attempt.get("evidence_at"))
    has_evidence = ev_min is not None and (at_minute is None or ev_min <= at_minute)
    received_min = as_minutes(attempt.get("received"))
    data_arrived = received_min is not None and (at_minute is None or received_min <= at_minute)
    waiting_data = (not has_evidence) or (not data_arrived)

    open_custody = False
    custody_rows = repo.custody_for(attempt["id"])
    if custody_rows:
        last = custody_rows[-1]
        open_custody = bool(last.get("handed")) and last.get("received_from_instrument") is None
    # 已证实耗用且 q<=used 的部分不产生“未交回实物”占用
    method = repo.find("methods", attempt.get("method")) or {}
    q = int(method.get("units_per_attempt", 1))
    used = attempt.get("used")
    residual = max(0, q - int(used or 0))
    physical_pending = open_custody and (used is None or residual > 0 or not method.get("consumes", False))
    return {
        "waiting_data": waiting_data,
        "data_arrived": data_arrived,
        "has_evidence": has_evidence,
        "open_custody": open_custody,
        "physical_pending": physical_pending,
        "custody_ids": [q["id"] for q in custody_rows],
    }


def classify_retest_action(
    repo: Repository,
    inv: Inventory,
    order: dict,
    invalid_attempt: dict,
    invalid_map: Dict[str, str],
    *,
    at: Optional[str] = None,
) -> dict:
    """对一个失效项目判断现在属于 可安排 / 缺留样 / 等仪器回报 中哪一类。"""
    at_minute = as_minutes(at)
    method_id = invalid_attempt.get("method")
    method = repo.find("methods", method_id) or {}
    q = int(method.get("units_per_attempt", 1))
    retained = int(order.get("retained_units") or 0)

    # 是否已有上机但未回报的重测/同项目试验
    candidates = _retest_attempts(repo, order["id"], method_id, invalid_attempt["id"],
                                  at_minute=at_minute)
    for cand in candidates:
        state = attempt_receipt_state(repo, cand, at_minute=at_minute)
        if state["waiting_data"] or state["physical_pending"]:
            return {
                "bucket": PlanStatus.AWAITING_INSTRUMENT,
                "label": STATUS_LABELS[PlanStatus.AWAITING_INSTRUMENT],
                "pending_attempt": cand["id"],
                **state,
            }

    # 已有有效读数的重测：区分是否已被新报告引用闭环
    done = []
    for cand in candidates:
        ev = as_minutes(cand.get("evidence_at"))
        in_hand = bool(cand.get("evidence_at")) and (
            at_minute is None or (ev is not None and ev <= at_minute))
        if cand["id"] not in invalid_map and in_hand:
            cites = _report_citing(repo, cand["id"])
            done.append({"attempt": cand["id"], "cited_by_reports": cites})
    if done and any(d["cited_by_reports"] for d in done):
        return {"bucket": "closed", "label": "已重测并换发报告", "retests": done}
    if done:
        return {"bucket": "retest_done_pending_report",
                "label": "重测读数已完成，待换发/补引报告", "retests": done}

    # 实物：先保留样承诺，再安排追加的（可能耗用的）重测
    extra = q if method.get("consumes") else 0
    allowed = list(order.get("allowed_samples") or [])
    caps = {s: max(0, inv.sample_view(s, at_minute=at_minute).schedulable) for s in allowed}
    from .acceptance import retest_allocation
    planned = retest_allocation(
        inv, order, consumptive_units=extra, retained_units=retained,
        at_minute=at_minute)
    total_cap = sum(caps.values())

    wins = candidate_windows(
        repo, method_id, at_minute=at_minute, deadline_minute=as_minutes(order.get("needed_by"))
    )
    good_windows = [w for w in wins if w["feasible"]]

    if not planned or not planned.get("ok"):
        short = (planned or {}).get("short_consumptive") or (planned or {}).get("short_retention") \
            or max(0, extra + retained - total_cap)
        return {
            "bucket": PlanStatus.NEED_SUPPLEMENT,
            "label": STATUS_LABELS[PlanStatus.NEED_SUPPLEMENT],
            "need_units": extra + retained,
            "available_units": total_cap,
            "supplement_units": max(1, short) if planned and planned.get("reason") else max(0, short),
            "short_reason": (planned or {}).get("reason"),
            "capacities": caps,
            "retention_allocation": (planned or {}).get("retention_allocation"),
            "blocked_by": _holders(inv, allowed),
        }
    if not good_windows:
        return {
            "bucket": PlanStatus.NO_INSTRUMENT_WINDOW,
            "label": STATUS_LABELS[PlanStatus.NO_INSTRUMENT_WINDOW],
            "need_units": extra + retained,
            "allocation": planned["allocation"],
            "retention_allocation": planned["retention_allocation"],
            "consumptive_allocation": planned["consumptive_allocation"],
            "windows": wins,
        }
    return {
        "bucket": PlanStatus.SCHEDULABLE,
        "label": STATUS_LABELS[PlanStatus.SCHEDULABLE],
        "need_units": extra + retained,
        "allocation": planned["allocation"],
        "retention_allocation": planned["retention_allocation"],
        "consumptive_allocation": planned["consumptive_allocation"],
        "windows": good_windows,
    }


def _holders(inv: Inventory, samples: List[str]) -> List[dict]:
    out = []
    for s in samples:
        view = inv.sample_view(s)
        if view.overcommitted or any(
            r.active and r.sample == s for r in inv.reservations.values()
        ):
            out.append({"sample": s, "schedulable": view.schedulable,
                        "holders": inv._active_holders(s)})  # noqa: SLF001
    return out


def report_status(
    repo: Repository, inv: Inventory, report_id: str, *, at: Optional[str] = None
) -> dict:
    report = repo.by_id("reports", report_id)
    at_minute = as_minutes(at)
    invalid_map = invalid_attempt_ids(repo, at_minute=at_minute)
    order = repo.find("orders", report.get("order"))
    items = []
    invalid_items = []
    for tid in report.get("attempts") or []:
        a = repo.find("attempts", tid)
        if a is None:
            items.append({"attempt": tid, "missing": True, "valid": False})
            continue
        kid = invalid_map.get(tid)
        retests = _retest_attempts(repo, report["order"], a.get("method"), tid,
                                   at_minute=at_minute)
        retest_rows = []
        closed = False
        for rt in retests:
            rt_valid = rt["id"] not in invalid_map
            cites = _report_citing(repo, rt["id"])
            ev_min = as_minutes(rt.get("evidence_at"))
            evidence_in_hand = bool(rt.get("evidence_at")) and (
                at_minute is None or (ev_min is not None and ev_min <= at_minute)
            )
            if rt_valid and cites and evidence_in_hand:
                closed = True
            retest_rows.append({
                "attempt": rt["id"], "sample": rt.get("sample"),
                "instrument": rt.get("instrument"), "evidence_at": rt.get("evidence_at"),
                "valid": rt_valid, "invalidated_by": invalid_map.get(rt["id"]),
                "cited_by_reports": cites,
                "has_evidence": evidence_in_hand,
            })
        item = {
            "attempt": tid,
            "method": a.get("method"),
            "sample": a.get("sample"),
            "instrument": a.get("instrument"),
            "evidence_at": a.get("evidence_at"),
            "valid": kid is None,
            "invalidated_by": kid,
            "retests": retest_rows,
            "retest_closed_by_report": closed,
            "still_usable": kid is None,
            "applicability": check_applicability(repo, a, order),
        }
        items.append(item)
        if kid:
            invalid_items.append(item)

    valid_count = sum(1 for i in items if i.get("valid"))
    if not invalid_items:
        validity = "valid"
    elif valid_count == 0:
        validity = "invalid"
    else:
        validity = "partially_invalid"

    order = order or {"id": report.get("order")}
    actions = []
    for item in invalid_items:
        a = repo.by_id("attempts", item["attempt"])
        action = classify_retest_action(repo, inv, order, a, invalid_map, at=at)
        # 若已有有效重测，则进度优先于“可安排”
        good_retests = []
        for r in item["retests"]:
            ev = as_minutes(r.get("evidence_at"))
            evidence_in_hand = r["has_evidence"] and (at_minute is None or (ev is not None and ev <= at_minute))
            if r["valid"] and evidence_in_hand:
                good_retests.append(r)
        if item["retest_closed_by_report"]:
            action = {"bucket": "closed", "label": "已重测并换发报告",
                      "retests": item["retests"]}
        elif good_retests:
            action = {"bucket": "retest_done_pending_report",
                      "label": "重测读数已完成，待换发/补引报告",
                      "retests": item["retests"]}
        action["for_attempt"] = item["attempt"]
        action["method"] = item["method"]
        actions.append(action)

    return {
        "report": report_id,
        "order": report.get("order"),
        "issued": report.get("issued"),
        "at": at,
        "validity": validity,
        "original_report_preserved": True,
        "cited_attempts": list(report.get("attempts") or []),
        "items": items,
        "actions": actions,
    }


def order_progress(repo: Repository, inv: Inventory, order_id: str, *, at: Optional[str] = None) -> dict:
    """汇总一份委托的报告效力、在途试验与送检方应看到的状态分类。"""
    order = repo.by_id("orders", order_id)
    at_minute = as_minutes(at)
    all_reports = repo.reports_for_order(order_id)
    if at_minute is not None:
        # 回放时刻之后才签发的报告当时尚不存在
        reports = [r for r in all_reports
                   if (as_minutes(r.get("issued")) or 0) <= at_minute]
    else:
        reports = all_reports
    report_rows = [report_status(repo, inv, r["id"], at=at) for r in reports]

    # 在途/未回报的委托试验（回放时刻尚未上机的不计）
    inflight = []
    custody_exceptions = []
    for a in repo.attempts_for_order(order_id):
        if not _visible_attempt(a, at_minute):
            continue
        st = attempt_receipt_state(repo, a, at_minute=at_minute)
        if st["waiting_data"] or st["physical_pending"]:
            inflight.append({"attempt": a["id"], "method": a.get("method"), **st})
        if st["open_custody"]:
            # 交接记载未结：即使 used=q 不产生可排扣减，也要提示实物交回无记录
            custody_exceptions.append({"attempt": a["id"], "method": a.get("method"), **st})

    # 没有报告覆盖的方法
    cited_methods = set()
    for r in reports:
        for tid in r.get("attempts") or []:
            a = repo.find("attempts", tid)
            if a:
                cited_methods.add(a.get("method"))
    covered_methods = set(cited_methods)
    for rr in report_rows:
        for it in rr["items"]:
            for rt in it.get("retests", []):
                if rt.get("cited_by_reports"):
                    covered_methods.add(it.get("method"))
    missing_report_methods = [m for m in (order.get("methods") or []) if m not in cited_methods]

    # 选择送检方主状态（优先级：等回报 > 缺留样 > 无窗口 > 可安排 > 报告进度）
    buckets = [act["bucket"] for rr in report_rows for act in rr["actions"]]
    state = None
    detail = None
    if any(x["waiting_data"] for x in inflight):
        state = PlanStatus.AWAITING_INSTRUMENT
        detail = {"inflight": inflight}
    elif "need_supplement" in buckets:
        state = PlanStatus.NEED_SUPPLEMENT
        detail = next(a for rr in report_rows for a in rr["actions"]
                      if a["bucket"] == "need_supplement")
    elif "no_instrument_window" in buckets:
        state = PlanStatus.NO_INSTRUMENT_WINDOW
    elif "schedulable" in buckets:
        state = PlanStatus.SCHEDULABLE
    elif any(a["bucket"] == "retest_done_pending_report"
             for rr in report_rows for a in rr["actions"]):
        state = "retest_done_pending_report"
    elif report_rows and all(rr["validity"] == "valid" for rr in report_rows) and not missing_report_methods:
        state = "valid"
    elif missing_report_methods:
        # 首轮试验是否都已拿到有效读数/回执
        pending = [x for x in inflight]
        if pending:
            state = PlanStatus.AWAITING_INSTRUMENT
            detail = {"inflight": pending}
        else:
            state = "ready_to_issue_report"
            detail = {"methods_awaiting_report": missing_report_methods}
    else:
        state = "in_progress"

    return {
        "order": order_id,
        "customer": order.get("customer"),
        "needed_by": order.get("needed_by"),
        "at": at,
        "sender_state": state,
        "sender_label": SENDER_LABELS.get(state, state),
        "detail": detail,
        "reports": [
            {"report": rr["report"], "validity": rr["validity"],
             "actions": [{"method": a.get("method"), "bucket": a.get("bucket"),
                          "label": a.get("label")} for a in rr["actions"]]}
            for rr in report_rows
        ],
        "inflight": inflight,
        "physical_return_exceptions": custody_exceptions,
        "methods_without_original_report": missing_report_methods,
    }


SENDER_LABELS = {
    PlanStatus.SCHEDULABLE: "可以安排复测/试验",
    PlanStatus.NEED_SUPPLEMENT: "缺合格留样，需补件",
    PlanStatus.AWAITING_INSTRUMENT: "在等仪器回报",
    PlanStatus.NO_INSTRUMENT_WINDOW: "没有合格仪器窗口",
    "valid": "报告当前有效",
    "retest_done_pending_report": "复测已完成，待换发报告",
    "ready_to_issue_report": "读数与回执已齐，待签发报告",
    "in_progress": "试验进行中",
    "closed": "复测已闭环",
}


def sender_overview(repo: Repository, inv: Inventory, *, at: Optional[str] = None) -> List[dict]:
    return [order_progress(repo, inv, o["id"], at=at) for o in repo.rows("orders")]


# 送检方最关心的三个可办分类
TRIAGE_BUCKETS = ("schedulable", "need_supplement", "awaiting_instrument")


def sender_board(repo: Repository, inv: Inventory, *, at: Optional[str] = None) -> dict:
    """把全部委托按 当前效力/可办状态 分组，直接对应叙述中的三类区分。"""
    overview = sender_overview(repo, inv, at=at)
    board: Dict[str, List[dict]] = {
        "schedulable": [],
        "need_supplement": [],
        "awaiting_instrument": [],
        "retest_done_pending_report": [],
        "ready_to_issue_report": [],
        "valid": [],
        "other": [],
    }
    for row in overview:
        state = row["sender_state"]
        compact = {
            "order": row["order"],
            "customer": row["customer"],
            "needed_by": row["needed_by"],
            "sender_state": state,
            "sender_label": row["sender_label"],
            "detail": row["detail"],
            "reports": row["reports"],
        }
        if state in TRIAGE_BUCKETS:
            board[state].append(compact)
        elif state in board:
            board[state].append(compact)
        else:
            board["other"].append(compact)
    board["triage"] = {key: [x["order"] for x in board[key]] for key in TRIAGE_BUCKETS}
    board["at"] = at
    return board
