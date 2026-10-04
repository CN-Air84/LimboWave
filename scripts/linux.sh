#!/usr/bin/env bash
# Run with: bash scripts/linux.sh bootstrap|run|verify|package [arguments...]
set -euo pipefail
cd -- "$(dirname -- "$(readlink -f -- "$0")")/.."
if [[ "$(uname -s)" != Linux ]]; then
    printf '%s\n' 'This entry point requires Linux; use the PowerShell scripts on Windows.' >&2
    exit 1
fi
command -v uv >/dev/null || { echo 'Install uv first: https://docs.astral.sh/uv/' >&2; exit 1; }
# Do not overwrite a Windows .venv in a shared checkout/WSL mount.
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$PWD/.venv-linux}"
action="${1:-run}"
if (( $# )); then shift; fi
case "$action" in
    bootstrap)
        uv sync --locked --python 3.12
        uv run --locked python scripts/selfcheck.py
        ;;
    run) uv run --locked python -m limbowave "$@" ;;
    verify)
        uv run --locked ruff check src tests
        uv run --locked mypy src
        uv run --locked pytest "$@"
        ;;
    package) uv run --locked python scripts/packager.py --cli "$@" ;;
    *) echo 'Usage: bash scripts/linux.sh bootstrap|run|verify|package [arguments...]' >&2; exit 2 ;;
esac
