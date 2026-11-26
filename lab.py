"""便捷/兼容入口：``import lab`` 即可使用整个领域引擎。

真正实现位于 :mod:`labdesk`；本模块只做导出与常见命名别名。
"""
from labdesk import *  # noqa: F401,F403
from labdesk import (  # noqa: F401
    ClaimError,
    Inventory,
    LabService,
    Repository,
    __version__,
    as_minutes,
    calibration_impacts,
    in_half_open,
    load_packet,
    plan_order,
    report_status,
    sender_overview,
)
from labdesk.acceptance import (  # noqa: F401
    check_applicability,
    material_requirement,
    reconcile_retentions,
    solve_units,
)
from labdesk.analysis import build_analysis  # noqa: F401
from labdesk.calibration import (  # noqa: F401
    attempt_validity,
    calibration_hits,
    invalid_attempt_ids,
)
from labdesk.reports import sender_board  # noqa: F401

# ---- 常见命名别名（便于不同调用习惯） ----
Service = LabService
Lab = LabService
Engine = LabService


def load(source=None):  # noqa: F821
    """加载报文并返回 :class:`LabService`；缺省读取演练快照。"""
    from pathlib import Path
    if source is None:
        source = Path(__file__).resolve().parent / "data" / "samples_20261208.json"
    return LabService(source)


def analyze(source=None):
    """返回快照的完整核对结论 dict。"""
    from pathlib import Path
    if source is None:
        source = Path(__file__).resolve().parent / "data" / "samples_20261208.json"
    return build_analysis(source)


def board(at=None, source=None):
    return load(source).board(at=at)


def affected_orders(source=None):
    svc = load(source)
    return sorted({oid for x in svc.calibration_impacts() for oid in x["affected_orders"]})


# 语义化函数别名
impact_analysis = calibration_impacts
find_affected_orders = affected_orders
check_acceptance = plan_order
feasibility = plan_order
check_report = report_status
status_overview = sender_overview
triage_board = sender_board
