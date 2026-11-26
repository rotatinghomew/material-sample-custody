"""用最小构造报文验证叙述中的每条业务规则。"""
import json
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from threading import Thread

from labdesk import LabService, ClaimError
from labdesk.calibration import calibration_impacts
from labdesk.reports import classify_retest_action
from labdesk.acceptance import plan_order


def packet(**over):
    base = {
        "subsamples": [
            {"id": "S", "lot": "L1", "parent": None, "units": 2,
             "form": "plaque", "supplier": "V"},
        ],
        "orders": [
            {"id": "O", "customer": "C", "lot": "L1", "allowed_samples": ["S"],
             "methods": ["bend"], "retained_units": 1, "needed_by": "17:00"},
        ],
        "methods": [
            {"id": "bend", "consumes": True, "units_per_attempt": 1},
            {"id": "scan", "consumes": False, "units_per_attempt": 1},
        ],
        "instruments": [
            {"id": "I1", "method": "bend", "qualification": "Q",
             "begin": "08:00", "end": "18:00"},
            {"id": "I3", "method": "scan", "qualification": "Q3",
             "begin": "08:00", "end": "18:00"},
        ],
        "attempts": [],
        "calibrations": [],
        "reports": [],
        "custody": [],
    }
    base.update(over)
    return base


class CalibrationRuleTest(unittest.TestCase):
    def setUp(self):
        p = packet(
            subsamples=[{"id": "S", "lot": "L1", "parent": None, "units": 4,
                         "form": "p", "supplier": "V"}],
            attempts=[
                {"id": "Ta", "order": "O", "instrument": "I1", "method": "bend",
                 "sample": "S", "start": "09:00", "evidence_at": "10:00",
                 "received": "10:05", "used": 1},
                {"id": "Tb", "order": "O", "instrument": "I1", "method": "bend",
                 "sample": "S", "start": "10:00", "evidence_at": "11:00",
                 "received": "11:05", "used": 1},
            ],
            calibrations=[
                {"id": "K", "instrument": "I1", "invalid_from": "10:00",
                 "invalid_to": "11:00", "recorded": "12:00", "affects": ["bend"]},
            ],
        )
        self.svc = LabService(p)

    def test_half_open_endpoints_use_evidence_time(self):
        # evidence==from 命中（左闭）；evidence==to 不命中（右开）
        impacts = calibration_impacts(self.svc.repo)
        self.assertEqual(impacts[0]["affected_attempts"], ["Ta"])

    def test_recorded_time_gates_visibility(self):
        # 11:30 时校准尚未登记(recorded=12:00)，报告仍视为有效
        inv_map_early = __import__("labdesk.calibration", fromlist=["invalid_attempt_ids"]) \
            .invalid_attempt_ids(self.svc.repo, at_minute=11 * 60 + 30)
        self.assertEqual(inv_map_early, {})
        late = __import__("labdesk.calibration", fromlist=["invalid_attempt_ids"]) \
            .invalid_attempt_ids(self.svc.repo, at_minute=12 * 60 + 1)
        self.assertIn("Ta", late)


class QuantityRuleTest(unittest.TestCase):
    def test_unconfirmed_destruction_differs_from_book(self):
        p = packet(attempts=[
            {"id": "T1", "order": "O", "instrument": "I1", "method": "bend",
             "sample": "S", "start": "09:00", "evidence_at": "09:10",
             "received": None, "used": None},
        ])
        svc = LabService(p)
        v = svc.inventory.sample_view("S")
        self.assertEqual(v.book_available, 2)
        self.assertEqual(v.unconfirmed, 1)
        self.assertEqual(v.schedulable, 1)  # 损毁未确认：可排比账面少

    def test_non_destructive_open_custody_holds_unit_until_return(self):
        p = packet(
            orders=[{"id": "O", "customer": "C", "lot": "L1",
                     "allowed_samples": ["S"], "methods": ["scan"],
                     "retained_units": 0, "needed_by": "17:00"}],
            attempts=[
                {"id": "T2", "order": "O", "instrument": "I3", "method": "scan",
                 "sample": "S", "start": "09:00", "evidence_at": "09:15",
                 "received": "09:20", "used": 0},
            ],
            custody=[{"id": "Q2", "attempt": "T2", "sample": "S",
                      "handed": "09:00", "received_from_instrument": None,
                      "reported": None}],
        )
        svc = LabService(p)
        v = svc.inventory.sample_view("S")
        self.assertEqual(v.book_available, 2)
        self.assertEqual(v.in_instrument, 1)
        self.assertEqual(v.schedulable, 1)  # 还在仪器：可排 != 账面
        svc.return_from_instrument("Q2", "09:30")
        self.assertEqual(svc.inventory.sample_view("S").schedulable, 2)

    def test_null_return_does_not_restore_stock(self):
        p = packet(attempts=[
            {"id": "T3", "order": "O", "instrument": "I1", "method": "bend",
             "sample": "S", "start": "10:00", "evidence_at": "10:10",
             "received": "16:50", "used": 1},
        ], custody=[{"id": "Q3", "attempt": "T3", "sample": "S", "handed": "10:00",
                     "received_from_instrument": None, "reported": "10:12"}])
        svc = LabService(p)
        v = svc.inventory.sample_view("S")
        self.assertEqual(v.consumed, 1)
        self.assertEqual(v.book_available, 1)
        # used=q=1：未交回不再额外扣同一份；另一份可排
        self.assertEqual(v.schedulable, 1)


