"""高 DPI 适配自测（Task 8.4 的剩余部分）。

不修改系统设置——而是用 Qt 的缩放因子/DPI 覆盖在**多种倍率下真实渲染**
主窗口并截图，检查布局是否被裁切/重叠。这是「不改用户环境也能验证」的做法。

用法：
    python scripts/hidpi_probe.py            # 默认测 100%/125%/150%/200%
    python scripts/hidpi_probe.py 1.75       # 单独测某个倍率
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dist" / "hidpi"

# 每个倍率用独立子进程：Qt 的缩放因子必须在 QApplication 之前设置
SCALES = ("1.0", "1.25", "1.5", "2.0")

RENDER = """
import os, sys
from pathlib import Path
sys.path.insert(0, "__SRC__")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from limbowave.ui import theme
from limbowave.ui.main_window import MainWindow
from limbowave.domain.tool_step import ToolStep, ToolStatus
from limbowave.application.services.preferences_service import Preferences

app = QApplication([])
app.setStyleSheet(theme.app_stylesheet())
w = MainWindow()
w.resize(1280, 800)
w.sidebar.show_conversations([("c1", "部署讨论", 4), ("c2", "周末计划", 1)])
w.chat.set_available(True)
w.toolbar.set_session_available(True)
w.toolbar.set_model_info("deepseek-chat", "relay-a")
w.toolbar.set_thinking_level("medium")
w.toolbar.set_context_usage("约 62%（79000/128000 tokens，估算）")
w.chat.add_user_message("看下部署顺序，顺便确认密钥怎么管")
w.chat.begin_assistant()
NL = chr(10)
w.chat.end_assistant(
    "建议：" + NL + NL + "1. 先起数据库" + NL + "2. 再起应用" + NL + NL
    + "```bash" + NL + "docker compose up -d db" + NL + "```"
)
row = w.chat._rows[-1]
row.set_tool_steps((ToolStep(tool_call_id="c1", name="read", status=ToolStatus.OK,
                             duration_ms=12, args={"path": "compose.yml"},
                             result_summary="services: db, app"),))
w.chat.set_status("就绪 · deepseek-chat")
w.show()

def shot():
    target = Path("__TARGET__")
    target.parent.mkdir(parents=True, exist_ok=True)
    pm = w.grab()
    pm.save(str(target))
    screen = app.primaryScreen()
    print("scale=__SCALE__ dpr=%s logical_dpi=%s saved=%sx%s" % (
        screen.devicePixelRatio(), round(screen.logicalDotsPerInch(), 1),
        pm.width(), pm.height()))
    app.quit()

QTimer.singleShot(700, shot)
app.exec()
"""


def render(scale: str) -> None:
    env = dict(os.environ)
    # 只用 Qt 的因子覆盖，不动系统缩放设置
    env["QT_SCALE_FACTOR"] = scale
    env["QT_ENABLE_HIGHDPI_SCALING"] = "1"
    target = OUT / f"scale-{scale.replace('.', '_')}.png"
    # 显式占位符替换：被嵌入的 Qt 代码里全是花括号，不能用 str.format
    source = (
        RENDER.replace("__SRC__", str(ROOT / "src").replace("\\", "/"))
        .replace("__TARGET__", str(target).replace("\\", "/"))
        .replace("__SCALE__", scale)
    )
    completed = subprocess.run(
        [sys.executable, "-c", source], env=env, capture_output=True, text=True, cwd=str(ROOT)
    )
    output = (completed.stdout or "").strip().splitlines()
    print(f"scale={scale}: {output[-1] if output else '(无输出)'}")
    if completed.returncode != 0:
        print((completed.stderr or "")[-800:])


def main() -> int:
    scales = sys.argv[1:] or list(SCALES)
    print(f"高 DPI 自测：{', '.join(scales)}")
    for scale in scales:
        render(scale)
    print(f"截图目录：{OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
