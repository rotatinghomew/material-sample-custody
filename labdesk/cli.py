"""命令行：核对快照、查询委托/报告、启动联调 HTTP 服务。

示例::

    python3 -m labdesk analyze --write out/decision_20261208.json
    python3 -m labdesk order O11 --at 14:20
    python3 -m labdesk report R1
    python3 -m labdesk serve --port 8040
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from .analysis import build_analysis
from .service import LabService

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "samples_20261208.json"


def _print(obj) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="labdesk", description="共用实验室送检与复测承诺引擎")
    p.add_argument("--data", default=str(DEFAULT_DATA), help="交换报文 JSON 路径")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("analyze", help="生成完整核对结论")
    a = sub.choices["analyze"]
    a.add_argument("--write", dest="write", default=None, help="同时写入 JSON 文件")

    s = sub.add_parser("state", help="数量/校准/委托总览")
    s.add_argument("--at", default=None)

    o = sub.add_parser("order", help="查询单个委托的送检方状态")
    o.add_argument("order_id")
    o.add_argument("--at", default=None)

    r = sub.add_parser("report", help="查询报告当前效力与复测进度")
    r.add_argument("report_id")
    r.add_argument("--at", default=None)

    sub.add_parser("quantities", help="各子样账面/可排数量")
    sub.add_parser("impacts", help="校准更正影响分析")
    ar = sub.add_parser("arrange", help="按校准更正给出受影响委托的补测安排")
    ar.add_argument("calibration_id")
    ar.add_argument("--at", default=None)
    b = sub.add_parser("board", help="送检方三分类看板")
    b.add_argument("--at", default=None)
    sub.add_parser("retentions", help="求解互不冲突的留样归属")

    sv = sub.add_parser("serve", help="启动 JSON API + 静态报文服务")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8040)
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "serve":
        from .server import run_server
        run_server(host=args.host, port=args.port, data_path=Path(args.data))
        return 0

    if args.cmd == "analyze":
        result = build_analysis(args.data)
        if args.write:
            out = Path(args.write)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"已写入 {out}", file=sys.stderr)
        _print(result)
        return 0

    svc = LabService(args.data)
    if args.cmd == "state":
        _print(svc.state(at=args.at))
    elif args.cmd == "order":
        _print(svc.order(args.order_id, at=args.at))
    elif args.cmd == "report":
        _print(svc.report(args.report_id, at=args.at))
    elif args.cmd == "quantities":
        _print(svc.quantities())
    elif args.cmd == "impacts":
        _print(svc.calibration_impacts())
    elif args.cmd == "arrange":
        _print(svc.arrange_for_calibration(args.calibration_id, at=args.at))
    elif args.cmd == "board":
        _print(svc.board(at=args.at))
    elif args.cmd == "retentions":
        from .acceptance import reconcile_retentions
        _print(reconcile_retentions(svc.repo, svc.inventory, create=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
