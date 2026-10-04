"""Atomic appearance-theme state and managed background assets."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from limbowave.domain.appearance import (
    BUILTIN_BY_ID,
    BUILTIN_THEMES,
    DEFAULT_THEME_ID,
    AppearanceTheme,
)

SCHEMA_VERSION = 1
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 64_000_000


@dataclass(frozen=True, slots=True)
class ThemeState:
    revision: int
    active_theme_id: str
    custom_themes: tuple[AppearanceTheme, ...]


class AppearanceThemeService:
    def __init__(
        self,
        root: Path,
        *,
        legacy_theme: str = "dark",
        legacy_background_image: str = "",
        legacy_blur_radius: int = 0,
    ) -> None:
        state_existed = (root / "theme-state.json").is_file()
        self.root = root
        self.assets = root / "assets"
        self.path = root / "theme-state.json"
        self._lock = RLock()
        self._legacy_theme = legacy_theme
        self._state = self._load()
        if not state_existed and legacy_background_image:
            self._migrate_legacy_background(legacy_background_image, legacy_blur_radius)

    def _migrate_legacy_background(self, path: str, radius: int) -> None:
        try:
            asset = self.import_background(Path(path))
        except (OSError, ValueError):
            return
        from dataclasses import replace

        source = self.active_theme
        migrated = AppearanceTheme(
            id=str(uuid.uuid4()),
            name="迁移的外观",
            colors=source.colors,
            background=replace(source.background, asset=asset),
            materials=replace(
                source.materials,
                content_blur_radius=max(0, min(64, radius)),
                sidebar_blur_radius=max(0, min(64, radius)),
            ),
            text_glow=source.text_glow,
        )
        self._commit(migrated.id, (migrated,))

    @property
    def revision(self) -> int:
        return self._state.revision

    @property
    def active_theme(self) -> AppearanceTheme:
        return self.get(self._state.active_theme_id)

    @property
    def active_theme_id(self) -> str:
        return self._state.active_theme_id

    def themes(self) -> tuple[AppearanceTheme, ...]:
        return (*BUILTIN_THEMES, *self._state.custom_themes)

    def get(self, theme_id: str) -> AppearanceTheme:
        if theme_id in BUILTIN_BY_ID:
            return BUILTIN_BY_ID[theme_id]
        for theme in self._state.custom_themes:
            if theme.id == theme_id:
                return theme
        return BUILTIN_BY_ID[DEFAULT_THEME_ID]

    def activate(self, theme_id: str) -> AppearanceTheme:
        theme = self.get(theme_id)
        if theme.id != theme_id:
            raise KeyError(theme_id)
        self._commit(theme.id, self._state.custom_themes)
        return theme

    def create(
        self, name: str, source: AppearanceTheme, *, activate: bool = True
    ) -> AppearanceTheme:
        clean_name = self._valid_name(name)
        self._assert_unique_name(clean_name)
        created = AppearanceTheme(
            id=str(uuid.uuid4()),
            name=clean_name,
            colors=source.colors,
            background=source.background,
            materials=source.materials,
            text_glow=source.text_glow,
        )
        themes = (*self._state.custom_themes, created)
        self._commit(created.id if activate else self._state.active_theme_id, themes)
        return created

    def save(self, theme: AppearanceTheme) -> AppearanceTheme:
        if theme.readonly:
            raise PermissionError("built-in themes are read-only")
        self._assert_unique_name(theme.name, excluding=theme.id)
        found = False
        updated: list[AppearanceTheme] = []
        for item in self._state.custom_themes:
            if item.id == theme.id:
                updated.append(theme)
                found = True
            else:
                updated.append(item)
        if not found:
            raise KeyError(theme.id)
        self._commit(self._state.active_theme_id, tuple(updated))
        return theme

    def rename(self, theme_id: str, name: str) -> AppearanceTheme:
        theme = self.get(theme_id)
        if theme.readonly:
            raise PermissionError("built-in themes cannot be renamed")
        renamed = theme.with_updates(name=self._valid_name(name))
        return self.save(renamed)

    def delete(self, theme_id: str) -> AppearanceTheme:
        theme = self.get(theme_id)
        if theme.readonly:
            raise PermissionError("built-in themes cannot be deleted")
        themes = tuple(item for item in self._state.custom_themes if item.id != theme_id)
        if len(themes) == len(self._state.custom_themes):
            raise KeyError(theme_id)
        active = (
            DEFAULT_THEME_ID
            if self._state.active_theme_id == theme_id
            else self._state.active_theme_id
        )
        self._commit(active, themes)
        return self.get(active)

    def import_background(self, source: Path) -> str:
        """Normalize a validated single-frame image into the managed asset directory."""
        stat = source.stat()
        if source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp"}:
            raise ValueError("only PNG, JPEG and BMP backgrounds are supported")
        if not source.is_file() or stat.st_size > MAX_IMAGE_BYTES:
            raise ValueError("background image is missing or exceeds 32 MiB")
        try:
            with Image.open(source) as opened:
                if getattr(opened, "is_animated", False):
                    raise ValueError("animated backgrounds are not supported")
                width, height = opened.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("background image exceeds 64 MP")
                normalized = ImageOps.exif_transpose(opened).convert("RGBA")
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError("unsupported or damaged background image") from exc
        self.assets.mkdir(parents=True, exist_ok=True)
        digest_source = f"{normalized.width}x{normalized.height}:".encode() + normalized.tobytes()
        digest = hashlib.sha256(digest_source).hexdigest()
        destination = self.assets / f"{digest}.png"
        if not destination.exists():
            pending = destination.with_suffix(".tmp")
            normalized.save(pending, format="PNG", optimize=True)
            os.replace(pending, destination)
        return destination.relative_to(self.root).as_posix()

    def orphan_assets(self) -> tuple[Path, ...]:
        referenced = {theme.background.asset for theme in self.themes() if theme.background.asset}
        if not self.assets.is_dir():
            return ()
        return tuple(
            path
            for path in self.assets.glob("*.png")
            if path.relative_to(self.root).as_posix() not in referenced
        )

    def cleanup_orphan_assets(self) -> tuple[Path, ...]:
        deleted: list[Path] = []
        for path in self.orphan_assets():
            try:
                path.unlink()
            except OSError:
                continue
            deleted.append(path)
        return tuple(deleted)

    def resolve_asset(self, asset: str) -> Path | None:
        if not asset:
            return None
        try:
            resolved = (self.root / asset).resolve(strict=True)
            root = self.assets.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            return None
        return resolved if resolved.is_file() else None

    def _load(self) -> ThemeState:
        if not self.path.is_file():
            legacy_id = "builtin:clear-day" if self._legacy_theme == "light" else DEFAULT_THEME_ID
            return ThemeState(0, legacy_id, ())
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if int(raw.get("schema_version", -1)) != SCHEMA_VERSION:
                raise ValueError("unsupported theme state schema")
            themes = tuple(AppearanceTheme.from_json(item) for item in raw.get("themes", []))
            if len({theme.id for theme in themes}) != len(themes):
                raise ValueError("duplicate custom theme id")
            names = {theme.name.casefold() for theme in themes}
            builtin_names = {theme.name.casefold() for theme in BUILTIN_THEMES}
            if len(names) != len(themes) or names & builtin_names:
                raise ValueError("duplicate theme name")
            active = str(raw.get("active_theme_id", DEFAULT_THEME_ID))
            available = {*BUILTIN_BY_ID, *(theme.id for theme in themes)}
            if active not in available:
                active = DEFAULT_THEME_ID
            return ThemeState(max(0, int(raw.get("revision", 0))), active, themes)
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            with suppress(OSError):
                self.path.replace(self.path.with_name(f"theme-state.corrupt-{stamp}.json"))
            return ThemeState(0, DEFAULT_THEME_ID, ())

    def _commit(self, active_id: str, themes: tuple[AppearanceTheme, ...]) -> None:
        with self._lock:
            revision = self._state.revision + 1
            payload: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "revision": revision,
                "active_theme_id": active_id,
                "themes": [theme.to_json() for theme in themes],
            }
            self.root.mkdir(parents=True, exist_ok=True)
            pending = self.path.with_suffix(".tmp")
            with pending.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, self.path)
            self._state = ThemeState(revision, active_id, themes)

    def _valid_name(self, name: str) -> str:
        clean = " ".join(name.strip().split())
        if not 1 <= len(clean) <= 48:
            raise ValueError("theme name must contain 1-48 characters")
        return clean

    def _assert_unique_name(self, name: str, *, excluding: str = "") -> None:
        folded = name.casefold()
        for theme in self.themes():
            if theme.id != excluding and theme.name.casefold() == folded:
                raise ValueError("theme name already exists")
