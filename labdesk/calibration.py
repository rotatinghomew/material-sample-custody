"""校准更正影响：按读数实际取得时刻 ``evidence_at`` 与精确方法判定。

区间端点左闭右开。只看上机时间 ``start``、仪器上送时间 ``received`` 或
报告签发时间都会误判；校准只作用于同一仪器且在 ``affects`` 列出的方法。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .repository import Repository
from .timeutil import as_minutes, in_half_open


def calibration_hits(repo: Repository, calibration: dict) -> List[dict]:
    """返回某条校准更正真正覆盖的试验（可能为空）。"""
    hits = []
    for a in repo.rows("attempts"):
        if a.get("instrument") != calibration.get("instrument"):
            continue
        affected_methods = calibration.get("affects") or []
        if affected_methods and a.get("method") not in affected_methods:
            continue
        if in_half_open(
            a.get("evidence_at"),
            calibration.get("invalid_from"),
            calibration.get("invalid_to"),
        ):
            hits.append(a)
    return hits


def calibration_impacts(repo: Repository) -> List[dict]:
    """逐条校准给出受影响的试验、委托与报告。"""
    results = []
    for k in repo.rows("calibrations"):
        hits = calibration_hits(repo, k)
        attempt_ids = [a["id"] for a in hits]
        order_ids = sorted({a.get("order") for a in hits if a.get("order")})
        report_ids = []
        for r in repo.rows("reports"):
            if any(t in (r.get("attempts") or []) for t in attempt_ids):
                report_ids.append(r["id"])
        results.append({
            "calibration": k["id"],
            "instrument": k.get("instrument"),
            "window": [k.get("invalid_from"), k.get("invalid_to")],
            "recorded": k.get("recorded"),
            "methods": k.get("affects") or [],
            "affected_attempts": attempt_ids,
            "affected_orders": order_ids,
            "affected_reports": sorted(report_ids),
            "hit_count": len(hits),
        })
    return results


def invalid_attempt_ids(repo: Repository, *, at_minute: Optional[int] = None) -> Dict[str, str]:
    """返回当前失效的试验编号 -> 命中的校准编号。

    ``at_minute`` 给定时，只采用在该时刻之前已经登记（``recorded``）的
    校准更正，用于回放更正到达前后的业务效力。
    """
    invalid: Dict[str, str] = {}
    for k in repo.rows("calibrations"):
        if at_minute is not None:
            recorded = as_minutes(k.get("recorded"))
            if recorded is not None and recorded > at_minute:
                continue
        for a in calibration_hits(repo, k):
            invalid[a["id"]] = k["id"]
    return invalid


def attempt_validity(repo: Repository, *, at_minute: Optional[int] = None) -> List[dict]:
    invalid = invalid_attempt_ids(repo, at_minute=at_minute)
    result = []
    for a in repo.rows("attempts"):
        kid = invalid.get(a["id"])
        result.append({
            "attempt": a["id"],
            "order": a.get("order"),
            "method": a.get("method"),
            "instrument": a.get("instrument"),
            "sample": a.get("sample"),
            "evidence_at": a.get("evidence_at"),
            "valid": kid is None,
            "invalidated_by": kid,
        })
    return result
