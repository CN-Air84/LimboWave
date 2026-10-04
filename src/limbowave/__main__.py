"""``python -m limbowave`` 入口。

不带子命令时启动 GUI；``secret`` 子命令维护加密密钥库（见 ``cli.py``）。
"""

from __future__ import annotations

from limbowave.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
