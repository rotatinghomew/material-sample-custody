"""针对 samples_20261208.json 的精确业务结论测试。"""
import json
import unittest
from pathlib import Path

from labdesk import LabService
from labdesk.acceptance import reconcile_retentions
from labdesk.calibration import calibration_hits, attempt_validity
from labdesk.repository import load_packet

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "samples_20261208.json"


class SnapshotFactsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = LabService(DATA)
        cls.repo = cls.svc.repo
        cls.inv = cls.svc.inventory

    def test_lot_total_avoids_parent_child_double_count(self):
        # A1=6 已全部抽出 A2/A3/A4 各 2；B51 实物仍是 6，不是 12。
        self.assertEqual(self.inv.lot_physical_units("B51"), 6)
        self.assertEqual(self.inv.lot_physical_units("B52"), 4)
        a1 = self.inv.sample_view("A1")
        self.assertEqual(a1.received, 6)
        self.assertEqual(a1.child_units, 6)
        self.assertEqual(a1.base_units, 0)

    def test_book_and_schedulable_balances(self):
        q = self.svc.quantities()
        for sid in ("A2", "A3", "A4"):
            self.assertEqual(q[sid]["consumed"], 1, sid)
            self.assertEqual(q[sid]["book_available"], 1, sid)
            self.assertEqual(q[sid]["schedulable"], 1, sid)
        self.assertEqual(q["B2"]["book_available"], 4)
        self.assertEqual(q["B2"]["schedulable"], 4)
        # Q3 未交回但 T3 已证实 used=1=需求份数：同一份实物不重复扣减，
        # A4 未上机的另一份仍可排；T3 仍作为未结交接被列出。
        self.assertEqual(q["A4"]["in_instrument"], 0)
        self.assertIn("T3", q["A4"]["open_attempts"])

    def test_k1_hits_only_t1(self):
        k1 = self.repo.by_id("calibrations", "K1")
        self.assertEqual([a["id"] for a in calibration_hits(self.repo, k1)], ["T1"])
        impacts = {x["calibration"]: x for x in self.svc.calibration_impacts()}
        self.assertEqual(impacts["K1"]["affected_orders"], ["O11"])
        self.assertEqual(impacts["K1"]["affected_reports"], ["R1"])

    def test_k2_boundary_and_evidence_time_trap(self):
        # K2=[10:00,10:30) scan on I3：T2 evidence 09:15 太早；
        # T5 start=10:30 恰为右端点且 evidence=10:45，均不受影响。
        k2 = self.repo.by_id("calibrations", "K2")
        self.assertEqual(calibration_hits(self.repo, k2), [])
        validity = {x["attempt"]: x for x in attempt_validity(self.repo)}
        self.assertTrue(validity["T5"]["valid"])
        self.assertTrue(validity["T2"]["valid"])
        self.assertFalse(validity["T1"]["valid"])
        self.assertEqual(validity["T1"]["invalidated_by"], "K1")
        # T3 虽在钟面区间内且 10:10 读数，但仪器是 I2，不受 K1 影响
        self.assertTrue(validity["T3"]["valid"])

    def test_r1_partial_validity_and_preservation(self):
        r1 = self.svc.report("R1")
        self.assertEqual(r1["validity"], "partially_invalid")
        by_id = {i["attempt"]: i for i in r1["items"]}
        self.assertFalse(by_id["T1"]["valid"])
        self.assertTrue(by_id["T2"]["valid"])
        self.assertTrue(by_id["T2"]["still_usable"])
        self.assertEqual(r1["cited_attempts"], ["T1", "T2"])
        self.assertTrue(r1["original_report_preserved"])

    def test_t4_is_valid_new_retest_not_overwriting_t1(self):
        r1 = self.svc.report("R1")
        action = r1["actions"][0]
        self.assertEqual(action["for_attempt"], "T1")
        self.assertEqual(action["bucket"], "retest_done_pending_report")
        rt = action["retests"][0]
        self.assertEqual(rt["attempt"], "T4")
        self.assertEqual(rt["sample"], "A3")
        self.assertEqual(rt["instrument"], "I2")
        self.assertTrue(rt["valid"])
        self.assertEqual(rt["cited_by_reports"], [])
        # 原 T1 事实仍在
        t1 = self.repo.by_id("attempts", "T1")
        self.assertEqual(t1["evidence_at"], "09:30")
        self.assertEqual(t1["used"], 1)

    def test_r2_fully_valid(self):
        r2 = self.svc.report("R2")
        self.assertEqual(r2["validity"], "valid")
        self.assertEqual(r2["actions"], [])

    def test_sender_states_over_time(self):
        eod = {o["order"]: o["sender_state"] for o in self.svc.overview()}
        self.assertEqual(eod["O11"], "retest_done_pending_report")
        self.assertEqual(eod["O13"], "valid")
        # 14:20：校准已到、T4 未做、T3 回执 16:50 未到
        mid = {o["order"]: o["sender_state"] for o in self.svc.overview(at="14:20")}
        self.assertEqual(mid["O11"], "schedulable")
        self.assertEqual(mid["O12"], "awaiting_instrument")
        self.assertEqual(mid["O13"], "valid")
        # 日终 T3 数据回执已齐可出报告，但 Q3 未结交接仍须可见
        o12 = self.svc.order("O12")
        self.assertEqual(o12["sender_state"], "ready_to_issue_report")
        self.assertEqual([x["attempt"] for x in o12["physical_return_exceptions"]], ["T3"])

    def test_o11_retest_plan_at_1420_uses_a3_i2(self):
        plan = self.svc.preview_order("O11", at="14:20",
                                      extra_consumptive_units=1, retain_override=1)
        self.assertEqual(plan["status"], "schedulable")
        self.assertEqual(plan["supplement_units"], 0)
        bend = plan["windows"]["bend"]
        by_ins = {w["instrument"]: w for w in bend}
        self.assertFalse(by_ins["I1"]["feasible"])   # 窗口 14:00 已结束
        self.assertTrue(by_ins["I2"]["feasible"])
        self.assertIn("I2", [w["instrument"] for w in bend if w["feasible"]])

    def test_calibration_arrangement_protects_a2_retention_and_uses_a3(self):
        out = self.svc.arrange_for_calibration("K1", at="14:20")
        arr = out["arrangements"][0]["arrangement"]
        self.assertEqual(arr["bucket"], "schedulable")
        # 先把 A2 最后一份留作承诺留样，复测耗用落到 A3（与实际 T4 一致）
        self.assertEqual(arr["retention_allocation"], {"A2": 1})
        self.assertEqual(arr["consumptive_allocation"], {"A3": 1})
        self.assertTrue(any(w["instrument"] == "I2" and w["feasible"]
                            for w in arr["windows"]))

    def test_retention_assignment_is_exclusive_and_affine(self):
        rows = reconcile_retentions(self.repo, self.inv, create=False)
        by_order = {r["order"]: r for r in rows}
        self.assertEqual(by_order["O11"]["assignment"], {"A2": 1})
        self.assertEqual(by_order["O12"]["assignment"], {"A4": 1})
        self.assertEqual(by_order["O13"]["status"], "schedulable")

    def test_traceability_after_split_and_instrument_change(self):
        trace = self.inv.trace("A3")
        ids = [x["id"] for x in trace["ancestry"]]
        self.assertEqual(ids, ["A3", "A1"])
        self.assertEqual(trace["lot"], "B51")
        self.assertEqual(trace["supplier"], "Vendor-A")

    def test_shared_a3_is_the_conflict_sample(self):
        o11 = self.repo.by_id("orders", "O11")
        o12 = self.repo.by_id("orders", "O12")
        self.assertEqual(set(o11["allowed_samples"]) & set(o12["allowed_samples"]), {"A3"})

    def test_exchange_file_is_valid_utf8_json(self):
        packet = json.loads(DATA.read_text(encoding="utf-8"))
        repo = load_packet(packet)
        self.assertIs(repo.by_id("orders", "O13")["retained_units"], 0)


if __name__ == "__main__":
    unittest.main()
