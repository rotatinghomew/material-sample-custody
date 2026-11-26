"""仓库根命令入口：``python3 main.py ...`` 等价于 ``python3 -m labdesk ...``。"""
from labdesk.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