class ClaimMutualExclusionTest(unittest.TestCase):
    def test_only_one_holder_wins_shared_unit(self):
        p = packet(subsamples=[{"id": "A3", "lot": "L1", "parent": None,
                                "units": 1, "form": "p", "supplier": "V"}])
        svc = LabService(p)
        svc.inventory.occupy("A3", 1, order="O11", reservation_id="r1")
        with self.assertRaises(ClaimError) as ctx:
            svc.inventory.occupy("A3", 1, order="O12", reservation_id="r2")
        self.assertEqual(ctx.exception.code, "insufficient_schedulable")
        self.assertEqual(ctx.exception.details["active_holders"][0]["order"], "O11")
        svc.inventory.release("r1")
        r2 = svc.inventory.occupy("A3", 1, order="O12", reservation_id="r2")
        self.assertTrue(r2.active)

    def test_accept_two_orders_one_retention_unit(self):
        p = packet(
            subsamples=[{"id": "A3", "lot": "L1", "parent": None, "units": 1,
                         "form": "p", "supplier": "V"}],
            orders=[
                {"id": "O11", "customer": "b1", "lot": "L1",
                 "allowed_samples": ["A3"], "methods": ["scan"],
                 "retained_units": 1, "needed_by": "17:00"},
                {"id": "O12", "customer": "b2", "lot": "L1",
                 "allowed_samples": ["A3"], "methods": ["scan"],
                 "retained_units": 1, "needed_by": "18:00"},
            ],
        )
        svc = LabService(p)
        svc.accept_order("O11")
        with self.assertRaises(ClaimError) as ctx:
            svc.accept_order("O12")
        self.assertEqual(ctx.exception.code, "need_supplement")

    def test_retest_cannot_take_other_orders_occupation(self):
        # 2 份实物：T1 已耗用 1 份，另 1 份被 O12 有效占用；
        # O11 等待复测也不能腾挪 O12 的占用。
        p = packet(
            subsamples=[{"id": "A3", "lot": "L1", "parent": None, "units": 2,
                         "form": "p", "supplier": "V"}],
            orders=[
                {"id": "O11", "customer": "b1", "lot": "L1",
                 "allowed_samples": ["A3"], "methods": ["bend"],
                 "retained_units": 0, "needed_by": "17:00"},
            ],
            attempts=[
                {"id": "T1", "order": "O11", "instrument": "I1", "method": "bend",
                 "sample": "A3", "start": "09:00", "evidence_at": "09:30",
                 "received": "09:40", "used": 1},
            ],
            calibrations=[
                {"id": "K1", "instrument": "I1", "invalid_from": "09:25",
                 "invalid_to": "10:00", "recorded": "14:00", "affects": ["bend"]},
            ],
        )
        svc = LabService(p)
        # 再来一份“别的委托”的有效占用（这里直接以 O12 留样表示）
        svc.inventory.occupy("A3", 1, order="O12", kind="retention",
                             reservation_id="RET-O12-A3")
        order = svc.repo.by_id("orders", "O11")
        t1 = svc.repo.by_id("attempts", "T1")
        from labdesk.calibration import invalid_attempt_ids
        action = classify_retest_action(
            svc.repo, svc.inventory, order, t1, invalid_attempt_ids(svc.repo))
        self.assertEqual(action["bucket"], "need_supplement")
        holder_orders = [h["order"] for b in action["blocked_by"] for h in b["holders"]]
        self.assertIn("O12", holder_orders)


