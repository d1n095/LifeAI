"""Read-only bottleneck observation — an observation is not a diagnosis.

Slow inference does not automatically mean insufficient RAM. This module samples the host
from ``/proc`` and ``shutil`` only (no psutil, no Docker socket, no network, no service
restart), reports each measured signal separately, and refuses to name a single cause from
one sample.

The output distinguishes:
  * ``observations`` — measured values with their thresholds,
  * ``candidate_causes`` — causes consistent with the observations,
  * ``diagnosis`` — always ``None`` unless a caller supplies repeated measurements,
  * ``hardware_upgrade_justified`` — always ``False`` from one sample.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class ResourceSample:
    captured_at: str
    load_1m: float | None
    load_5m: float | None
    load_15m: float | None
    cpu_count: int | None
    mem_total_kb: int | None
    mem_available_kb: int | None
    swap_total_kb: int | None
    swap_free_kb: int | None
    disk_total_bytes: int | None
    disk_free_bytes: int | None
    source: str = "proc_and_disk"

    @property
    def memory_pressure(self) -> float | None:
        if not self.mem_total_kb or self.mem_available_kb is None:
            return None
        return round(1.0 - (self.mem_available_kb / self.mem_total_kb), 4)

    @property
    def swap_used_ratio(self) -> float | None:
        if not self.swap_total_kb or self.swap_free_kb is None:
            return None
        return round(1.0 - (self.swap_free_kb / self.swap_total_kb), 4)

    @property
    def load_per_cpu(self) -> float | None:
        if self.load_1m is None or not self.cpu_count:
            return None
        return round(self.load_1m / self.cpu_count, 3)

    @property
    def disk_used_ratio(self) -> float | None:
        if not self.disk_total_bytes or self.disk_free_bytes is None:
            return None
        return round(1.0 - (self.disk_free_bytes / self.disk_total_bytes), 4)

    def to_dict(self) -> dict:
        return {
            "captured_at": self.captured_at,
            "load_1m": self.load_1m,
            "load_5m": self.load_5m,
            "load_15m": self.load_15m,
            "cpu_count": self.cpu_count,
            "mem_total_kb": self.mem_total_kb,
            "mem_available_kb": self.mem_available_kb,
            "swap_total_kb": self.swap_total_kb,
            "swap_free_kb": self.swap_free_kb,
            "disk_total_bytes": self.disk_total_bytes,
            "disk_free_bytes": self.disk_free_bytes,
            "memory_pressure": self.memory_pressure,
            "swap_used_ratio": self.swap_used_ratio,
            "load_per_cpu": self.load_per_cpu,
            "disk_used_ratio": self.disk_used_ratio,
            "source": self.source,
        }


def _meminfo(proc_root: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in (proc_root / "meminfo").read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                values[key.strip()] = int(parts[0])
    except OSError:
        return {}
    return values


def sample_host_resources(*, proc_root: str | Path = "/proc", disk_path: str | Path = "/") -> ResourceSample:
    """One best-effort read-only sample. Missing files become ``None``, never zero."""
    root = Path(proc_root)
    load: list[float] = []
    try:
        raw = (root / "loadavg").read_text(encoding="utf-8").split()
        load = [float(part) for part in raw[:3]]
    except (OSError, ValueError):
        pass
    meminfo = _meminfo(root)
    try:
        usage = shutil.disk_usage(str(disk_path))
        disk_total, disk_free = usage.total, usage.free
    except OSError:
        disk_total, disk_free = None, None
    return ResourceSample(
        captured_at=datetime.now(timezone.utc).isoformat(),
        load_1m=load[0] if len(load) > 0 else None,
        load_5m=load[1] if len(load) > 1 else None,
        load_15m=load[2] if len(load) > 2 else None,
        cpu_count=_count_cpus(root),
        mem_total_kb=meminfo.get("MemTotal"),
        mem_available_kb=meminfo.get("MemAvailable"),
        swap_total_kb=meminfo.get("SwapTotal"),
        swap_free_kb=meminfo.get("SwapFree"),
        disk_total_bytes=disk_total,
        disk_free_bytes=disk_free,
    )


def _count_cpus(proc_root: Path) -> int | None:
    try:
        text = (proc_root / "cpuinfo").read_text(encoding="utf-8")
    except OSError:
        return None
    count = sum(1 for line in text.splitlines() if line.startswith("processor"))
    return count or None


def _severity(value: float, *, high: float, critical: float) -> str:
    if value >= critical:
        return "critical"
    if value >= high:
        return "high"
    return "normal"


def assess_bottleneck(
    sample: ResourceSample,
    *,
    provider_failures: int | None = None,
    queue_delay_seconds: float | None = None,
    model_latency_ms: float | None = None,
    db_slow_query_count: int | None = None,
    repeated_test_failures: int | None = None,
    retry_count: int | None = None,
) -> dict:
    """Report measured signals and plausible causes — never a single-sample diagnosis."""
    observations: list[dict] = []

    def observe(name: str, value: float | None, unit: str, high: float, critical: float) -> None:
        if value is None:
            observations.append({"signal": name, "observed": None, "unit": unit, "severity": "unknown"})
            return
        observations.append(
            {
                "signal": name,
                "observed": value,
                "unit": unit,
                "high_threshold": high,
                "critical_threshold": critical,
                "severity": _severity(value, high=high, critical=critical),
            }
        )

    observe("memory_pressure", sample.memory_pressure, "ratio", 0.90, 0.97)
    observe("swap_used_ratio", sample.swap_used_ratio, "ratio", 0.25, 0.60)
    observe("load_per_cpu", sample.load_per_cpu, "ratio", 1.5, 2.5)
    observe("disk_used_ratio", sample.disk_used_ratio, "ratio", 0.90, 0.97)

    for name, value, unit, high, critical in (
        ("provider_failures", provider_failures, "count", 3, 10),
        ("queue_delay_seconds", queue_delay_seconds, "seconds", 30.0, 120.0),
        ("model_latency_ms", model_latency_ms, "ms", 5000.0, 20000.0),
        ("db_slow_query_count", db_slow_query_count, "count", 1, 10),
        ("repeated_test_failures", repeated_test_failures, "count", 2, 5),
        ("retry_count", retry_count, "count", 3, 10),
    ):
        if value is not None:
            observe(name, float(value), unit, float(high), float(critical))

    by_signal = {item["signal"]: item for item in observations}
    memory = by_signal["memory_pressure"]["severity"]
    swap = by_signal["swap_used_ratio"]["severity"]
    load = by_signal["load_per_cpu"]["severity"]
    disk = by_signal["disk_used_ratio"]["severity"]

    candidates: list[dict] = []
    if memory in ("high", "critical") and swap in ("high", "critical"):
        candidates.append(
            {
                "cause": "memory_pressure_with_swapping",
                "consistent_signals": ["memory_pressure", "swap_used_ratio"],
                "still_competing_with": ["model_size", "context_length", "competing_workloads"],
            }
        )
    elif memory in ("high", "critical"):
        candidates.append(
            {
                "cause": "memory_pressure",
                "consistent_signals": ["memory_pressure"],
                "still_competing_with": ["page_cache_accounting", "transient_allocation"],
            }
        )
    if load in ("high", "critical"):
        candidates.append(
            {
                "cause": "cpu_contention",
                "consistent_signals": ["load_per_cpu"],
                "still_competing_with": ["model_size", "context_length", "provider_latency"],
            }
        )
    if disk in ("high", "critical"):
        candidates.append(
            {
                "cause": "disk_pressure",
                "consistent_signals": ["disk_used_ratio"],
                "still_competing_with": ["log_growth", "artifact_retention"],
            }
        )
    for name in ("provider_failures", "model_latency_ms"):
        if by_signal.get(name, {}).get("severity") in ("high", "critical"):
            candidates.append(
                {
                    "cause": "provider_latency_or_failure",
                    "consistent_signals": [name],
                    "still_competing_with": ["network_path", "provider_side_load", "retry_storms"],
                }
            )

    return {
        "captured_at": sample.captured_at,
        "observations": observations,
        "candidate_causes": candidates,
        "diagnosis": None,
        "diagnosis_withheld_because": (
            "a single sample cannot distinguish CPU saturation, model size, context length, "
            "swapping, provider latency or competing workloads"
        ),
        "hardware_upgrade_justified": False,
        "hardware_upgrade_note": (
            "no single observation justifies a hardware purchase; collect a time series across "
            "the suspected window first"
        ),
        "recommended_next_step": (
            "measure the suspected signal over time and correlate it with the slow request; "
            "do not upgrade hardware on one sample"
        ),
        "read_only": True,
        "service_restart_required": False,
        "execution_authorized": False,
        "sample": sample.to_dict(),
    }
