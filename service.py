"""兼容入口：等价于 ``labdesk.service``。"""
from labdesk.service import *  # noqa: F401,F403
from labdesk.service import LabService  # noqa: F401
from labdesk.inventory import ClaimError, Inventory  # noqa: F401
from labdesk.repository import Repository, load_packet  # noqa: F401
