"""The benchmark itself is side-effect-free until explicitly executed."""

import argparse
import subprocess
import sys

import pytest

from scripts.performance_benchmark import history_sample, positive_count, summarize


def test_import_does_not_initialize_qt_or_application():
    result = subprocess.run(
        [sys.executable, "-c", (
            "import sys; import scripts.performance_benchmark; "
            "assert 'PySide6' not in sys.modules; "
            "assert 'limbowave.app' not in sys.modules"
        )], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_summary_uses_medians_not_flags_and_handles_unavailable_memory():
    assert summarize([]) == {}
    assert summarize([
        {"time_ms": 9.0, "flag": True, "memory_mb": None},
        {"time_ms": 1.0, "flag": False, "memory_mb": None},
        {"time_ms": 2.0, "flag": False, "memory_mb": None},
    ]) == {"time_ms": 2.0}


@pytest.mark.parametrize("value", ["0", "-1"])
def test_sample_count_must_be_positive(value):
    with pytest.raises(argparse.ArgumentTypeError):
        positive_count(value)
    assert positive_count("2") == 2


def test_synthetic_history_comparison_preserves_attachment_results():
    result = history_sample(100)
    assert result["intents"] == 100
    assert result["selected_messages"] == 2
    assert result["legacy_ms"] >= 0
    assert result["scoped_ms"] >= 0
