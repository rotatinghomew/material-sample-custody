# 让 pytest 从仓库根运行时也能导入 labdesk（unittest 下为空操作）。
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
