"""界面偏好（Task 8.4 / §三.4）。

偏好是**普通配置**（不含密钥），落在 ``<data_root>/preferences.json``。
与 ``config.json`` 分开：站点/模型是领域配置，界面外观是本地使用偏好，
两者的变更频率与语义都不同。

字段缺失或损坏时回退到默认值——界面偏好不该让应用起不来。
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_THEME = "dark"
DEFAULT_FONT_SCALE = 1.0
SUPPORTED_THEMES = ("dark", "light")
FONT_SCALES = (0.9, 1.0, 1.15, 1.3, 1.5)


@dataclass(frozen=True, slots=True)
class Preferences:
    """界面偏好。不可变；改动走 :meth:`PreferencesService.save`。"""

    theme: str = DEFAULT_THEME
    font_scale: float = DEFAULT_FONT_SCALE
    font_family: str = ""
    font_file: str = ""
    background_image: str = ""
    blur_radius: int = 0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Preferences:
        theme = str(data.get("theme", DEFAULT_THEME))
        if theme not in SUPPORTED_THEMES:
            theme = DEFAULT_THEME
        try:
            scale = float(data.get("font_scale", DEFAULT_FONT_SCALE))
        except (TypeError, ValueError):
            scale = DEFAULT_FONT_SCALE
        # 与 theme.set_font_scale 同一夹取范围
        scale = max(0.8, min(1.6, scale)) if math.isfinite(scale) else DEFAULT_FONT_SCALE

        def text(key: str) -> str:
            value = data.get(key, "")
            return value[:1024] if isinstance(value, str) else ""

        try:
            radius = int(data.get("blur_radius", 0))
        except (TypeError, ValueError, OverflowError):
            radius = 0
        return cls(
            theme=theme,
            font_scale=scale,
            font_family=text("font_family"),
            font_file=text("font_file"),
            background_image=text("background_image"),
            blur_radius=max(0, min(32, radius)),
        )


class PreferencesService:
    """读写界面偏好。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Preferences:
        """读取偏好。文件缺失/损坏/字段非法都回退默认值。"""
        if not self._path.is_file():
            return Preferences()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return Preferences()
        if not isinstance(data, dict):
            return Preferences()
        return Preferences.from_json(data)

    def save(self, preferences: Preferences) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(preferences.to_json(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
