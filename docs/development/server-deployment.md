# LimboWave On Server deployment

This guide deploys the locked-first, single-user server behind HTTPS on a Linux VPS.

## 1. Create the service account

```bash
sudo useradd --system --home /var/lib/limbowave --shell /usr/sbin/nologin limbowave
sudo mkdir -p /var/lib/limbowave /opt/limbowave
sudo chown -R limbowave:limbowave /var/lib/limbowave
```

## 2. Install the application

Build or copy the packaged `limbowave` entrypoint into `/opt/limbowave/bin/limbowave`, then initialize the data directory:

```bash
sudo -u limbowave /opt/limbowave/bin/limbowave server init --data-dir /var/lib/limbowave
```

## 3. Install the systemd unit

```bash
sudo cp deploy/limbowave.service /etc/systemd/system/limbowave.service
sudo systemctl daemon-reload
sudo systemctl enable --now limbowave
```

The server starts locked. It must not auto-unlock and must not store the vault password on disk.

## 4. Configure the reverse proxy

Use the example in `deploy/limbowave-nginx.conf`. Replace the domain and certificate paths with your real values. The application must remain bound to loopback (`127.0.0.1:8765`) and must not be exposed directly.

Recommended nginx settings already included in the example:

- TLS only; HTTP redirects to HTTPS.
- Strict security headers.
- A 64 KiB request-body limit.
- No response buffering, so SSE works correctly.
- One-hour read timeout for long-lived event streams.

## 5. Unlock and pair

Open an SSH session to the VPS and run:

```bash
sudo -u limbowave /opt/limbowave/bin/limbowave server unlock --data-dir /var/lib/limbowave
```

Enter the vault password interactively. After unlocking, open a pairing window and complete device pairing:

```bash
sudo -u limbowave /opt/limbowave/bin/limbowave server pair --data-dir /var/lib/limbowave
```

## 6. Backups

Use the existing encrypted backup and restore flow:

```bash
sudo -u limbowave uv run python scripts/server_backup.py \
  --data-dir /var/lib/limbowave \
  --output /var/backups/limbowave/limbowave-$(date +%F).limbowave-backup

sudo -u limbowave uv run python scripts/server_backup.py \
  --data-dir /var/lib/limbowave \
  --source /var/backups/limbowave/limbowave-2026-10-13.limbowave-backup \
  --destination /var/tmp/limbowave-restore
```

Do not copy the live SQLite database or WAL files directly. Store backup archives off the VPS and keep the backup password separate from the server vault password.

## 7. Operational limits

- Keep only ports 22 and 443 open.
- Monitor disk usage on `/var/lib/limbowave`.
- Rotate device authorizations when a device is no longer trusted.
- If the process restarts, it returns to the locked state and requires another manual SSH unlock.
