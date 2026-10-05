"""应用主题：双调色板 + 字号缩放 + 集中式 QSS（Task 8.4 / §三）。

为什么集中：散落的 ``setStyleSheet`` 字面量会让配色/圆角/字号各自漂移，
改一处忘三处。这里定义唯一的设计令牌（颜色、圆角、字号），全部视图的
样式都从这里派生——视图只声明「我是谁」（objectName），不声明「我长什么样」。

**双调色板**：深色（默认）与浅色。令牌是**模块级可变全局**——
``set_palette()`` 更新它们，视图下次读 ``theme.BG_APP`` 拿到新值。
已经在构造时把颜色拼进内联样式的组件由上层刷新内联样式；
消息行在主题切换后重新渲染（``app.py`` 负责）。

**字号缩放**：所有字号来自令牌（``FS_BASE`` / ``FS_SMALL`` / ``FS_TINY``），
``set_font_scale()`` 统一缩放。视图不再写死 ``12px``——这是可访问性的前提。

**高 DPI**：Qt 6 默认启用高 DPI 缩放（``AA_EnableHighDpiScaling`` 已废弃），
因此这里不手动缩放像素值——手动缩放会和系统缩放叠加成双重放大。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from pathlib import Path

from limbowave.domain.appearance import AppearanceTheme, MaterialSettings

type ThemeName = str


@dataclass(frozen=True, slots=True)
class Palette:
    """一套配色。字段名与模块级令牌一一对应。"""

    name: str
    bg_app: str
    bg_surface: str
    bg_surface_hover: str
    bg_elevated: str
    border: str
    accent: str
    accent_hover: str
    accent_pressed: str
    text_primary: str
    text_secondary: str
    text_on_accent: str
    danger_bg: str
    danger_text: str
    warning: str
    # 代码块背景（与表面色区分，保证可读对比度）
    code_bg: str


DARK = Palette(
    name="dark",
    bg_app="#0f1115",
    bg_surface="#171a21",
    bg_surface_hover="#1e222b",
    bg_elevated="#22262f",
    border="#2a2f3a",
    accent="#5b6cff",
    accent_hover="#6d7dff",
    accent_pressed="#4a5ae0",
    text_primary="#e6e9ef",
    text_secondary="#9aa3b2",
    text_on_accent="#ffffff",
    danger_bg="#3a2226",
    danger_text="#f2b8c6",
    warning="#d9a03f",
    code_bg="#1c1f27",
)

# 浅色：白底 + 同色相强调色（保证与深色同一设计语言），文字用深灰而非纯黑
LIGHT = Palette(
    name="light",
    bg_app="#f6f7f9",
    bg_surface="#ffffff",
    bg_surface_hover="#eef1f5",
    bg_elevated="#e6eaf0",
    border="#d6dbe3",
    accent="#4a5ae0",
    accent_hover="#5b6cff",
    accent_pressed="#3a48c0",
    text_primary="#1c2027",
    text_secondary="#5c6675",
    text_on_accent="#ffffff",
    danger_bg="#fbe6ea",
    danger_text="#a3324a",
    warning="#8a5b12",
    code_bg="#eef1f5",
)

_PALETTES: dict[str, Palette] = {"dark": DARK, "light": LIGHT}

# ---------- 字号令牌（基准值；set_font_scale 会缩放它们） ----------

FS_TINY_BASE = 11
FS_SMALL_BASE = 12
FS_BASE_BASE = 13
FS_TITLE_BASE = 15

# ---------- 圆角（与字号无关） ----------

RADIUS_SM = 6
RADIUS_MD = 10
RADIUS_LG = 14
RADIUS_BUBBLE = 18  # 用户消息气泡

DEFAULT_FONT_FAMILY = (
    "'Segoe UI', 'Microsoft YaHei UI', 'PingFang SC', "
    "'Noto Sans CJK SC', 'WenQuanYi Micro Hei', sans-serif"
)
FONT_FAMILY = DEFAULT_FONT_FAMILY
FONT_MONO = "'Cascadia Code', 'JetBrains Mono', Consolas, monospace"

# ---------- 当前令牌（由 set_palette / set_font_scale 更新） ----------

BG_APP = DARK.bg_app
BG_SURFACE = DARK.bg_surface
BG_SURFACE_HOVER = DARK.bg_surface_hover
BG_ELEVATED = DARK.bg_elevated
BORDER = DARK.border
ACCENT = DARK.accent
ACCENT_HOVER = DARK.accent_hover
ACCENT_PRESSED = DARK.accent_pressed
TEXT_PRIMARY = DARK.text_primary
TEXT_SECONDARY = DARK.text_secondary
TEXT_ON_ACCENT = DARK.text_on_accent
DANGER_BG = DARK.danger_bg
DANGER_TEXT = DARK.danger_text
WARNING = DARK.warning
CODE_BG = DARK.code_bg
TAB_INDICATOR = DARK.accent

FS_TINY = FS_TINY_BASE
FS_SMALL = FS_SMALL_BASE
FS_BASE = FS_BASE_BASE
FS_TITLE = FS_TITLE_BASE

_current_palette = DARK
_current_scale = 1.0
_current_materials = MaterialSettings()
_background_active = False


def set_palette(name: ThemeName) -> Palette:
    """切换调色板：更新模块级令牌。返回新的调色板。"""
    global BG_APP, BG_SURFACE, BG_SURFACE_HOVER, BG_ELEVATED, BORDER
    global ACCENT, ACCENT_HOVER, ACCENT_PRESSED
    global TEXT_PRIMARY, TEXT_SECONDARY, TEXT_ON_ACCENT
    global DANGER_BG, DANGER_TEXT, WARNING, CODE_BG, _current_palette

    palette = _PALETTES.get(name, DARK)
    global _current_materials, _background_active, TAB_INDICATOR
    _current_materials = MaterialSettings()
    TAB_INDICATOR = palette.accent
    _background_active = False
    BG_APP = palette.bg_app
    BG_SURFACE = palette.bg_surface
    BG_SURFACE_HOVER = palette.bg_surface_hover
    BG_ELEVATED = palette.bg_elevated
    BORDER = palette.border
    ACCENT = palette.accent
    ACCENT_HOVER = palette.accent_hover
    ACCENT_PRESSED = palette.accent_pressed
    TEXT_PRIMARY = palette.text_primary
    TEXT_SECONDARY = palette.text_secondary
    TEXT_ON_ACCENT = palette.text_on_accent
    DANGER_BG = palette.danger_bg
    DANGER_TEXT = palette.danger_text
    WARNING = palette.warning
    CODE_BG = palette.code_bg
    _current_palette = palette
    return palette


def _rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    return int(value[:2], 16), int(value[2:4], 16), int(value[4:], 16)


def _blend(first: str, second: str, amount: float) -> str:
    left = _rgb(first)
    right = _rgb(second)
    values = tuple(round(a + (b - a) * amount) for a, b in zip(left, right, strict=True))
    return "#" + "".join(f"{value:02X}" for value in values)


def qss_alpha(color: str, opacity: float) -> str:
    red, green, blue = _rgb(color)
    return f"rgba({red}, {green}, {blue}, {max(0, min(255, round(opacity * 255)))})"


def apply_appearance_theme(definition: AppearanceTheme) -> Palette:
    """Derive LimboWave's design tokens from the complete appearance model."""
    colors = definition.colors
    dark = sum(_rgb(colors.background)) < 384
    text_on_accent = "#FFFFFF" if sum(_rgb(colors.accent)) < 500 else "#101318"
    palette = Palette(
        name=definition.id,
        bg_app=colors.background,
        bg_surface=colors.card,
        bg_surface_hover=_blend(colors.component, colors.text, 0.10),
        bg_elevated=colors.component,
        border=_blend(colors.component, colors.text, 0.20),
        accent=colors.accent,
        accent_hover=_blend(colors.accent, "#FFFFFF" if dark else "#000000", 0.12),
        accent_pressed=_blend(colors.accent, "#000000", 0.16),
        text_primary=colors.text,
        text_secondary=_blend(colors.text, colors.background, 0.38),
        text_on_accent=text_on_accent,
        danger_bg=colors.error,
        danger_text=_blend(colors.text, colors.error, 0.12),
        warning=colors.warning,
        code_bg=_blend(colors.component, colors.background, 0.35),
    )
    global _current_materials, _background_active, TAB_INDICATOR
    _current_materials = definition.materials
    TAB_INDICATOR = colors.tab_indicator
    _background_active = bool(definition.background.asset)
    _install_palette(palette)
    return palette


