"""「正在构思……N秒」提示的渲染自测。

驱动 ChatView 走一轮真实流程（发送 → 构思计秒 → 思考流 → 首个正文落字），
在三个阶段各截一张图，肉眼检查提示的位置、样式与收起时机。

用法：
    python scripts/composing_hint_probe.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dist" / "composing_hint"
OUT.mkdir(parents=True, exist_ok=True)

RENDER = r"""
import sys
from pathlib import Path
sys.path.insert(0, "__SRC__")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from limbowave.ui import theme
from limbowave.ui.chat_view import ChatView

app = QApplication([])
app.setStyleSheet(theme.app_stylesheet())
view = ChatView()
view.resize(900, 640)
view.show()

stage = {"n": 0}

def snap(name: str) -> None:
    view.grab().save(str(Path("__OUT__") / name))

view.set_available(True)
view.add_user_message("帮我看看这段报错")
view.set_busy(True)          # 预立卡 + 「正在构思……0秒」
view.set_status("生成中…")

def t1():
    snap("1-composing.png")   # 构思计秒中（等 2 秒后应为「2秒」）
def t2():
    view.append_thinking_delta("用户贴了一段报错，我先判断原因……")
    snap("2-thinking.png")    # 思考流期间提示仍在计秒
def t3():
    view.append_assistant_delta("这个报错来自配置文件里的字段名拼写。")
    view.end_assistant("这个报错来自配置文件里的字段名拼写。", "m1")
    view.set_busy(False)
    snap("3-settled.png")     # 落字后提示应收起
    app.quit()

QTimer.singleShot(2000, t1)
QTimer.singleShot(2200, t2)
QTimer.singleShot(2400, t3)
stage["timer"] = True
app.exec()
"""

if __name__ == "__main__":
    src = RENDER.replace("__SRC__", str(ROOT / "src")).replace("__OUT__", str(OUT))
    sys.path.insert(0, str(ROOT / "src"))
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    exec(compile(src, "composing_hint_render", "exec"), {"__name__": "__render__"})
    print(f"截图输出：{OUT}")
