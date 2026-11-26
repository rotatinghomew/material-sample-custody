"""标准库 JSON API + 静态报文/看板服务。

路由（GET）：

``/api/analysis``                 完整快照核对结论
``/api/state?at=HH:MM``           数量/校准/委托总览
``/api/quantities``               子样数量分层
``/api/impacts``                  校准影响
``/api/retentions``               留样归属求解
``/api/orders[?at=]``、``/api/order/O11[?at=]``
``/api/reports``、``/api/report/R1[?at=]``
``/samples_20261208.json``        原始交换报文（与只读 http.server 同路径）

POST ``/api/message`` 可投递交换报文或同编号更正回执。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .analysis import build_analysis
from .service import LabService

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"
DEFAULT_DATA = DATA_DIR / "samples_20261208.json"


def create_handler(service: LabService, data_path: Path):
    analysis_cache = {"value": None}
    lock = threading.Lock()

    def analysis():
        with lock:
            if analysis_cache["value"] is None:
                analysis_cache["value"] = build_analysis(data_path)
            return analysis_cache["value"]

    class Handler(BaseHTTPRequestHandler):
        server_version = "labdesk/1.0"

        def log_message(self, fmt, *args):  # 保持测试输出干净
            pass

        def _json(self, payload, status: int = 200):
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path, content_type: str):
            if not path.is_file():
                self._json({"error": "not_found", "path": str(path.name)}, 404)
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            at = qs.get("at", [None])[0]
            try:
                if path == "/api/analysis":
                    self._json(analysis())
                elif path == "/api/state":
                    self._json(service.state(at=at))
                elif path == "/api/quantities":
                    self._json(service.quantities())
                elif path == "/api/impacts":
                    self._json(service.calibration_impacts())
                elif path.startswith("/api/arrange/"):
                    kid = path.rsplit("/", 1)[-1]
                    self._json(service.arrange_for_calibration(kid, at=at))
                elif path == "/api/retentions":
                    from .acceptance import reconcile_retentions
                    self._json(reconcile_retentions(service.repo, service.inventory))
                elif path == "/api/orders":
                    self._json(service.overview(at=at))
                elif path == "/api/board":
                    self._json(service.board(at=at))
                elif path.startswith("/api/order/"):
                    self._json(service.order(path.rsplit("/", 1)[-1], at=at))
                elif path == "/api/reports":
                    self._json([
                        service.report(r["id"], at=at)
                        for r in service.repo.rows("reports")
                    ])
                elif path.startswith("/api/report/"):
                    self._json(service.report(path.rsplit("/", 1)[-1], at=at))
                elif path == "/samples_20261208.json":
                    self._file(data_path, "application/json; charset=utf-8")
                elif path == "/" or path == "/index.html":
                    self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
                elif path.startswith("/static/"):
                    name = Path(path.removeprefix("/static/")).name
                    self._file(WEB_DIR / name, "application/octet-stream")
                elif path == "/api":
                    self._json({"endpoints": _ENDPOINTS})
                else:
                    self._json({"error": "not_found", "path": path, "see": "/api"}, 404)
            except KeyError as exc:
                self._json({"error": "unknown_id", "detail": str(exc)}, 404)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": type(exc).__name__, "detail": str(exc)}, 400)

        def do_POST(self):  # noqa: N802
            parsed = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                message = json.loads(raw.decode("utf-8") or "{}")
                if parsed.path == "/api/message":
                    result = service.load_message(message)
                    analysis_cache["value"] = None
                    self._json(result, 201)
                elif parsed.path == "/api/accept":
                    result = service.accept_order(
                        message.get("order"), at=message.get("at"),
                        reserve_tests=bool(message.get("reserve_tests")),
                    )
                    self._json(_jsonable(result), 201)
                elif parsed.path == "/api/occupy":
                    rev = service.inventory.occupy(**{
                        k: message[k] for k in ("sample_id", "units") if k in message
                    }, order=message.get("order"), kind=message.get("kind", "booking"),
                        reservation_id=message.get("reservation_id"))
                    self._json(rev.to_dict(), 201)
                else:
                    self._json({"error": "not_found", "path": parsed.path}, 404)
            except Exception as exc:  # noqa: BLE001
                status = 409 if exc.__class__.__name__ == "ClaimError" else 400
                self._json({"error": type(exc).__name__, "detail": str(exc),
                            "details": getattr(exc, "details", None)}, status)

    return Handler


def _jsonable(result: dict) -> dict:
    plan = result.get("plan")
    return {
        "accepted": result.get("accepted"),
        "reservations": result.get("reservations"),
        "plan_status": (plan or {}).get("status"),
        "plan": plan,
    }


_ENDPOINTS = [
    "GET /api/analysis", "GET /api/state?at=HH:MM", "GET /api/quantities",
    "GET /api/impacts", "GET /api/retentions", "GET /api/orders?at=",
    "GET /api/order/{id}?at=", "GET /api/reports?at=", "GET /api/report/{id}?at=",
    "POST /api/message", "POST /api/accept", "POST /api/occupy",
    "GET /samples_20261208.json",
]


def run_server(*, host: str = "127.0.0.1", port: int = 8040,
               data_path: Optional[Path] = None) -> ThreadingHTTPServer:
    data_path = Path(data_path or DEFAULT_DATA)
    service = LabService(data_path)
    httpd = ThreadingHTTPServer((host, port), create_handler(service, data_path))
    print(f"labdesk API on http://{host}:{port}/api  (data {data_path})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return httpd


if __name__ == "__main__":
    run_server()