def _install_palette(palette: Palette) -> None:
    global BG_APP, BG_SURFACE, BG_SURFACE_HOVER, BG_ELEVATED, BORDER
    global ACCENT, ACCENT_HOVER, ACCENT_PRESSED
    global TEXT_PRIMARY, TEXT_SECONDARY, TEXT_ON_ACCENT
    global DANGER_BG, DANGER_TEXT, WARNING, CODE_BG, _current_palette
    BG_APP = palette.bg_app
    BG_SURFACE = palette.bg_surface
    BG_SURFACE_HOVER = palette.bg_surface_hover
    BG_ELEVATED = palette.bg_elevated
    BORDER = palette.border
    ACCENT = palette.accent
    ACCENT_HOVER = palette.accent_hover
    ACCENT_PRESSED = palette.accent_pressed
    TEXT_PRIMARY = palette.text_primary
    TEXT_SECONDARY = palette.text_secondary
    TEXT_ON_ACCENT = palette.text_on_accent
    DANGER_BG = palette.danger_bg
    DANGER_TEXT = palette.danger_text
    WARNING = palette.warning
    CODE_BG = palette.code_bg
    _current_palette = palette


def current_materials() -> MaterialSettings:
    return _current_materials


def tab_indicator_surface() -> str:
    return qss_alpha(TAB_INDICATOR, _current_materials.tab_indicator_opacity)


