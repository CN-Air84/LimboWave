"""Immutable appearance-theme model used by LimboWave's PySide6 renderer.

The model deliberately contains no Qt objects. Built-ins are code-owned and
custom themes are serialized by the application service.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any

CONTROL_CATEGORIES = ("buttons", "text_inputs", "selections", "item_views", "scrollbars")
BACKGROUND_FIT_MODES = ("cover", "contain", "stretch", "tile")
BACKGROUND_POSITIONS = (
    "top-left",
    "top",
    "top-right",
    "left",
    "center",
    "right",
    "bottom-left",
    "bottom",
    "bottom-right",
)
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def normalize_color(value: str) -> str:
    text = str(value).strip()
    if not _HEX.fullmatch(text):
        raise ValueError(f"invalid color: {value!r}")
    return text.upper()


def _fraction(value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError("value must be between 0 and 1")
    return number


def _radius(value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 64.0:
        raise ValueError("radius must be between 0 and 64")
    return number


@dataclass(frozen=True, slots=True)
class ThemeColors:
    background: str
    card: str
    component: str
    accent: str
    text: str
    info: str
    warning: str
    error: str
    tab_indicator: str

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, normalize_color(getattr(self, name)))

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> ThemeColors:
        return cls(**{name: str(values[name]) for name in cls.__dataclass_fields__})


@dataclass(frozen=True, slots=True)
class BackgroundSettings:
    asset: str = ""
    fit_mode: str = "cover"
    position: str = "center"
    image_opacity: float = 1.0
    mask_color: str = "#000000"
    mask_opacity: float = 0.0

    def __post_init__(self) -> None:
        if self.fit_mode not in BACKGROUND_FIT_MODES:
            raise ValueError(f"unsupported background fit: {self.fit_mode}")
        if self.position not in BACKGROUND_POSITIONS:
            raise ValueError(f"unsupported background position: {self.position}")
        object.__setattr__(self, "asset", str(self.asset).replace("\\", "/")[:512])
        object.__setattr__(self, "image_opacity", _fraction(self.image_opacity))
        object.__setattr__(self, "mask_color", normalize_color(self.mask_color))
        object.__setattr__(self, "mask_opacity", _fraction(self.mask_opacity))

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> BackgroundSettings:
        values = values or {}
        return cls(
            asset=str(values.get("asset", "")),
            fit_mode=str(values.get("fit_mode", "cover")),
            position=str(values.get("position", "center")),
            image_opacity=float(values.get("image_opacity", 1.0)),
            mask_color=str(values.get("mask_color", "#000000")),
            mask_opacity=float(values.get("mask_opacity", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class ControlSettings:
    buttons: bool = True
    text_inputs: bool = True
    selections: bool = True
    item_views: bool = True
    scrollbars: bool = True

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> ControlSettings:
        values = values or {}
        return cls(**{name: bool(values.get(name, True)) for name in CONTROL_CATEGORIES})


@dataclass(frozen=True, slots=True)
class MaterialSettings:
    content_enabled: bool = True
    content_opacity: float = 0.78
    content_blur_radius: float = 18.0
    cards_enabled: bool = True
    controls_master_enabled: bool = True
    controls: ControlSettings = field(default_factory=ControlSettings)
    sidebar_enabled: bool = True
    sidebar_opacity: float = 0.82
    sidebar_blur_radius: float = 20.0
    tab_indicator_opacity: float = 0.7
    hover_suspend_enabled: bool = True
    hover_enter_ms: int = 90
    hover_restore_ms: int = 180

    def __post_init__(self) -> None:
        object.__setattr__(self, "content_opacity", _fraction(self.content_opacity))
        object.__setattr__(self, "content_blur_radius", _radius(self.content_blur_radius))
        object.__setattr__(self, "sidebar_opacity", _fraction(self.sidebar_opacity))
        object.__setattr__(self, "sidebar_blur_radius", _radius(self.sidebar_blur_radius))
        object.__setattr__(self, "tab_indicator_opacity", _fraction(self.tab_indicator_opacity))
        for name in ("hover_enter_ms", "hover_restore_ms"):
            value = int(getattr(self, name))
            if not 0 <= value <= 60_000:
                raise ValueError(f"{name} must be between 0 and 60000")
            object.__setattr__(self, name, value)

    def control_enabled(self, category: str) -> bool:
        return self.controls_master_enabled and bool(getattr(self.controls, category))

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> MaterialSettings:
        values = values or {}
        return cls(
            content_enabled=bool(values.get("content_enabled", True)),
            content_opacity=float(values.get("content_opacity", 0.78)),
            content_blur_radius=float(values.get("content_blur_radius", 18.0)),
            cards_enabled=bool(values.get("cards_enabled", True)),
            controls_master_enabled=bool(values.get("controls_master_enabled", True)),
            controls=ControlSettings.from_mapping(values.get("controls")),
            sidebar_enabled=bool(values.get("sidebar_enabled", True)),
            sidebar_opacity=float(values.get("sidebar_opacity", 0.82)),
            sidebar_blur_radius=float(values.get("sidebar_blur_radius", 20.0)),
            tab_indicator_opacity=float(values.get("tab_indicator_opacity", 0.7)),
            hover_suspend_enabled=bool(values.get("hover_suspend_enabled", True)),
            hover_enter_ms=int(values.get("hover_enter_ms", 90)),
            hover_restore_ms=int(values.get("hover_restore_ms", 180)),
        )


@dataclass(frozen=True, slots=True)
class TextGlowSettings:
    enabled: bool = False
    minimum_intensity: float = 0.18
    maximum_intensity: float = 0.55
    minimum_radius: float = 1.5
    maximum_radius: float = 6.0

    def __post_init__(self) -> None:
        minimum = _fraction(self.minimum_intensity)
        maximum = _fraction(self.maximum_intensity)
        min_radius = _radius(self.minimum_radius)
        max_radius = _radius(self.maximum_radius)
        if minimum > maximum or min_radius > max_radius:
            raise ValueError("text glow minimum cannot exceed maximum")
        object.__setattr__(self, "minimum_intensity", minimum)
        object.__setattr__(self, "maximum_intensity", maximum)
        object.__setattr__(self, "minimum_radius", min_radius)
        object.__setattr__(self, "maximum_radius", max_radius)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> TextGlowSettings:
        values = values or {}
        return cls(
            enabled=bool(values.get("enabled", False)),
            minimum_intensity=float(values.get("minimum_intensity", 0.18)),
            maximum_intensity=float(values.get("maximum_intensity", 0.55)),
            minimum_radius=float(values.get("minimum_radius", 1.5)),
            maximum_radius=float(values.get("maximum_radius", 6.0)),
        )

    def values_for_size(
        self, size: float, minimum_size: float, maximum_size: float
    ) -> tuple[float, float]:
        if maximum_size <= minimum_size:
            factor = 0.0
        else:
            factor = max(0.0, min(1.0, (size - minimum_size) / (maximum_size - minimum_size)))
        intensity = self.minimum_intensity + factor * (
            self.maximum_intensity - self.minimum_intensity
        )
        radius = self.minimum_radius + factor * (self.maximum_radius - self.minimum_radius)
        return intensity, radius


@dataclass(frozen=True, slots=True)
class AppearanceTheme:
    id: str
    name: str
    colors: ThemeColors
    background: BackgroundSettings = field(default_factory=BackgroundSettings)
    materials: MaterialSettings = field(default_factory=MaterialSettings)
    text_glow: TextGlowSettings = field(default_factory=TextGlowSettings)

    @property
    def readonly(self) -> bool:
        return self.id.startswith("builtin:")

    def with_updates(self, **changes: Any) -> AppearanceTheme:
        return replace(self, **changes)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, values: Mapping[str, Any]) -> AppearanceTheme:
        theme_id = str(values.get("id", ""))
        name = str(values.get("name", "")).strip()
        try:
            normalized_id = str(uuid.UUID(theme_id))
        except ValueError as exc:
            raise ValueError("invalid custom theme identity") from exc
        if normalized_id != theme_id.lower() or not 1 <= len(name) <= 48:
            raise ValueError("invalid custom theme identity")
        return cls(
            id=theme_id,
            name=name,
            colors=ThemeColors.from_mapping(values.get("colors", {})),
            background=BackgroundSettings.from_mapping(values.get("background")),
            materials=MaterialSettings.from_mapping(values.get("materials")),
            text_glow=TextGlowSettings.from_mapping(values.get("text_glow")),
        )


DARK_COLORS = ThemeColors(
    "#0F1115",
    "#171A21",
    "#22262F",
    "#5B6CFF",
    "#E6E9EF",
    "#263B55",
    "#5B431C",
    "#51252E",
    "#5B6CFF",
)


def _builtin(
    slug: str, name: str, colors: tuple[str, ...], *, dark: bool = False
) -> AppearanceTheme:
    materials = MaterialSettings(
        content_opacity=0.72 if dark else 0.82,
        sidebar_opacity=0.76 if dark else 0.86,
    )
    return AppearanceTheme(f"builtin:{slug}", name, ThemeColors(*colors), materials=materials)


BUILTIN_THEMES = (
    AppearanceTheme("builtin:limbowave-dark", "灵波夜", DARK_COLORS),
    _builtin(
        "clear-day",
        "清昼",
        (
            "#EEF3F8",
            "#FFFFFF",
            "#F8FAFD",
            "#3E76D1",
            "#202A35",
            "#DCEBFA",
            "#FFF0D1",
            "#FBE0E2",
            "#3E76D1",
        ),
    ),
    _builtin(
        "morning-mist-blue",
        "晨雾蓝",
        (
            "#E8F1F8",
            "#F8FCFF",
            "#F1F7FC",
            "#3977B8",
            "#203142",
            "#D5EAF8",
            "#FBEBCF",
            "#F8DDDF",
            "#3977B8",
        ),
    ),
    _builtin(
        "green-bamboo",
        "青竹",
        (
            "#EAF3EC",
            "#FAFDF9",
            "#F1F8F1",
            "#3F8563",
            "#24352C",
            "#DCEEE6",
            "#F7EBCB",
            "#F4DDDA",
            "#3F8563",
        ),
    ),
    _builtin(
        "amber-paper",
        "琥珀纸",
        (
            "#F6EEDC",
            "#FFFDF6",
            "#FAF4E6",
            "#A46D2D",
            "#3A3025",
            "#E4ECF3",
            "#F9E1AE",
            "#F3D9D4",
            "#A46D2D",
        ),
    ),
    _builtin(
        "scarlet-cherry",
        "绯樱",
        (
            "#F8EAEF",
            "#FFF9FB",
            "#FCEFF3",
            "#B65373",
            "#3B2730",
            "#E3E8F7",
            "#F8E8C9",
            "#F7D9E0",
            "#B65373",
        ),
    ),
    _builtin(
        "lavender-mist",
        "薰衣草雾",
        (
            "#F0EBF8",
            "#FCFAFF",
            "#F5F0FB",
            "#7661B1",
            "#302A3F",
            "#E1E3F7",
            "#F5E7CA",
            "#F3DDE5",
            "#7661B1",
        ),
    ),
    _builtin(
        "sea-salt-cyan",
        "海盐青",
        (
            "#E7F3F2",
            "#F9FEFD",
            "#EFF8F7",
            "#317E83",
            "#213536",
            "#D5ECEC",
            "#F5E9C9",
            "#F1DDDC",
            "#317E83",
        ),
    ),
)
BUILTIN_BY_ID = {theme.id: theme for theme in BUILTIN_THEMES}
DEFAULT_THEME_ID = BUILTIN_THEMES[0].id


def replace_controls(settings: MaterialSettings, **changes: bool) -> MaterialSettings:
    return replace(settings, controls=replace(settings.controls, **changes))