class LateReceiptRuleTest(unittest.TestCase):
    def test_late_used_versioned_and_consumed_unit_not_resplittable(self):
        p = packet(attempts=[
            {"id": "T1", "order": "O", "instrument": "I1", "method": "bend",
             "sample": "S", "start": "09:00", "evidence_at": "09:30",
             "received": None, "used": None},
        ])
        svc = LabService(p)
        self.assertEqual(svc.inventory.sample_view("S").schedulable, 1)
        svc.instrument_receipt("T1", {"used": 1, "received": "15:00"},
                               reliability="instrument", arrived_at="15:00")
        v = svc.inventory.sample_view("S")
        self.assertEqual(v.consumed, 1)
        self.assertEqual(v.schedulable, 1)
        # 原回执保留，初始 used=None 仍可查
        versions = svc.repo.receipts["T1"]
        self.assertEqual(len(versions), 2)
        self.assertIsNone(versions[0]["fact"]["used"])
        self.assertEqual(svc.repo.by_id("attempts", "T1")["used"], 1)
        # 已用掉的单位不能借分取重新可分：S base=2，自由仅 1
        svc.split_sample("S", "S2", 1)
        with self.assertRaises(ClaimError):
            svc.split_sample("S", "S3", 1)

    def test_late_reading_belongs_to_original_attempt_not_retest(self):
        p = packet(
            subsamples=[{"id": "S", "lot": "L1", "parent": None, "units": 4,
                         "form": "p", "supplier": "V"}],
            attempts=[
                {"id": "T1", "order": "O", "instrument": "I1", "method": "bend",
                 "sample": "S", "start": "09:00", "evidence_at": "09:30",
                 "received": "09:40", "used": 1},
                {"id": "T9", "order": "O", "instrument": "I1", "method": "bend",
                 "sample": "S", "start": "15:00", "evidence_at": "15:10",
                 "received": "15:20", "used": 1, "retest_of": "T1"},
            ],
            calibrations=[
                {"id": "K1", "instrument": "I1", "invalid_from": "09:25",
                 "invalid_to": "10:15", "recorded": "14:00", "affects": ["bend"]},
            ],
            reports=[{"id": "R1", "order": "O", "issued": "16:00",
                      "attempts": ["T1", "T9"]}],
        )
        svc = LabService(p)
        # 迟到读数更正原 T1（仍落在失效区间），绝不改 T9
        svc.instrument_receipt("T1", {"evidence_at": "09:40"}, source="late")
        self.assertEqual(svc.repo.by_id("attempts", "T1")["evidence_at"], "09:40")
        self.assertEqual(svc.repo.by_id("attempts", "T9")["evidence_at"], "15:10")
        status = svc.report("R1")
        items = {i["attempt"]: i for i in status["items"]}
        self.assertFalse(items["T1"]["valid"])
        self.assertTrue(items["T9"]["valid"])


class SplitTraceRuleTest(unittest.TestCase):
    def test_split_conservation_and_trace(self):
        p = packet(subsamples=[
            {"id": "A1", "lot": "B51", "parent": None, "units": 6,
             "form": "plaque", "supplier": "Vendor-A"},
        ])
        svc = LabService(p)
        svc.split_sample("A1", "A2", 2)
        svc.split_sample("A1", "A3", 2)
        self.assertEqual(svc.inventory.lot_physical_units("B51"), 6)
        self.assertEqual(svc.inventory.sample_view("A1").base_units, 2)
        trace = svc.inventory.trace("A3")
        self.assertEqual([x["id"] for x in trace["ancestry"]], ["A3", "A1"])
        self.assertEqual(trace["supplier"], "Vendor-A")

    def test_split_cannot_resurrect_consumed_units(self):
        p = packet(attempts=[
            {"id": "T1", "order": "O", "instrument": "I1", "method": "bend",
             "sample": "S", "start": "09:00", "evidence_at": "09:10",
             "received": "09:20", "used": 2},
        ])
        svc = LabService(p)  # S units=2 全部耗用
        with self.assertRaises(ClaimError):
            svc.split_sample("S", "SX", 1)


