# Headless server development baseline

The server entrypoint is Qt-free and starts in a locked state. It requires:

- Linux with Python 3.12 or 3.13;
- a local SQLite database on block storage (do not put the database or WAL on NFS);
- Node.js and the Pi CLI for a real model run;
- a dedicated non-root service account for deployment;
- one active model run at a time in the first release.

The process keeps the web listener on loopback by default. A reverse proxy is responsible for TLS and the public HTTPS origin. The server does not read a password on startup. Unlocking is an interactive SSH operation through the owner-only Unix control socket:

```text
python -m limbowave server init --data-dir /srv/limbowave
python -m limbowave server run --data-dir /srv/limbowave
ssh vps python -m limbowave server unlock --data-dir /srv/limbowave
```

The default small-instance baseline is 1 vCPU, 1–2 GiB RAM, 10 GiB free disk, and no concurrent model runs. Measure the actual Pi/provider footprint before enabling attachments or larger context windows.
