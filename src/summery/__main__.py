"""``python -m summery`` 入口（等同 ``summery`` 命令）。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
