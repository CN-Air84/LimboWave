"""可复现的本地诊断压力探针；仅向自动清理的临时目录写入合成数据。

运行：uv run python scripts/diagnostics_benchmark.py --records 20000
耗时/吞吐只作观测，不作为跨机器单测断言。tracemalloc 案例单列，避免冒充正常吞吐。
"""

from __future__ import annotations

import argparse
import json
import logging
import tempfile
import time
import tracemalloc
from dataclasses import asdict
from pathlib import Path
from typing import Any

from limbowave.infrastructure.diagnostics import LogConfig, LogManager, LogReader


def measure(
    name: str,
    records: int,
    *,
    level: int = logging.INFO,
    trace: bool = False,
    unlimited: bool = False,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="limbowave-diagnostics-") as folder:
        options: dict[str, Any] = {}
        if unlimited:
            options.update(disk_bytes_per_second=0, queue_capacity=records + 64)
        config = LogConfig(Path(folder), **options)
        with LogManager(config) as manager:
            log = manager.get_logger("benchmark", component="diagnostics")
            if trace:
                tracemalloc.start()
            samples: list[int] = []
            started = time.perf_counter()
            cpu_started = time.process_time()
            for index in range(records):
                before = time.perf_counter_ns() if index < 1000 else 0
                log.log(level, "synthetic event %d", index, extra={"event_id": index})
                if index < 1000:
                    samples.append(time.perf_counter_ns() - before)
            producer_seconds = time.perf_counter() - started
            drained = manager.flush(timeout=30)
            elapsed = time.perf_counter() - started
            cpu_seconds = time.process_time() - cpu_started
            peak = tracemalloc.get_traced_memory()[1] if trace else None
            if trace:
                tracemalloc.stop()
            status = manager.status()
            retained_bytes = sum(
                path.stat().st_size for path in LogReader(config).paths() if path.exists()
            )
            if not drained or status.file_dropped or status.format_errors:
                raise RuntimeError(f"Benchmark did not complete cleanly: {status}")
            assert status.pending == 0
            assert status.sequence == (
                status.written + status.queue_dropped + status.rate_limited + status.file_dropped
            )
            assert retained_bytes <= config.max_file_bytes * (config.backup_count + 1)
            if config.disk_bytes_per_second:
                # 容许启动到计时点之间的少量时间，不把计时边界变成性能断言。
                budget = max(config.disk_bytes_per_second, config.max_record_bytes)
                assert status.bytes_written <= budget + config.disk_bytes_per_second * (elapsed + 1)
            samples.sort()
            return {
                "case": name,
                "records": records,
                "producer_seconds": round(producer_seconds, 4),
                "calls_per_second": round(records / producer_seconds),
                "producer_p95_us_first_1000": round(
                    samples[int((len(samples) - 1) * 0.95)] / 1000, 2
                ),
                "total_seconds": round(elapsed, 4),
                "process_cpu_seconds": round(cpu_seconds, 4),
                "tracemalloc_enabled": trace,
                "traced_peak_bytes": peak,
                "retained_bytes": retained_bytes,
                "status": asdict(status),
            }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=20000)
    args = parser.parse_args()
    if args.records < 1:
        parser.error("--records must be positive")
    results = [
        measure("disabled_debug", args.records, level=logging.DEBUG),
        measure("unlimited_info", min(args.records, 5000), unlimited=True),
        measure("default_budget_flood", args.records),
        measure("default_budget_memory_probe", min(args.records, 10000), trace=True),
    ]
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
