"""兼容入口：领域引擎常用 API。"""
from labdesk import *  # noqa: F401,F403
from labdesk import (  # noqa: F401
    LabService, Inventory, Repository, ClaimError, load_packet,
    plan_order, report_status, calibration_impacts,
)
from labdesk.analysis import build_analysis  # noqa: F401

Engine = LabService


def create_engine(source=None):
    from pathlib import Path
    if source is None:
        source = Path(__file__).resolve().parent / "data" / "samples_20261208.json"
    return LabService(source)
