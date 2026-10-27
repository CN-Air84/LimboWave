"""One offline review entry for current shared OOBE proposals; never production onboarding."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QFontDatabase  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QFrame,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from limbowave.ui import theme  # noqa: E402
from tools.demos.voice_oobe_demo import VoiceOobeDemo  # noqa: E402


class JointOobeDemo(VoiceOobeDemo):
    """Inherit all shared proposal pages instead of maintaining a second copy."""

    def build_network(self) -> QWidget:
        page = super().build_network()
        layout = page.layout()
        assert isinstance(layout, QVBoxLayout)
        item = layout.takeAt(0)
        assert item is not None and item.layout() is not None
        content = QWidget()
        content.setObjectName("oobeColumn")
        content.setLayout(item.layout())
        content.setMinimumHeight(560)
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        layout.insertWidget(0, scroll, 1)
        self.network_scroll = scroll
        return page

    def apply_theme(self) -> None:
        super().apply_theme()
        page = self.pages.widget(self.indices["network"])
        for title in page.findChildren(QLabel, "oobeTitle"):
            title.ensurePolished()
            lines = max(1, len(title.text().splitlines()))
            title.setMinimumHeight(title.fontMetrics().lineSpacing() * lines + 8)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument("--screen", default="network")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    font = Path("C:/Windows/Fonts/msyh.ttc")
    if font.exists():
        QFontDatabase.addApplicationFont(str(font))
        theme.set_font_family("Microsoft YaHei")
    theme.set_palette("dark")
    window = JointOobeDemo()
    window.show_screen(args.screen if args.screen in window.indices else "network")
    window.show()
    if args.screenshots:
        from PySide6.QtTest import QTest

        args.screenshots.mkdir(parents=True, exist_ok=True)

        def capture() -> None:
            window.show_screen("network")
            QTest.qWait(100)
            window.grab().save(str(args.screenshots / "network-joint-dark.png"))
            window.network_tryout.configured.setChecked(True)
            window.network_tryout.request_search()
            window.network_tryout.approve()
            window.network_tryout.request_page()
            window.network_tryout.approve()
            QTest.qWait(80)
            window.grab().save(str(args.screenshots / "network-joint-article-dark.png"))
            window.toggle_theme()
            window.resize(1000, 760)
            QTest.qWait(100)
            window.grab().save(str(args.screenshots / "network-joint-minimum-light.png"))
            window.close()
            app.quit()

        QTimer.singleShot(120, capture)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
