# LimboWave On Server deployment

This guide deploys the locked-first, single-user server behind HTTPS on a Linux VPS.

## 1. Create the service account

```bash
sudo useradd --system --home /var/lib/limbowave --shell /usr/sbin/nologin limbowave
sudo mkdir -p /var/lib/limbowave /opt/limbowave
sudo chown -R limbowave:limbowave /var/lib/limbowave
```

## 2. Install Node and the pinned Pi runtime

The packaged Python application **does not bundle Node or Pi**. Install both before
starting the service. The runtime contract requires **Node >=22.19.0** and exactly
**`@earendil-works/pi-coding-agent@0.86.0`**; do not substitute Pi `latest` or assume
a developer's nvm installation is visible to the `nologin` service account.

The following example is for the supported glibc Linux x86_64 target. It installs
the current Node 24 LTS patch from the official distribution into a root-owned
system path. Review the official release/checksum information before deploying;
record the resolved archive/version for reproducible rebuilds. Other architectures
need their matching archive and separate acceptance testing.

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl xz-utils
workdir=$(mktemp -d)
cd "$workdir"
curl -fsSLO https://nodejs.org/dist/latest-v24.x/SHASUMS256.txt
NODE_ARCHIVE=$(awk '$2 ~ /^node-v24[.][0-9]+[.][0-9]+-linux-x64[.]tar[.]xz$/ {print $2}' SHASUMS256.txt)
test -n "$NODE_ARCHIVE" || exit 1
NODE_VERSION=$(printf '%s' "$NODE_ARCHIVE" | sed 's/^node-//; s/-linux-x64.tar.xz$//')
curl -fsSLO "https://nodejs.org/dist/$NODE_VERSION/$NODE_ARCHIVE"
grep "  $NODE_ARCHIVE$" SHASUMS256.txt | sha256sum --check --strict - || exit 1
sudo install -d /opt/limbowave/node /opt/limbowave/pi
sudo tar -xJf "$NODE_ARCHIVE" -C /opt/limbowave/node --strip-components=1
sudo env PATH=/opt/limbowave/node/bin:/usr/bin:/bin \
  /opt/limbowave/node/bin/npm install --global --prefix /opt/limbowave/pi \
  --ignore-scripts @earendil-works/pi-coding-agent@0.86.0
cd -
```

Preflight with the **same HOME and PATH as systemd**, not a login shell. The first
command rejects an unsupported Node or a wrong Pi version; the second verifies
that the actual CLI entrypoint can start under the service identity:

```bash
sudo -u limbowave env -i HOME=/var/lib/limbowave \
  PATH=/opt/limbowave/node/bin:/opt/limbowave/pi/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  node -e "const [m,n]=process.versions.node.split('.').map(Number); if(m<22||(m===22&&n<19))process.exit(1); require('node:assert').strictEqual(require('/opt/limbowave/pi/lib/node_modules/@earendil-works/pi-coding-agent/package.json').version,'0.86.0')"
sudo -u limbowave env -i HOME=/var/lib/limbowave \
  PATH=/opt/limbowave/node/bin:/opt/limbowave/pi/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  pi --version
```

Keep `/opt/limbowave/node` and `/opt/limbowave/pi` root-owned and read-only to the
service; writable runtime state belongs in `/var/lib/limbowave`. The supplied unit
sets these paths explicitly and uses `ExecStartPre` to fail startup if Node or Pi
cannot launch. `ProtectHome=true` stays enabled. Re-run preflight after upgrades.
See [the Pi runtime contract](../architecture/pi-runtime-contract.md) for the
validated protocol and [Linux support](linux.md) for platform limitations.

## 3. Install the application

Build or copy the packaged `limbowave` entrypoint into `/opt/limbowave/bin/limbowave`, then initialize the data directory:

```bash
sudo -u limbowave /opt/limbowave/bin/limbowave server init --data-dir /var/lib/limbowave
```

## 4. Install the systemd unit

```bash
sudo cp deploy/limbowave.service /etc/systemd/system/limbowave.service
sudo systemctl daemon-reload
sudo systemctl enable --now limbowave
```

The server starts locked. It must not auto-unlock and must not store the vault password on disk.

## 5. Configure the reverse proxy

Use the example in `deploy/limbowave-nginx.conf`. Replace the domain and certificate paths with your real values. The application must remain bound to loopback (`127.0.0.1:8765`) and must not be exposed directly.

Recommended nginx settings already included in the example:

- TLS only; HTTP redirects to HTTPS.
- Strict security headers.
- A 64 KiB request-body limit.
- No response buffering, so SSE works correctly.
- One-hour read timeout for long-lived event streams.

## 6. Unlock and pair

Open an SSH session to the VPS and run:

```bash
sudo -u limbowave /opt/limbowave/bin/limbowave server unlock --data-dir /var/lib/limbowave
```

Enter the vault password interactively. After unlocking, open a pairing window and complete device pairing:

```bash
sudo -u limbowave /opt/limbowave/bin/limbowave server pair --data-dir /var/lib/limbowave
```

Before declaring the deployment ready, configure a usable model endpoint and
credentials using the [configuration guide](configuration.md), then send a short
prompt from the paired browser and receive a **real model** reply. A successful
`--version`, locked `/healthz`, unlock, pairing or static build is not a model
acceptance test. If there is no model reply, inspect the service diagnostics for
`kernel.unavailable`, runtime spawn errors and model/credential configuration.
Do not expose passwords or API keys in command arguments or diagnostic reports.

## 7. Backups

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

## 8. Operational limits

- Keep only ports 22 and 443 open.
- Monitor disk usage on `/var/lib/limbowave`.
- Rotate device authorizations when a device is no longer trusted.
- If the process restarts, it returns to the locked state and requires another manual SSH unlock.

### Sync capacity and retention

Full sync snapshots pin their cursor and contents to one SQLite read transaction.
Only one snapshot is built at a time, on a background worker; concurrent exports
receive retryable `503 snapshot_busy`. The default budget is 10,000 entities and
8 MiB of stored field bytes checked **before decryption**, plus an 8 MiB encoded
response cap. Encryption overhead counts toward the stored-byte budget. Exceeding
either cap returns `413 snapshot_too_large`, with no snapshot or cursor to save;
there is no silent truncation or partial export. Large-history paged snapshots
are not implemented; a larger instance/explicitly reviewed export budget is
needed beyond these limits. Browser paged chat history is separate from full sync.

Content changes retain the latest 50,000 sequence-number window automatically on
write. Older clients receive `409 resync_required` and must fetch a fresh snapshot.
Device authorizations and touch heartbeats do not enter this log. Deleting log
rows allows SQLite to reuse space but does not promise a smaller database file;
continue monitoring disk usage and backups. Incremental entity application in the
PC cache remains a separate, documented limitation.
