"""Deterministic reuse boundaries; no model-based semantic-erasure promise."""

from __future__ import annotations

import hashlib
import math
import unicodedata
from collections.abc import Iterator
from datetime import datetime
from urllib.parse import parse_qsl, unquote, unquote_plus, urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


def normalized(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def content_fingerprint(text: str) -> str:
    return hashlib.sha256(normalized(text).encode("utf-8")).hexdigest()


class TopicBoundary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    topic: str = Field(min_length=1, max_length=200)
    aliases: tuple[str, ...] = Field(default=(), max_length=20)
    source_ids: tuple[str, ...] = Field(default=(), max_length=200)
    content_hashes: tuple[str, ...] = Field(default=(), max_length=200)
    created_at: AwareDatetime

    @field_validator("topic")
    @classmethod
    def validate_topic(cls, value: str) -> str:
        if not normalized(value):
            raise ValueError("话题不能为空")
        return value

    @field_validator("aliases", "source_ids")
    @classmethod
    def validate_terms(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not normalized(v) or len(v) > 200 for v in values):
            raise ValueError("别名和资料 ID 需为1–200字")
        return values

    def matches(self, text: str, source_id: str = "") -> bool:
        if source_id and source_id in self.source_ids:
            return True
        if self.content_hashes and content_fingerprint(text) in self.content_hashes:
            return True
        value = normalized(text)
        return any(normalized(term) in value for term in (self.topic, *self.aliases))


class PrivacyInspectionError(ValueError):
    """A payload cannot be safely inspected; callers must reject it as a whole."""


def _inspection_views(text: str, *, encoded: bool) -> Iterator[str]:
    """Bounded inspection copies, never replacements for an authorization target."""
    current = text
    for _ in range(8):
        yield current
        url = current.lower().startswith(("http://", "https://"))
        if url:
            try:
                parsed = urlsplit(current)
                host = parsed.hostname or ""
                yield host
                yield host.encode("idna").decode("ascii")
                yield host.encode("ascii").decode("idna") if host.isascii() else host
                yield parsed.path
                yield from parsed.path.split("/")
                for key, value in parse_qsl(parsed.query, keep_blank_values=True,
                                             errors="strict", max_num_fields=2000):
                    yield key
                    yield value
            except (ValueError, UnicodeError) as exc:
                raise PrivacyInspectionError("隐私检查无法安全解析网络地址") from exc
        if not encoded and not url:
            return
        try:
            decoded = unquote_plus(current, errors="strict") if encoded else unquote(
                current, errors="strict",
            )
        except UnicodeError as exc:
            raise PrivacyInspectionError("隐私检查无法安全解析网络编码") from exc
        if decoded == current:
            return
        current = decoded
    raise PrivacyInspectionError("隐私检查超过网络编码层数预算")


class PrivacyPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(default=1, ge=1, le=1, strict=True)
    fresh_start: bool = Field(default=False, strict=True)
    rules: tuple[TopicBoundary, ...] = Field(default=(), max_length=100)

    @property
    def active(self) -> bool:
        return self.fresh_start or bool(self.rules)

    def blocks(self, text: str, source_id: str = "") -> bool:
        return any(rule.matches(text, source_id) for rule in self.rules)

    def blocks_structure(self, payload: object, *, encoded_views: bool = False) -> bool:
        """Check every text/key/source ID without serializing away leaf fingerprints.

        Only plain JSON-shaped objects (plus tuples used by tool notes) are accepted.
        Unknown objects, cycles and budget exhaustion are errors, not permission.
        No custom iteration, stringification or partial output mutation is performed.
        """
        if not self.rules:
            return False
        active: set[int] = set()
        nodes = 0
        characters = 0

        def inspect(value: object, depth: int) -> bool:
            nonlocal nodes, characters
            nodes += 1
            if depth > 64 or nodes > 20_000:
                raise PrivacyInspectionError("隐私检查超过结构预算")
            if type(value) is str:
                assert isinstance(value, str)
                # An ID can occur as a list element, dict key, note or error too.
                try:
                    for view in _inspection_views(value, encoded=encoded_views):
                        characters += len(view)
                        if characters > 2_000_000:
                            raise PrivacyInspectionError("隐私检查超过文本预算")
                        if self.blocks(view, source_id=view):
                            return True
                except UnicodeError as exc:
                    raise PrivacyInspectionError("隐私检查遇到无效文本编码") from exc
                return False
            if value is None or type(value) is bool:
                return False
            if type(value) is int:
                return inspect(str(value), depth + 1) if encoded_views else False
            if type(value) is float:
                assert isinstance(value, float)
                if not math.isfinite(value):
                    raise PrivacyInspectionError("隐私检查不能接受非有限数值")
                return inspect(str(value), depth + 1) if encoded_views else False
            if type(value) not in (dict, list, tuple):
                raise PrivacyInspectionError("隐私检查遇到不支持的资料类型")
            identity = id(value)
            if identity in active:
                raise PrivacyInspectionError("隐私检查遇到循环资料")
            active.add(identity)
            try:
                if isinstance(value, dict):
                    for key, child in value.items():
                        if type(key) is not str:
                            raise PrivacyInspectionError("隐私检查要求文本字段名")
                        if inspect(key, depth + 1) or inspect(child, depth + 1):
                            return True
                elif isinstance(value, (list, tuple)):
                    for child in value:
                        if inspect(child, depth + 1):
                            return True
            finally:
                active.remove(identity)
            return False

        return inspect(payload, 0)

    def permits_derived(self, created_at: datetime) -> bool:
        # Old derived text has no reliable provenance: do not keyword-redact it.
        return all(created_at > rule.created_at for rule in self.rules)
