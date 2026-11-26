"""共用实验室送检、样品占用与复测承诺领域引擎。

仅使用 Python 3.11 标准库。所有时刻在演练数据中均为 2026-12-08 (UTC+08:00)
当天的 ``HH:MM`` 字符串；跨天场景可直接使用完整 ISO 字符串。
"""

from .timeutil import HM, as_minutes, in_half_open
from .repository import Repository, load_packet
from .inventory import Inventory, SampleView
from .calibration import calibration_impacts, attempt_validity
from .acceptance import plan_order, PlanStatus
from .reports import report_status, sender_overview
from .service import LabService, ClaimError

__all__ = [
    "HM",
    "as_minutes",
    "in_half_open",
    "Repository",
    "load_packet",
    "Inventory",
    "SampleView",
    "calibration_impacts",
    "attempt_validity",
    "plan_order",
    "PlanStatus",
    "report_status",
    "sender_overview",
    "LabService",
    "ClaimError",
]

__version__ = "1.0.0"
