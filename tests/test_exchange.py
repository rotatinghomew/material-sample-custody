"""检查本地交换资料的完整性和取件接口。"""
from hashlib import sha256
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from functools import partial
from pathlib import Path
from threading import Thread
import unittest
from urllib.request import urlopen

DATA = Path(__file__).resolve().parents[1] / "data"
FILE = DATA / "samples_20261208.json"


def by_id(packet, group, key):
    return next(row for row in packet[group] if row["id"] == key)


class ExchangeTest(unittest.TestCase):
    def test_records(self):
        data = json.loads(FILE.read_text(encoding="utf-8"))
        for group, rows in data.items():
            with self.subTest(group=group):
                self.assertTrue(rows)
                self.assertEqual(len(rows), len({row["id"] for row in rows}))
                self.assertTrue(all(row.keys() == rows[0].keys() for row in rows))
        primary, other = (by_id(data, "orders", item) for item in ("O11", "O12"))
        self.assertEqual(set(primary["allowed_samples"]) & set(other["allowed_samples"]), {"A3"})
        attempt = by_id(data, "attempts", "T1")
        calibration = by_id(data, "calibrations", "K1")
        self.assertEqual(attempt["instrument"], calibration["instrument"])
        self.assertLessEqual(calibration["invalid_from"], attempt["evidence_at"])
        self.assertLess(attempt["evidence_at"], calibration["invalid_to"])
        self.assertIsNone(by_id(data, "custody", "Q3")["received_from_instrument"])
        self.assertIn(attempt["id"], by_id(data, "reports", "R1")["attempts"])

    def test_source_over_http(self):
        handler = partial(SimpleHTTPRequestHandler, directory=str(DATA))
        with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                address = f"http://127.0.0.1:{server.server_port}/{FILE.name}"
                with urlopen(address, timeout=5) as reply:
                    self.assertEqual(reply.status, 200)
                    self.assertEqual(sha256(reply.read()).digest(), sha256(FILE.read_bytes()).digest())
            finally:
                server.shutdown()
                thread.join(timeout=5)
