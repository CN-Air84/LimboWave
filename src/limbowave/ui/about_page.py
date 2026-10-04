"""关于页面：只读展示关于内容，不读取私密数据或发起网络请求。

内容来自 :mod:`limbowave.ui.about_content`：默认是占位文案，打包时可通过注入的
``about.json`` 覆盖（见 ``scripts/packager.py``），开发态可用 ``LIMBOWAVE_ABOUT``
环境变量指向一个 about.json 预览注入效果。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from limbowave.ui import theme
from limbowave.ui.about_content import AboutContent, AboutSection, default_content, load_embedded


def _label(text: str, role: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("aboutRole", role)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    if role == "value":
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
    return label


class AboutPage(QScrollArea):
    """可独立使用、随主题更新的只读关于页。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        content: AboutContent | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("aboutPage")
        self.setAccessibleName("关于 LimboWave / 灵波")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # 未显式给内容时按约定位置发现注入文件；无论如何都与默认占位合并，
        # 保证未填写的条目仍然显示「待填写」占位而不是空白。
        embedded = content if content is not None else load_embedded()
        self._content = embedded.merged(default_content())

        content_widget = QWidget()
        content_widget.setObjectName("aboutContent")
        root = QVBoxLayout(content_widget)
        root.setContentsMargins(0, 0, 12, 0)
        root.setSpacing(16)
        self.setWidget(content_widget)

        hero = QFrame()
        hero.setProperty("aboutCard", True)
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(24, 22, 24, 22)
        hero_layout.setSpacing(10)
        hero_layout.addWidget(_label("ABOUT / 关于灵波", "eyebrow"))
        brand = _label("LimboWave", "brand")
        brand.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        hero_layout.addWidget(brand)
        hero_layout.addWidget(_label(self._content.tagline or "待填写", "description"))
        hero_layout.addWidget(_label(self._content.notice or "待填写", "notice"))
        root.addWidget(hero)

        for section in self._content.sections:
            root.addWidget(self._section_card(section))

        footer = _label(self._content.footer or "待填写", "description")
        footer.setObjectName("aboutFooter")
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(footer)
        root.addStretch(1)
        self.restyle()

    @staticmethod
    def _section_card(section: AboutSection) -> QFrame:
        card = QFrame()
        card.setProperty("aboutCard", True)
        card.setAccessibleName(section.title)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        layout.addWidget(_label(section.title, "sectionTitle"))
        layout.addWidget(_label(section.description, "description"))

        form = QFormLayout()
        form.setContentsMargins(0, 4, 0, 4)
        form.setHorizontalSpacing(24)
        form.setVerticalSpacing(12)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for field in section.fields:
            name = _label(field.label, "fieldTitle")
            value = _label(field.value or "待填写", "value")
            value.setAccessibleName(field.label)
            name.setBuddy(value)
            form.addRow(name, value)
        layout.addLayout(form)

        for action in section.actions:
            button = QPushButton(f"{action}（待接入）")
            button.setEnabled(False)
            button.setToolTip("这是预留入口，功能尚未接入，不会联网或打开外部页面。")
            layout.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
        return card

    def restyle(self) -> None:
        """使用当前主题令牌；颜色、材质或字号变化后可原地刷新。"""
        css = f"""
            #aboutPage, #aboutContent {{ background: transparent; border: none; }}
            #aboutPage QFrame[aboutCard="true"] {{
                background: {theme.card_surface()}; border: 1px solid {theme.BORDER};
                border-radius: {theme.RADIUS_LG}px;
            }}
            #aboutPage QLabel {{ background: transparent; border: none;
                color: {theme.TEXT_PRIMARY}; font-size: {theme.FS_BASE}px; }}
            #aboutPage QLabel[aboutRole="eyebrow"] {{ color: {theme.ACCENT};
                font-size: {theme.FS_SMALL}px; font-weight: 600; }}
            #aboutPage QLabel[aboutRole="brand"] {{
                font-size: {theme.FS_TITLE * 2}px; font-weight: 600; }}
            #aboutPage QLabel[aboutRole="sectionTitle"] {{
                font-size: {theme.FS_TITLE}px; font-weight: 600; }}
            #aboutPage QLabel[aboutRole="description"],
            #aboutPage QLabel[aboutRole="value"] {{ color: {theme.TEXT_SECONDARY}; }}
            #aboutPage QLabel[aboutRole="description"],
            #aboutPage QLabel[aboutRole="notice"] {{ font-size: {theme.FS_SMALL}px; }}
            #aboutPage QLabel[aboutRole="notice"] {{ color: {theme.ACCENT}; }}
            #aboutPage QPushButton:disabled {{ background: {theme.BG_APP};
                color: {theme.TEXT_SECONDARY}; border: 1px solid {theme.BORDER};
                border-radius: {theme.RADIUS_SM}px; padding: 8px 12px;
                font-size: {theme.FS_SMALL}px; }}
        """
        if css != self.styleSheet():
            self.setStyleSheet(css)