class AcceptanceRuleTest(unittest.TestCase):
    def test_material_gap_reports_supplement_units(self):
        p = packet(subsamples=[{"id": "S", "lot": "L1", "parent": None,
                                 "units": 1, "form": "p", "supplier": "V"}])
        svc = LabService(p)  # bend 耗用1 + 留样1 = 2，只有1
        plan = svc.preview_order("O")
        self.assertEqual(plan["status"], "need_supplement")
        self.assertEqual(plan["supplement_units"], 1)

    def test_instrument_window_deadline(self):
        p = packet(instruments=[
            {"id": "I1", "method": "bend", "qualification": "Q",
             "begin": "08:00", "end": "14:00"},
        ])
        svc = LabService(p)
        order = {"id": "OX", "allowed_samples": ["S"], "methods": ["bend"],
                 "retained_units": 0, "needed_by": "17:00"}
        early = plan_order(svc.repo, svc.inventory, order, at="10:00")
        self.assertEqual(early["status"], "schedulable")  # 14:00 前可做
        late = plan_order(svc.repo, svc.inventory, order, at="14:30")
        self.assertEqual(late["status"], "no_instrument_window")

    def test_non_consumptive_ride_along_needs_no_extra_unit(self):
        p = packet(
            subsamples=[{"id": "S", "lot": "L1", "parent": None, "units": 2,
                         "form": "p", "supplier": "V"}],
            orders=[{"id": "O", "customer": "C", "lot": "L1",
                     "allowed_samples": ["S"], "methods": ["bend", "scan"],
                     "retained_units": 1, "needed_by": "17:00"}],
        )
        svc = LabService(p)
        plan = svc.preview_order("O")
        # bend 1 + 留样 1 = 2；scan 可搭在同一单位顺序做，不另加
        self.assertEqual(plan["material"]["required_units"], 2)
        self.assertEqual(plan["status"], "schedulable")


class ServiceWorkflowTest(unittest.TestCase):
    def test_receive_batch_and_group_message(self):
        svc = LabService(packet())
        svc.receive_batch([
            {"id": "N1", "lot": "L9", "parent": None, "units": 3,
             "form": "pellet", "supplier": "V2"},
        ])
        self.assertEqual(svc.repo.by_id("subsamples", "N1")["units"], 3)
        out = svc.load_message({"group": "instruments", "row": {
            "id": "I9", "method": "scan", "qualification": "C9",
            "begin": "00:00", "end": "23:59"}})
        self.assertTrue(out["ok"])
        self.assertTrue(svc.repo.has("instruments", "I9"))

    def test_accept_then_retest_needs_supplement_when_no_free_unit(self):
        svc = LabService(packet())  # S units=2, bend 耗用1 + 留样1
        result = svc.accept_order("O")
        self.assertTrue(any(r.startswith("RET-O-") for r in result["reservations"]))
        svc.repo.add("attempts", {
            "id": "T1", "order": "O", "instrument": "I1", "method": "bend",
            "sample": "S", "start": "09:00", "evidence_at": "09:30", "used": 1})
        v = svc.inventory.sample_view("S")
        self.assertEqual(v.consumed, 1)
        self.assertEqual(v.reserved, 1)
        self.assertEqual(v.schedulable, 0)
        # 没有额外实物：复测不能动用已承诺留样，也没有别的占用可腾
        with self.assertRaises(ClaimError):
            svc.start_retest("T1", "T99", instrument="I1", start="16:00")
        # 送检方补来 1 份替代样 S2 并加入允许清单后，重测可落在 S2
        svc.repo.add("subsamples", {"id": "S2", "lot": "L1", "parent": None,
                                    "units": 1, "form": "plaque", "supplier": "V"})
        svc.repo.by_id("orders", "O")["allowed_samples"].append("S2")
        rt = svc.start_retest("T1", "T99", instrument="I1", start="16:00",
                              sample="S2", evidence_at="16:10",
                              received="16:15", used=1)
        self.assertEqual(rt["attempt"]["retest_of"], "T1")
        self.assertTrue(svc.repo.has("attempts", "T99"))


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from labdesk.server import create_handler
        from pathlib import Path
        data = Path(__file__).resolve().parents[1] / "data" / "samples_20261208.json"
        cls.service = LabService(data)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                        create_handler(cls.service, data))
        cls.port = cls.httpd.server_address[1]
        cls.thread = Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def test_impacts_endpoint(self):
        status, body = self.get("/api/impacts")
        self.assertEqual(status, 200)
        ids = {x["calibration"]: x for x in body}
        self.assertEqual(ids["K1"]["affected_orders"], ["O11"])
        self.assertEqual(ids["K2"]["affected_attempts"], [])

    def test_data_file_endpoint(self):
        status, body = self.get("/samples_20261208.json")
        self.assertEqual(status, 200)
        self.assertIn("subsamples", body)

    def test_post_correction_message(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/message",
            data=json.dumps({"receipt": "T2", "changes": {"used": 0}}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 201)


if __name__ == "__main__":
    unittest.main()
