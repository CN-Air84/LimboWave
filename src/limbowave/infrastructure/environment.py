"""固定 HTTPS JSON 天气后端；不抓页面、不自动定位、不借用 MSN 网站凭据。"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from ipaddress import ip_address, ip_network
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from limbowave.application.services.environment_service import (
    EnvironmentError,
    EnvironmentService,
    aware_timestamp,
    location_zone,
    validate_environment_params,
)
from limbowave.domain.network_guard import NetworkBlocked, check_host, resolve_host

MSN_KEY_ENV = "LIMBOWAVE_MSN_WEATHER_API_KEY"
PROVIDER_ENV = "LIMBOWAVE_ENVIRONMENT_PROVIDER"
MAX_RESPONSE_BYTES = 128 * 1024
_FIXED_HOSTS = frozenset({"api.msn.com", "api.open-meteo.com", "geocoding-api.open-meteo.com"})
_FAKE_IP_RANGE = ip_network("198.18.0.0/15")
_WMO = {
    0: "晴",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "强毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷暴",
    96: "雷暴伴冰雹",
    99: "强雷暴伴冰雹",
}


def _number(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: Any, limit: int = 100) -> str | None:
    if not isinstance(value, str):
        return None
    return "".join(char for char in value if ord(char) >= 32)[:limit] or None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _unix(value: Any, zone: ZoneInfo, *, sun: bool = False) -> str | None:
    number = _number(value)
    if number is None or (sun and number <= 0):
        return None
    try:
        return datetime.fromtimestamp(number, UTC).astimezone(zone).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


class _WeatherCredentialFilter(logging.Filter):
    """httpx 在 INFO 级别记录完整 URL；查询参数凭据必须在生成日志时脱敏。"""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        clean = re.sub(r"(?i)([?&]apikey=)[^&\s\"']+", r"\1[REDACTED]", message)
        if clean != message:
            record.msg, record.args = clean, ()
        return True


_CREDENTIAL_FILTER = _WeatherCredentialFilter()


def _validate_coordinates(lat: Any, lon: Any, place: dict[str, Any]) -> None:
    lat, lon = _number(lat), _number(lon)
    if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise EnvironmentError("invalid_response", "天气服务未返回有效的数据地点坐标")
    # 天气模型网格可与请求点稍有偏移，但不能用默认 IP 地点的天气冒充指定地点。
    longitude_distance = abs((lon - place["longitude"] + 180) % 360 - 180)
    if abs(lat - place["latitude"]) > 1.5 or longitude_distance > 1.5:
        raise EnvironmentError("location_mismatch", "天气服务返回的数据地点与请求地点不符")


class HttpEnvironmentProvider:
    """MSN 使用用户授权 Key；Open-Meteo 免费端点限个人非商业用途。"""

    def __init__(
        self,
        *,
        provider: str = "open-meteo",
        api_key: str | None = None,
        app_id: str | None = None,
        proxy: str | None = None,
        no_proxy: str = "",
        transport: httpx.BaseTransport | None = None,
        host_checker: Callable[[str], None] = check_host,
    ) -> None:
        self._provider = provider
        self._api_key = api_key
        self._app_id = app_id
        self._proxy = proxy
        self._no_proxy = no_proxy
        self._transport = transport
        self._check_host = host_checker
        if provider == "msn":
            for name in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2"):
                logger = logging.getLogger(name)
                if _CREDENTIAL_FILTER not in logger.filters:
                    logger.addFilter(_CREDENTIAL_FILTER)

    def domains(self, *, has_location: bool) -> tuple[str, ...]:
        host = "api.msn.com" if self._provider == "msn" else "api.open-meteo.com"
        return ("geocoding-api.open-meteo.com", host) if has_location else (host,)

    def _proxy_for(self, host: str) -> str | None:
        for entry in self._no_proxy.lower().split(","):
            entry = entry.strip().split(":")[0].lstrip(".")
            if entry and (entry == "*" or host == entry or host.endswith("." + entry)):
                return None
        return self._proxy

    def _guard_host(self, host: str, proxy: str | None) -> None:
        if host not in _FIXED_HOSTS:
            raise NetworkBlocked("天气后端只允许固定的 HTTPS 服务主机")
        try:
            self._check_host(host)
        except NetworkBlocked:
            # 用户显式配置的 HTTP CONNECT 代理负责远端 DNS；Fake-IP 不会作为连接目标。
            # 只兼容固定天气主机、纯 198.18/15 Fake-IP，其他私网/元数据/混合解析仍拒绝。
            addresses = resolve_host(host) if proxy else []
            try:
                fake_only = bool(addresses) and all(
                    ip_address(address) in _FAKE_IP_RANGE for address in addresses
                )
            except ValueError:
                fake_only = False
            if not proxy or not fake_only:
                raise

    def _get_json(self, host: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        proxy = self._proxy_for(host)
        if proxy and urlsplit(proxy).scheme not in ("http", "https"):
            raise EnvironmentError("proxy_not_supported", "天气工具仅支持 HTTP/HTTPS CONNECT 代理")
        self._guard_host(host, proxy)
        try:
            with (
                httpx.Client(
                    transport=self._transport,
                    proxy=proxy,
                    trust_env=False,
                    timeout=httpx.Timeout(8, connect=3),
                    follow_redirects=False,
                    headers={"Accept": "application/json"},
                ) as client,
                client.stream("GET", f"https://{host}{path}", params=params) as response,
            ):
                if response.status_code in (401, 403):
                    raise EnvironmentError(
                        "provider_auth", "天气服务拒绝授权；请检查合法 API Key 和使用权限"
                    )
                if response.status_code == 429:
                    raise EnvironmentError("rate_limited", "天气服务限流，请稍后重试")
                if response.status_code != 200:
                    raise EnvironmentError(
                        "provider_unavailable", "天气服务暂不可用（不跟随重定向）"
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise EnvironmentError("invalid_response", "天气 JSON 响应超过大小上限")
                    body.extend(chunk)
            value = json.loads(body)
            if not isinstance(value, dict):
                raise EnvironmentError("invalid_response", "天气服务返回了无效 JSON 对象")
            return value
        except httpx.TimeoutException as exc:
            raise EnvironmentError("provider_timeout", "天气服务请求超时，请稍后重试") from exc
        except (httpx.HTTPError, OSError) as exc:
            raise EnvironmentError(
                "provider_unavailable", "天气服务网络请求失败，请稍后重试"
            ) from exc
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise EnvironmentError("invalid_response", "天气服务返回了无法解析的 JSON") from exc

    def _geocode(self, location: str) -> dict[str, Any]:
        data = self._get_json(
            "geocoding-api.open-meteo.com",
            "/v1/search",
            {
                "name": location,
                "count": 5,
                "language": "zh",
                "format": "json",
            },
        )
        candidates = []
        for item in _list(data.get("results"))[:5]:
            item = _mapping(item)
            lat, lon = _number(item.get("latitude")), _number(item.get("longitude"))
            if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
                continue
            candidates.append(
                {
                    "name": _text(item.get("name")),
                    "region": _text(item.get("admin1")),
                    "country": _text(item.get("country")),
                    "latitude": lat,
                    "longitude": lon,
                }
            )
        if not candidates:
            raise EnvironmentError(
                "location_not_found", "未找到该地点，请提供更明确的城市/地区或经纬度"
            )
        if len(candidates) > 1:
            raise EnvironmentError(
                "ambiguous_location",
                "找到多个同名地点，请让用户选择后使用返回的经纬度",
                {
                    "candidates": candidates,
                },
            )
        return candidates[0]

    def query(
        self,
        *,
        location: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> dict[str, Any]:
        if self._provider not in ("msn", "open-meteo"):
            raise EnvironmentError(
                "provider_not_configured", "环境数据源须配置为 msn 或 open-meteo"
            )
        if self._provider == "msn" and not self._api_key:
            raise EnvironmentError(
                "provider_not_configured", "MSN 天气需要用户自己的合法授权 API Key；未借用网站 Key"
            )
        validate_environment_params(
            {"location": location}
            if location is not None
            else {
                "latitude": latitude,
                "longitude": longitude,
            }
        )
        place = (
            self._geocode(location.strip())
            if location is not None
            else {
                "name": None,
                "region": None,
                "country": None,
                "latitude": latitude,
                "longitude": longitude,
            }
        )
        latitude, longitude = place["latitude"], place["longitude"]
        if self._provider == "msn":
            raw = self._get_json(
                "api.msn.com",
                "/weather/overview",
                {
                    "lat": latitude,
                    "lon": longitude,
                    "apikey": self._api_key,
                    **({"appId": self._app_id} if self._app_id else {}),
                    "ocid": "msftweather",
                    "locale": "zh-cn",
                    "units": "C",
                    "days": 3,
                    "hours": 0,
                    "wrapodata": "false",
                },
            )
            return self._msn(raw, place)
        raw = self._get_json(
            "api.open-meteo.com",
            "/v1/forecast",
            {
                "latitude": latitude,
                "longitude": longitude,
                "timezone": "auto",
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,"
                "wind_speed_10m,is_day",
                "daily": "temperature_2m_max,temperature_2m_min,sunrise,sunset",
                "temperature_unit": "celsius",
                "wind_speed_unit": "kmh",
                "past_days": 1,
                "forecast_days": 2,
                "timeformat": "unixtime",
            },
        )
        return self._open_meteo(raw, place)

    def _open_meteo(self, raw: dict[str, Any], place: dict[str, Any]) -> dict[str, Any]:
        _validate_coordinates(raw.get("latitude"), raw.get("longitude"), place)
        place = {**place, "timezone": _text(raw.get("timezone"))}
        zone = location_zone({"location": place})
        units = _mapping(raw.get("current_units"))
        if (
            units.get("temperature_2m", "°C") != "°C"
            or units.get("wind_speed_10m", "km/h") != "km/h"
        ):
            raise EnvironmentError("invalid_response", "天气服务返回了不符合请求的单位")
        current, daily = _mapping(raw.get("current")), _mapping(raw.get("daily"))
        code = _number(current.get("weather_code"))
        days = []
        for index, date in enumerate(_list(daily.get("time"))[:3]):
            # 文档要求给 daily Unix 时间重新应用偏移才能还原日期；UTC 日历日期即 API 日期。
            date_text = _unix(date, ZoneInfo("UTC"))
            if date_text is None:
                continue

            def at(key: str, index: int = index) -> Any:
                values = _list(daily.get(key))
                return values[index] if index < len(values) else None

            days.append(
                {
                    "date": date_text[:10],
                    "high_c": _number(at("temperature_2m_max")),
                    "low_c": _number(at("temperature_2m_min")),
                    "sunrise": _unix(at("sunrise"), zone, sun=True),
                    "sunset": _unix(at("sunset"), zone, sun=True),
                }
            )
        if not days and _number(current.get("temperature_2m")) is None:
            raise EnvironmentError("invalid_response", "天气服务没有返回可用的天气数据")
        return {
            "provider": "open-meteo",
            "attribution": "Weather data: Open-Meteo (CC BY 4.0)",
            "source_url": "https://open-meteo.com/",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "location": place,
            "current": {
                "temperature_c": _number(current.get("temperature_2m")),
                "feels_like_c": _number(current.get("apparent_temperature")),
                "humidity_percent": _number(current.get("relative_humidity_2m")),
                "wind_kmh": _number(current.get("wind_speed_10m")),
                "condition": _WMO.get(int(code))
                if code is not None and code.is_integer()
                else None,
                "weather_code": code,
                "updated_at": _unix(current.get("time"), zone),
            },
            "daily": days,
            "notes": [
                "Open-Meteo 当前与逐日温度来自天气模型，不是个人所在地的现场实测。",
                "免费端点仅限非商业使用；地点搜索来自 Open-Meteo/GeoNames。",
            ],
        }

    def _msn(self, raw: dict[str, Any], place: dict[str, Any]) -> dict[str, Any]:
        if "value" in raw:
            values = _list(raw["value"])
            raw = _mapping(values[0]) if values else {}
        responses = _list(raw.get("responses"))
        response = _mapping(responses[0]) if responses else {}
        source = _mapping(response.get("source"))
        coordinates = _mapping(source.get("coordinates"))
        _validate_coordinates(coordinates.get("lat"), coordinates.get("lon"), place)
        source_place = _mapping(source.get("location"))
        place = {
            **place,
            "name": _text(source_place.get("Name")) or place.get("name"),
            "region": _text(source_place.get("StateCode")) or place.get("region"),
            "country": _text(source_place.get("CountryName")) or place.get("country"),
            "timezone": _text(source_place.get("TimezoneName")),
        }
        location_zone({"location": place})
        if _mapping(raw.get("units")).get("system", "Metric") != "Metric":
            raise EnvironmentError("invalid_response", "MSN 返回了非公制单位")
        weather_items = _list(response.get("weather"))
        weather = _mapping(weather_items[0]) if weather_items else {}
        current = _mapping(weather.get("current"))
        days = []
        for item in _list(_mapping(weather.get("forecast")).get("days"))[:3]:
            item = _mapping(item)
            daily, almanac = _mapping(item.get("daily")), _mapping(item.get("almanac"))
            valid = aware_timestamp(daily.get("valid"))
            if valid is None:
                continue

            def stamp(key: str, almanac: dict[str, Any] = almanac) -> str | None:
                value = aware_timestamp(almanac.get(key))
                return value.isoformat() if value is not None else None

            days.append(
                {
                    "date": valid.date().isoformat(),
                    "high_c": _number(daily.get("tempHi")),
                    "low_c": _number(daily.get("tempLo")),
                    "sunrise": stamp("sunrise"),
                    "sunset": stamp("sunset"),
                }
            )
        if not days and _number(current.get("temp")) is None:
            raise EnvironmentError("invalid_response", "MSN 没有返回可用的天气数据")
        updated = aware_timestamp(current.get("created"))
        return {
            "provider": "msn",
            "attribution": "Microsoft MSN Weather and its suppliers",
            "location": place,
            "current": {
                "temperature_c": _number(current.get("temp")),
                "feels_like_c": _number(current.get("feels")),
                "humidity_percent": _number(current.get("rh")),
                "wind_kmh": _number(current.get("windSpd")),
                "condition": _text(current.get("cap"), 80),
                "updated_at": updated.isoformat() if updated is not None else None,
            },
            "daily": days,
            "notes": [
                "MSN 概览不提供昨日同刻气温，不据此宣称今天比昨天降温。",
                "MSN 网页接口不是稳定公开的第三方 REST 契约；须自行取得微软授权。",
            ],
        }


def create_environment_service(env: Mapping[str, str] | None = None) -> EnvironmentService:
    """构造时不联网、不读取地点；凭据只留在应用侧 HTTP 后端。"""
    env = os.environ if env is None else env
    key = env.get(MSN_KEY_ENV, "").strip() or None
    provider = env.get(PROVIDER_ENV, "msn" if key else "open-meteo").strip().lower()
    proxy = (
        env.get("https_proxy")
        or env.get("HTTPS_PROXY")
        or env.get("all_proxy")
        or env.get("ALL_PROXY")
    )
    no_proxy = env.get("no_proxy", env.get("NO_PROXY", ""))
    return EnvironmentService(
        HttpEnvironmentProvider(
            provider=provider,
            api_key=key,
            app_id=env.get("LIMBOWAVE_MSN_WEATHER_APP_ID"),
            proxy=proxy,
            no_proxy=no_proxy,
        )
    )