def tab_hover_surface() -> str:
    """选项卡悬浮底色。

    不用 BG_SURFACE_HOVER / BG_ELEVATED：派生调色板里悬浮色比组件色更重，
    悬浮项会比选中项更显眼。悬浮与选中都用正文色叠加、浓度递增，
    在任何底色（包括背景图）上都保持「静止 < 悬浮 < 选中」。
    """
    return qss_alpha(TEXT_PRIMARY, 0.06)


def tab_selected_surface() -> str:
    return qss_alpha(TEXT_PRIMARY, 0.12)


def tool_hover_surface() -> str:
    """输入区工具行小按钮（附件 +、权限档位）的悬浮底色。

    与页签悬浮同理用正文色叠加，在任何底色（包括背景图）上都可见；
    目标只有 30px 高、比整行页签更容易错过，浓度略高。
    """
    return qss_alpha(TEXT_PRIMARY, 0.08)


def card_surface() -> str:
    if _background_active and _current_materials.cards_enabled:
        return qss_alpha(BG_SURFACE, _current_materials.content_opacity)
    return BG_SURFACE


def control_surface(category: str, color: str | None = None) -> str:
    base = color or BG_SURFACE
    if _background_active and _current_materials.control_enabled(category):
        return qss_alpha(base, _current_materials.content_opacity)
    return base


def set_font_family(family: str) -> str:
    """Use only names verified by QFontDatabase; never interpolate user CSS."""
    from limbowave.ui.font_registry import select_family

    global FONT_FAMILY
    selected = select_family(family) if family else ""
    if any(character in selected for character in '"\\\n\r;{}'):
        selected = ""
    FONT_FAMILY = f'"{selected}"' if selected else DEFAULT_FONT_FAMILY
    return selected


