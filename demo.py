#!/usr/bin/env python3
"""端到端联调演示：校准影响 → 三分类 → 共享实物互斥 → 迟到回执归原试验。

运行：``python3 demo.py``（仅标准库，不修改 data/ 下的原始报文）。
"""
from __future__ import annotations

import json

from labdesk import LabService
from labdesk.inventory import ClaimError

DATA = "data/samples_20261208.json"


def show(title, value):
    print(f"\n=== {title} ===")
    if isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> int:
    svc = LabService(DATA)

    show("校准更正影响", [
        {"calibration": x["calibration"], "hits": x["affected_attempts"],
         "orders": x["affected_orders"], "reports": x["affected_reports"]}
        for x in svc.calibration_impacts()
    ])

    show("14:20 送检方三分类看板", svc.board(at="14:20")["triage"])
    show("K1 补测安排（按当时实际留样）", svc.arrange_for_calibration("K1", at="14:20"))

    print("\n=== 共享 A3 原子占用演示 ===")
    a = svc.inventory.occupy("A3", 1, order="O11", reservation_id="DEMO-O11-A3")
    print("O11 占用成功:", a.id, a.serials)
    try:
        svc.inventory.occupy("A3", 1, order="O12", reservation_id="DEMO-O12-A3")
    except ClaimError as exc:
        print("O12 占用被拒绝:", exc)
        print("占用方:", exc.details["active_holders"])
    svc.inventory.release("DEMO-O11-A3")
    b = svc.inventory.occupy("A3", 1, order="O12", reservation_id="DEMO-O12-A3")
    print("O11 释放后 O12 占用成功:", b.id)

    print("\n=== 迟到回执：同编号更正且保留原回执 ===")
    before = svc.repo.by_id("attempts", "T3")["received"]
    svc.instrument_receipt("T3", {"used": 1}, source="late-instrument")
    versions = svc.repo.receipts["T3"]
    print("T3 received 快照值:", before, "| 回执版本数:", len(versions))
    print("初始回执仍保留 used =", versions[0]["fact"].get("used"))

    show("日终送检方状态", [
        {"order": o["order"], "state": o["sender_state"], "label": o["sender_label"],
         "open_custody": [x["attempt"] for x in o["physical_return_exceptions"]]}
        for o in svc.overview()
    ])
    print("\n完整结论：python3 -m labdesk analyze --write out/decision_20261208.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
