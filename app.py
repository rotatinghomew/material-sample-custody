"""兼容入口：以 WSGI/ASGI 之外的标准库 HTTP 方式暴露联调服务。

- ``python3 app.py`` 启动在 8040；
- 测试可用 :func:`make_server` 取得绑定随机端口的 ``ThreadingHTTPServer``。
"""
from pathlib import Path

from labdesk.server import create_handler, run_server  # noqa: F401
from labdesk.service import LabService

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "samples_20261208.json"


def make_server(host: str = "127.0.0.1", port: int = 0, data_path: Path = DATA):
    from http.server import ThreadingHTTPServer
    service = LabService(data_path)
    return ThreadingHTTPServer((host, port), create_handler(service, Path(data_path)))


# 某些启动器寻找名为 app/application 的可调用对象；这里给出 handler 工厂。
def app(*_args, **_kwargs):  # pragma: no cover - 仅名称兼容
    return create_handler(LabService(DATA), DATA)


application = app


if __name__ == "__main__":
    run_server(host="127.0.0.1", port=8040, data_path=DATA)
