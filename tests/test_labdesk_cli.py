"""命令行入口的冒烟测试。"""
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from labdesk.cli import main

ROOT = Path(__file__).resolve().parents[1]
DATA = str(ROOT / "data" / "samples_20261208.json")


class CliTest(unittest.TestCase):
    def run_json(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--data", DATA, *argv])
        self.assertEqual(code, 0)
        return json.loads(buf.getvalue())

    def test_quantities(self):
        q = self.run_json(["quantities"])
        self.assertEqual(q["A3"]["schedulable"], 1)

    def test_impacts(self):
        rows = self.run_json(["impacts"])
        self.assertEqual({r["calibration"] for r in rows}, {"K1", "K2"})

    def test_board_at(self):
        board = self.run_json(["board", "--at", "14:20"])
        self.assertEqual(board["triage"]["schedulable"], ["O11"])
        self.assertEqual(board["triage"]["awaiting_instrument"], ["O12"])

    def test_order_and_report(self):
        o = self.run_json(["order", "O13"])
        self.assertEqual(o["sender_state"], "valid")
        r = self.run_json(["report", "R1"])
        self.assertEqual(r["validity"], "partially_invalid")


if __name__ == "__main__":
    unittest.main()
