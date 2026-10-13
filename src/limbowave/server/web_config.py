"""Deployment-only web configuration for a loopback reverse-proxy boundary."""

from __future__ import annotations

from dataclasses import dataclass

from limbowave.web.security import WebConfig


@dataclass(frozen=True, slots=True)
class ServerWebConfig:
    public_origin: str
    bind_host: str = "127.0.0.1"
    bind_port: int = 8765
    max_body: int = 65536
    requests_per_minute: int = 240

    def web_config(self) -> WebConfig:
        config = WebConfig(
            host=self.bind_host,
            port=self.bind_port,
            public_origin=self.public_origin,
            max_body=self.max_body,
            requests_per_minute=self.requests_per_minute,
        )
        config.validate()
        return config