def set_font_scale(scale: float) -> float:
    """缩放全部字号。``scale`` 夹在 0.8–1.6（过小不可读，过大撑坏布局）。"""
    global FS_TINY, FS_SMALL, FS_BASE, FS_TITLE, _current_scale
    clamped = max(0.8, min(1.6, scale))
    FS_TINY = round(FS_TINY_BASE * clamped)
    FS_SMALL = round(FS_SMALL_BASE * clamped)
    FS_BASE = round(FS_BASE_BASE * clamped)
    FS_TITLE = round(FS_TITLE_BASE * clamped)
    _current_scale = clamped
    return clamped


def pygments_style() -> str:
    """代码高亮配色，**跟随调色板**。

    深色的 monokai 样式在浅色底上会让代码变成浅色文字——不可读。
    浅色主题必须换成浅底样式。
    """
    return "monokai" if sum(_rgb(BG_APP)) < 384 else "friendly"


# 状态文字色：每种状态各备（深色主题, 浅色主题）两档。外观主题里的 warning/error
# 是背景向色值，直接当文字在深色底上读不清；这组在两类卡片底色上都保持 4.5:1 以上对比度。
_STATUS_TEXT = {
    "success": ("#3FB950", "#1A7F37"),
    "warning": ("#D9A03F", "#8A5B12"),
    "danger": ("#F85149", "#CF222E"),
}


def status_text_color(tone: str) -> str:
    """成功 / 警告 / 失败的文字色。按正文明暗选档：正文偏亮说明底色偏暗。"""
    on_dark, on_light = _STATUS_TEXT[tone]
    return on_dark if sum(_rgb(TEXT_PRIMARY)) >= 384 else on_light


def current_palette() -> Palette:
    return _current_palette


def current_font_scale() -> float:
    return _current_scale


def user_bubble_surface() -> str:
    """用户气泡底色：强调色的淡色调。半透明叠色，落在任何底色（包括背景图）上都柔和可读。"""
    return qss_alpha(ACCENT, 0.55)


def bubble_stylesheet(role: str) -> str:
    """消息气泡的 QSS 声明，调用方贴在气泡容器上。

    用户消息使用强调色淡色调；助手消息使用主题卡片表面与细边框；
    错误提示使用暖红背景。
    """
    if role == "error":
        return f"background: {DANGER_BG}; color: {DANGER_TEXT}; border-radius: {RADIUS_MD}px;"
    if role == "assistant":
        return (
            f"background: {card_surface()}; color: {TEXT_PRIMARY};"
            f" border: 1px solid {BORDER}; border-radius: {RADIUS_BUBBLE}px;"
        )
    return (
        f"background: {user_bubble_surface()}; color: {TEXT_PRIMARY};"
        f" border-radius: {RADIUS_BUBBLE}px;"
    )


def refresh_inline_styles(
    root: object,
    previous: Palette,
    old_sizes: tuple[int, ...],
    old_card_surface: str | None = None,
) -> None:
    """Refresh colors, material alpha and font sizes in existing inline QSS."""
    from PySide6.QtWidgets import QWidget

    if not isinstance(root, QWidget):
        return
    colors = {
        getattr(previous, field.name).lower(): getattr(_current_palette, field.name)
        for field in fields(Palette)
        if field.name != "name"
    }
    sizes = dict(zip(old_sizes, (FS_TINY, FS_SMALL, FS_BASE, FS_TITLE), strict=True))

    def remap_rgba(match: re.Match[str]) -> str:
        # rgba(R, G, B, A) 的底色若是旧调色板令牌，换成新令牌、浓度不动
        key = "#{:02x}{:02x}{:02x}".format(int(match[1]), int(match[2]), int(match[3]))
        replacement = colors.get(key)
        if replacement is None:
            return match.group()
        red, green, blue = _rgb(replacement)
        return f"rgba({red}, {green}, {blue}, {match[4]})"

    for widget in (root, *root.findChildren(QWidget)):
        css = widget.styleSheet()
        if not css:
            continue
        updated = css
        if old_card_surface:
            updated = re.sub(
                re.escape(old_card_surface), card_surface(), updated, flags=re.IGNORECASE
            )
        updated = re.sub(
            r"#[0-9a-fA-F]{6}", lambda m: colors.get(m.group().lower(), m.group()), updated
        )
        updated = re.sub(
            r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([0-9.]+)\s*\)",
            remap_rgba,
            updated,
        )
        updated = re.sub(
            r"font-size:\s*(\d+)px",
            lambda m: f"font-size: {sizes.get(int(m.group(1)), int(m.group(1)))}px",
            updated,
        )
        if updated != css:
            widget.setStyleSheet(updated)


