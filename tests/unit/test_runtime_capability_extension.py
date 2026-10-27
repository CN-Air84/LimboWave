"""Execute the actual extension hooks; no provider credentials or external network."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="requires Node with stripTypeScriptTypes")
def test_context_snapshot_refresh_fallback_and_privacy_order():
    script = Path(__file__).with_name("runtime_capability_extension.mjs")
    extension = (
        Path(__file__).parents[2] / "src/limbowave/infrastructure/extensions/policy_enforcement.ts"
    )
    result = subprocess.run(
        [NODE, str(script), str(extension)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=25,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["ok"] is True
