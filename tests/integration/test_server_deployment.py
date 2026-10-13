from __future__ import annotations

from pathlib import Path


def test_deployment_unit_uses_dedicated_user_and_locked_data_dir() -> None:
    unit = Path("deploy/limbowave.service").read_text(encoding="utf-8")
    assert "User=limbowave" in unit
    assert "Group=limbowave" in unit
    assert "--data-dir /var/lib/limbowave" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit


def test_reverse_proxy_example_keeps_https_headers_and_streaming() -> None:
    config = Path("deploy/limbowave-nginx.conf").read_text(encoding="utf-8")
    assert "listen 443 ssl http2" in config
    assert "proxy_pass http://127.0.0.1:8765" in config
    assert "X-Forwarded-Proto https" in config
    assert "proxy_buffering off" in config
    assert "client_max_body_size 64k" in config


def test_deployment_guide_documents_manual_unlock_and_backups() -> None:
    guide = Path("docs/development/server-deployment.md").read_text(encoding="utf-8")
    assert "server unlock" in guide
    assert "locked" in guide
    assert "backup" in guide.lower()
    assert "443" in guide