def backdrop_chrome_active() -> bool:
    return _background_active


def chrome_surface(color: str) -> str:
    """Leave workspace chrome clear so the backdrop supplies its own blurred tint."""
    return "transparent" if _background_active else color


def combo_frame_colors() -> tuple[str, str, str]:
    """下拉框边框的（静止, 悬停/焦点, 展开）颜色。全部不透明，动画层可直接覆盖在 QSS 边框上。"""
    return TEXT_SECONDARY, _blend(TEXT_SECONDARY, TEXT_PRIMARY, 0.55), ACCENT


def app_stylesheet() -> str:
    """全局 QSS。切换主题/字号后重新设置一次，多数组件立即生效。"""
    icon_dir = (Path(__file__).parent / "assets").as_posix()
    button_surface = control_surface("buttons")
    input_surface = control_surface("text_inputs")
    selection_surface = control_surface("selections", BG_ELEVATED)
    item_surface = control_surface("item_views")
    scroll_surface = control_surface("scrollbars", BORDER)
    card = chrome_surface(card_surface())
    canvas = chrome_surface(BG_APP)
    hover = chrome_surface(BG_SURFACE_HOVER)
    elevated = chrome_surface(BG_ELEVATED)
    disabled = chrome_surface(BG_APP)
    button_surface = chrome_surface(button_surface)
    input_surface = chrome_surface(input_surface)
    selection_surface = chrome_surface(selection_surface)
    selection_border = qss_alpha(TEXT_SECONDARY, 0.55)
    combo_rest, combo_emphasis, combo_open = combo_frame_colors()
    item_surface = chrome_surface(item_surface)
    return f"""
/* ---------- 基础 ---------- */
QWidget {{
    background: {canvas};
    color: {TEXT_PRIMARY};
    font-family: {FONT_FAMILY};
    font-size: {FS_BASE}px;
}}
QToolTip {{
    background: {BG_ELEVATED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    padding: 4px 8px;
    border-radius: {RADIUS_SM}px;
}}

/* ---------- 按钮 ---------- */
QPushButton {{
    background: {button_surface};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    padding: 7px 16px;
}}
QPushButton:hover {{ background: {hover}; border-color: {TEXT_SECONDARY}; }}
QPushButton:pressed {{ background: {elevated}; }}
QPushButton:focus {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: {TEXT_SECONDARY}; background: {disabled}; border-color: {BORDER}; }}
QPushButton[accent="true"] {{
    background: {ACCENT};
    color: {TEXT_ON_ACCENT};
    border: none;
    font-weight: 600;
}}
QPushButton[accent="true"]:hover {{ background: {ACCENT_HOVER}; }}
QPushButton[accent="true"]:pressed {{ background: {ACCENT_PRESSED}; }}
QPushButton[accent="true"]:disabled {{ background: {BORDER}; color: {TEXT_SECONDARY}; }}
QPushButton[flat="true"] {{
    background: transparent;
    border: 1px solid transparent;
    color: {TEXT_SECONDARY};
}}
QPushButton[flat="true"]:hover {{ background: {hover}; color: {TEXT_PRIMARY}; }}
QPushButton[flat="true"]:focus {{ border-color: {ACCENT}; color: {TEXT_PRIMARY}; }}
/* 方形图标按钮（如输入框发送/停止）：去掉正文按钮的内边距，图标才能居中。 */
QPushButton[iconOnly="true"] {{ padding: 0px; margin: 0px; }}

/* ---------- 输入 ---------- */
QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QDateEdit {{
    background: {input_surface};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    min-height: 20px;
    padding: 7px 12px;
    margin: 0px;
    selection-background-color: {ACCENT};
    selection-color: {TEXT_ON_ACCENT};
}}
QTextEdit, QDateTimeEdit, QTimeEdit {{ margin: 0px; }}
QLineEdit:focus, QPlainTextEdit:focus, QDateEdit:focus {{
    border: 2px solid {ACCENT};
    padding: 6px 11px;
}}
QLineEdit:disabled, QPlainTextEdit:disabled {{
    background: {disabled};
    color: {TEXT_SECONDARY};
}}

/* 整数与小数数值框必须共享完全相同的主体和按钮区。 */
QSpinBox, QDoubleSpinBox {{
    padding: 7px 34px 7px 12px;
}}
QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 2px solid {ACCENT};
    padding: 6px 33px 6px 11px;
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    width: 22px;
    background: {selection_surface};
    border-left: 1px solid {BORDER};
}}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-position: top right;
    border-top-right-radius: {RADIUS_MD}px;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-position: bottom right;
    border-top: 1px solid {BORDER};
    border-bottom-right-radius: {RADIUS_MD}px;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {hover};
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url("{icon_dir}/spin-up.svg");
    width: 12px;
    height: 8px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url("{icon_dir}/spin-down.svg");
    width: 12px;
    height: 8px;
}}

/* 下拉框使用同规格右侧按钮分区，避免只剩一个没有箭头的输入框外壳。
   静止态用实色边框提示可点击；悬停只加深边框、不换底色。
   这里的伪状态是无动画兜底，状态间的颜色过渡由 theme_effects 的边框层负责。 */
QComboBox {{
    background: {selection_surface};
    border: 1px solid {combo_rest};
    border-radius: {RADIUS_MD}px;
    min-height: 20px;
    padding: 7px 34px 7px 12px;
    margin: 0px;
    selection-background-color: {ACCENT};
    selection-color: {TEXT_ON_ACCENT};
}}
QComboBox:hover, QComboBox:focus {{
    border-color: {combo_emphasis};
    border-radius: {RADIUS_MD}px;
}}
QComboBox:on {{
    border-color: {combo_open};
}}
QComboBox:disabled {{
    color: {TEXT_SECONDARY};
    border-color: {BORDER};
}}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 22px;
    background: {selection_surface};
    border: none;
    border-left: 1px solid {BORDER};
    border-top-right-radius: {RADIUS_MD}px;
    border-bottom-right-radius: {RADIUS_MD}px;
}}
QComboBox::down-arrow {{
    image: url("{icon_dir}/spin-down.svg");
    width: 12px;
    height: 8px;
}}
QComboBox::down-arrow:on {{ image: url("{icon_dir}/spin-up.svg"); }}
/* 未接入共享背景的独立弹层保留实体底色；popup_material 仅对已接入的表面局部覆盖。
   悬停项仍使用清晰的实体悬停面，不随底板透明。 */
QComboBox QAbstractItemView {{
    background: {BG_ELEVATED};
    border: 1px solid {selection_border};
    selection-background-color: {BG_SURFACE_HOVER};
    selection-color: {TEXT_PRIMARY};
    outline: none;
}}
QComboBox QAbstractItemView::item:hover, QComboBox QAbstractItemView::item:selected {{
    background: {BG_SURFACE_HOVER};
    color: {TEXT_PRIMARY};
}}

/* ---------- 列表 ---------- */
QListWidget {{
    background: transparent;
    border: none;
    outline: none;
}}
QListWidget::item {{
    border-radius: {RADIUS_MD}px;
    padding: 8px 10px;
    margin: 1px 2px;
}}
QListWidget::item:hover {{ background: {hover}; }}
QListWidget::item:selected {{ background: {elevated}; color: {TEXT_PRIMARY}; }}
/* 设置页名称列表使用更具体的选择器，确保条目背景按圆角裁切。 */
QListWidget#settingsNameList::item {{
    border-radius: {RADIUS_MD}px;
}}
QListWidget#settingsNameList::item:hover {{ background: {hover}; }}
QListWidget#settingsNameList::item:selected {{
    background: {elevated}; color: {TEXT_PRIMARY};
}}
QListWidget:focus {{ border: 1px solid {ACCENT}; border-radius: {RADIUS_MD}px; }}
QListWidget#settingsNameList, QListWidget#settingsNameList:focus {{ border: none; }}
QFrame#settingsCurrentIndicator {{
    background: {ACCENT};
    border: none;
    border-radius: 1px;
}}

/* 绑定列表是可选择的记录区域，不沿用导航列表的透明背景。 */
QListWidget#modelBindingsList {{
    background: {selection_surface};
    border: 1px solid {selection_border};
    border-radius: {RADIUS_MD}px;
    padding: 4px;
}}
QListWidget#modelBindingsList:focus {{ border-color: {ACCENT}; }}
QListWidget#modelBindingsList::item {{
    min-height: 24px;
    padding: 8px 10px;
    margin: 0px;
    border-radius: 0px;
    border-bottom: 1px solid {BORDER};
}}
QListWidget#modelBindingsList::item:hover {{ background: {BG_SURFACE_HOVER}; }}
QListWidget#modelBindingsList::item:selected {{
    background: {ACCENT};
    color: {TEXT_ON_ACCENT};
}}

/* ---------- 树 / 表格 ---------- */
QTreeWidget {{
    background: {item_surface};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    outline: none;
}}
QTreeWidget::item {{ padding: 3px 4px; }}
QTreeWidget::item:selected {{ background: {elevated}; color: {TEXT_PRIMARY}; }}
QHeaderView::section {{
    background: {item_surface};
    color: {TEXT_SECONDARY};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 8px;
    font-size: {FS_SMALL}px;
}}

/* ---------- 实际模型自建条目 ---------- */
#actualModelsScroll, #actualModelsContent {{ background: transparent; border: none; }}
QFrame#actualModelRow {{
    background: {card}; border: 1px solid {BORDER}; border-radius: {RADIUS_MD}px;
}}
#actualModelRow QLabel, #actualModelRow QCheckBox {{ background: transparent; border: none; }}
#actualModelsTitle {{ font-size: {FS_TITLE}px; font-weight: 600; border: none; }}
#actualModelName {{ font-weight: 600; }}
QPushButton#actualModelAdd {{ margin: 0px; padding: 7px 0px; }}
QCheckBox[capabilityTone="muted"] {{ color: {TEXT_SECONDARY}; }}
QCheckBox[capabilityTone="success"] {{ color: {status_text_color('success')}; }}
QCheckBox[capabilityTone="warning"] {{ color: {status_text_color('warning')}; }}
QCheckBox[capabilityTone="danger"] {{ color: {status_text_color('danger')}; }}

/* ---------- 设置二级选项卡 ---------- */
#appearanceSubtabHost, #appearanceSubtabStack, #appearancePage,
#endpointSubtabHost, #endpointSubtabStack, #endpointSubtabPage,
#modelSubtabHost, #modelSubtabStack, #modelSubtabPage,
#memorySubtabHost, #memorySubtabStack, #memoryPage,
#memoryPageScroll, #memoryPageScroll > QWidget > QWidget,
#appearancePageScroll, #appearancePageScroll > QWidget > QWidget {{
    background: transparent;
}}
QTabBar#memorySubtabs, QTabBar#modelSubtabs,
QTabBar#appearanceSubtabs, QTabBar#endpointSubtabs {{
    background: transparent;
}}
QTabBar#memorySubtabs::tab, QTabBar#modelSubtabs::tab,
QTabBar#appearanceSubtabs::tab, QTabBar#endpointSubtabs::tab {{
    background: transparent;
    color: {TEXT_SECONDARY};
    border: none;
    border-bottom: 2px solid transparent;
    border-top-left-radius: {RADIUS_SM}px;
    border-top-right-radius: {RADIUS_SM}px;
    padding: 9px 15px;
    margin: 0 2px;
}}
QTabBar#memorySubtabs::tab:hover, QTabBar#modelSubtabs::tab:hover,
QTabBar#appearanceSubtabs::tab:hover, QTabBar#endpointSubtabs::tab:hover {{
    color: {TEXT_PRIMARY};
    background: {tab_hover_surface()};
}}
QTabBar#memorySubtabs::tab:selected, QTabBar#modelSubtabs::tab:selected,
QTabBar#appearanceSubtabs::tab:selected, QTabBar#endpointSubtabs::tab:selected,
QTabBar#memorySubtabs::tab:selected:hover, QTabBar#modelSubtabs::tab:selected:hover,
QTabBar#appearanceSubtabs::tab:selected:hover, QTabBar#endpointSubtabs::tab:selected:hover {{
    color: {TEXT_PRIMARY};
    background: {tab_selected_surface()};
    border-bottom: 2px solid transparent;
    font-weight: 600;
}}
#memorySubtabIndicator,
#appearanceSubtabIndicator, #endpointSubtabIndicator, #modelSubtabIndicator {{
    background: {tab_indicator_surface()};
    border: none;
    border-radius: 1px;
}}
#memoryPageTitle, #appearancePageTitle {{
    color: {TEXT_PRIMARY};
    font-size: {FS_TITLE}px;
    font-weight: 600;
    background: transparent;
}}
#memoryPageHint, #appearancePageHint {{
    color: {TEXT_SECONDARY};
    font-size: {FS_SMALL}px;
    background: transparent;
    padding-bottom: 8px;
}}

/* ---------- 页签 ---------- */
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: {RADIUS_MD}px; top: -1px; }}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_SECONDARY};
    padding: 8px 16px;
    border-top-left-radius: {RADIUS_MD}px;
    border-top-right-radius: {RADIUS_MD}px;
}}
QTabBar::tab:selected {{ background: {card}; color: {TEXT_PRIMARY}; }}
QTabBar::tab:hover:!selected {{ color: {TEXT_PRIMARY}; }}
QTabBar::tab:focus {{ color: {TEXT_PRIMARY}; border-bottom: 2px solid {ACCENT}; }}

/* ---------- 滚动条 ---------- */
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {scroll_surface}; border-radius: 5px; min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{ background: {TEXT_SECONDARY}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: transparent; height: 10px; margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {scroll_surface}; border-radius: 5px; min-width: 32px;
}}
QScrollBar::handle:horizontal:hover {{ background: {TEXT_SECONDARY}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ---------- 分隔条 ---------- */
QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 1px; }}

/* ---------- 对话框与提示 ---------- */
QDialog, QMessageBox {{ background: {BG_APP}; }}
QMenu {{ background: {BG_SURFACE}; color: {TEXT_PRIMARY}; border: 1px solid {BORDER}; }}
QMessageBox QLabel {{ color: {TEXT_PRIMARY}; }}
/* 指示器由 checkbox_style 的代理样式绘制（方框 + 勾选过渡）。
   不要在这里写 QCheckBox::indicator：样式表一旦有该规则就会自己画，过渡随之失效。 */
QCheckBox {{ spacing: 8px; }}
QCheckBox:focus {{ color: {TEXT_PRIMARY}; }}
QLabel[hint="true"] {{ color: {TEXT_SECONDARY}; font-size: {FS_SMALL}px; }}
QProgressBar {{ background: {card}; border: none; border-radius: 3px; }}
"""
