"""对演练快照生成完整、可归档的核对结论（机器可读 JSON + 供文档引用）。"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional, Union

from .acceptance import reconcile_retentions
from .service import LabService

# 演练中几个有业务意义的核对时点
CUTS = ("11:05", "14:05", "14:20", "16:00", "17:10")


def build_analysis(source: Union[str, Path, dict]) -> dict:
    path: Optional[Path] = None
    if not isinstance(source, dict):
        path = Path(source)
    svc = LabService(source)

    raw_bytes = path.read_bytes() if path else b""
    digest = hashlib.sha256(raw_bytes).hexdigest() if raw_bytes else None

    quantities = svc.quantities()
    lot_totals = {}
    for s in svc.repo.rows("subsamples"):
        lot_totals[s["lot"]] = svc.inventory.lot_physical_units(s["lot"])

    traces = {s["id"]: svc.inventory.trace(s["id"]) for s in svc.repo.rows("subsamples")}
    impacts = svc.calibration_impacts()
    retentions = reconcile_retentions(svc.repo, svc.inventory, create=False)

    report_ids = [r["id"] for r in svc.repo.rows("reports")]
    reports_eod = {rid: svc.report(rid) for rid in report_ids}

    timeline = {cut: svc.overview(at=cut) for cut in CUTS}
    timeline["eod"] = svc.overview(at=None)

    boards = {cut: svc.board(at=cut)["triage"] for cut in CUTS}
    boards["eod"] = svc.board(at=None)["triage"]

    previews = {
        o["id"]: svc.preview_order(o["id"], at="08:30")
        for o in svc.repo.rows("orders")
    }

    # 在一份全新服务上演示“受理即原子锁定留样”，共享 A3 不会被双方同时占用
    atomic_demo = []
    demo = LabService(source) if not isinstance(source, dict) else LabService(dict(source))
    for oid in ("O11", "O12", "O13"):
        try:
            res = demo.accept_order(oid, at="08:30")
            atomic_demo.append({"order": oid, "accepted": True,
                                "reservations": res["reservations"]})
        except Exception as exc:  # noqa: BLE001
            atomic_demo.append({"order": oid, "accepted": False,
                                "reason": str(exc)})

    k1 = next((x for x in impacts if x["calibration"] == "K1"), None)
    k2 = next((x for x in impacts if x["calibration"] == "K2"), None)

    return {
        "meta": {
            "exercise_date": "2026-12-08",
            "timezone": "UTC+08:00",
            "source_file": str(path) if path else "<dict>",
            "source_sha256": digest,
            "cuts": list(CUTS) + ["eod"],
        },
        "headline": {
            "calibration_K1_hits": (k1 or {}).get("affected_attempts", []),
            "calibration_K1_orders": (k1 or {}).get("affected_orders", []),
            "calibration_K1_reports": (k1 or {}).get("affected_reports", []),
            "calibration_K2_hits": (k2 or {}).get("affected_attempts", []),
            "truly_affected_orders": sorted({
                oid for x in impacts for oid in x["affected_orders"]
            }),
            "shared_sample_O11_O12": "A3",
            "open_custody": ["Q3"],
            "retention_assignment": retentions,
        },
        "lot_physical_units": lot_totals,
        "quantities": quantities,
        "traceability": traces,
        "calibration_impacts": impacts,
        "acceptance_preview": previews,
        "atomic_acceptance_demo": atomic_demo,
        "reports_eod": reports_eod,
        "order_timeline": timeline,
        "triage_boards": boards,
    }
