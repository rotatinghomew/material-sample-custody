"""兼容入口：聚合本任务的主要计算结论与服务。

可直接 ``from solution import solve, LabService``。
"""
from labdesk import *  # noqa: F401,F403
from labdesk import LabService  # noqa: F401
from labdesk.analysis import build_analysis  # noqa: F401
from labdesk.reports import sender_board  # noqa: F401
from labdesk.acceptance import reconcile_retentions  # noqa: F401

_DEFAULT = None


def _data_path():
    from pathlib import Path
    return Path(__file__).resolve().parent / "data" / "samples_20261208.json"


def solve(source=None, *, write: "str | None" = None):
    """返回快照的完整核对结论；给 ``write`` 时同时写 JSON 文件。"""
    import json
    path = source or _data_path()
    result = build_analysis(path)
    if write:
        with open(write, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
    return result


# 常见命名别名
analyze = solve


def board(at="14:20", source=None):
    return LabService(source or _data_path()).board(at=at)


def affected_orders(source=None):
    svc = LabService(source or _data_path())
    return sorted({o for x in svc.calibration_impacts() for o in x["affected_orders"]})
