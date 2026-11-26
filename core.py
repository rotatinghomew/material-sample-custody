"""兼容入口：核心领域类型与纯函数。"""
from labdesk import *  # noqa: F401,F403
from labdesk.inventory import Inventory, Reservation, SampleView, ClaimError  # noqa: F401
from labdesk.repository import Repository, load_packet  # noqa: F401
from labdesk.timeutil import as_minutes, in_half_open, HM  # noqa: F401
from labdesk.calibration import calibration_impacts, calibration_hits, invalid_attempt_ids  # noqa: F401
from labdesk.acceptance import plan_order, solve_units, material_requirement  # noqa: F401
from labdesk.reports import report_status, sender_board, order_progress  # noqa: F401
