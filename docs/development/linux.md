# Linux support (initial implementation)

Target: mainstream glibc Linux, Python 3.12–3.13, Node >=22.19.0. Ubuntu 24.04 x86_64 is the CI target. Other distributions, ARM, musl/Alpine and desktop environments are not certified.

## Development / launch
Install uv and Node first. On Debian/Ubuntu, Qt may require libegl1, libopengl0, libxkbcommon0, libdbus-1-3 and (for X11) libxcb-cursor0; install a Chinese font such as fonts-noto-cjk. The exact runtime libraries depend on the Qt wheel and desktop.

```bash
bash scripts/linux.sh bootstrap
npm install -g --ignore-scripts @earendil-works/pi-coding-agent
bash scripts/linux.sh run
QT_QPA_PLATFORM=offscreen bash scripts/linux.sh verify
bash scripts/linux.sh package --console
```

The script uses .venv-linux by default to avoid overwriting a Windows .venv on a shared checkout. Override UV_PROJECT_ENVIRONMENT only intentionally. This script does not install system packages or change security policy.

## Security and migration
- Normal master-password unlock uses Argon2id + ChaCha20-Poly1305; no plaintext-key fallback is introduced.
- Windows DPAPI recovery is unavailable on Linux, including wraps copied from Windows. Knowing the master password remains sufficient for password-wrapped vaults.
- In-app destructive reset is disabled on Linux until a verified system identity backend exists. It is NOT silently downgraded to password-only confirmation.
- Create and verify independent-password encrypted backups before moving machines. Save the backup password separately. Forgetting the master password without a usable backup has no recovery bypass.
- Paths in settings/permissions are OS-specific. Do not carry Windows grants over as Linux grants; review and reauthorize resource scopes after migration.
- Data defaults to ~/.local/share/LimboWave (platformdirs/XDG overrides apply); logs follow platformdirs.user_log_dir.

## Shell
Linux prefers Bash, falls back to POSIX sh; it does not execute arbitrary $SHELL. Temporary UTF-8 scripts run in a separate process session. The tool description identifies the actual shell. Background descendants in that process group are cleaned up on completion or timeout. Deliberately daemonized/detached processes are not a security sandbox guarantee. Captured output is spooled to private temporary files and returned text is capped; temporary disk consumption is not hard-quota-limited.

## Packaging and desktop launcher
Build on Linux, not by cross-compiling a Windows executable. The default result is dist/pyinstaller/LimboWave/LimboWave; distribute the entire directory, including _internal. Node and Pi are still external prerequisites, not bundled by this script. A frozen --help smoke test is not a GUI/Agent acceptance test.

Linux ELF binaries do not embed Windows ICO files. To add a launcher, create a .desktop file with Type=Application, Name=LimboWave, Exec set to the installed executable's absolute path (properly quoted for spaces), Icon set to an installed PNG/SVG path and Terminal=false. Do not point a desktop launcher at the build temp directory. The packager GUI additionally requires Tk; prefer --cli for reproducible builds.

## Verification status
The Linux CI workflow covers native POSIX process behavior, tests and a directory build. Adding the workflow does not mean it has run successfully. Before release, verify on an actual Linux desktop: password creation/unlock, model setup, a real Pi request, tool permission prompts, Bash tool execution, X11 and Wayland, Chinese IME, clipboard, font fallback, HiDPI and packaged launch. Offscreen UI tests cannot certify these behaviors.
