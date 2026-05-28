"""Apply low CPU/IO priority and optional CPU affinity for background daemon work."""

from __future__ import annotations

import contextlib
import os
import sys
from dataclasses import dataclass

from loguru import logger

from .plumber_config import PlumberLintConfig, _env_int, _map_bool, load_plumber_lint_config


@dataclass(frozen=True, slots=True)
class CpuTopology:
    """Host CPU layout used to pick a Plumber sandbox."""

    physical_cores: int
    logical_cores: int
    recommended_plumber_cpus: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CpuSandboxConfig:
    """Resolved CPU sandbox settings."""

    enabled: bool
    affinity_cpus: tuple[int, ...] | None
    nice_level: int = 19
    ionice_idle: bool = True


@dataclass(frozen=True, slots=True)
class AppliedPriorityReport:
    """Outcome of applying process priority / affinity."""

    nice_applied: bool
    ionice_applied: bool
    affinity_cpus: tuple[int, ...] | None
    topology: CpuTopology | None


def _parse_cpu_affinity_env() -> tuple[int, ...] | None:
    raw = os.environ.get("MATRYCA_PLUMBER_CPU_AFFINITY", "").strip()
    if not raw:
        return None
    cpus: list[int] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        cpus.append(int(token))
    return tuple(cpus) if cpus else None


def probe_cpu_topology() -> CpuTopology:
    """Detect physical/logical cores and suggest a small Plumber CPU set."""
    logical = os.cpu_count() or 1
    physical = logical
    try:
        import psutil

        physical = psutil.cpu_count(logical=False) or logical
    except ImportError:
        pass

    explicit = _parse_cpu_affinity_env()
    if explicit is not None:
        recommended = explicit
    elif logical <= 2:
        recommended = (0,)
    else:
        # Pin to the highest-indexed logical CPUs (often E-cores on hybrid chips).
        tail = min(2, logical - 1)
        recommended = tuple(range(logical - tail, logical))

    return CpuTopology(
        physical_cores=max(1, physical),
        logical_cores=max(1, logical),
        recommended_plumber_cpus=recommended,
    )


def resolve_cpu_sandbox_config(config: PlumberLintConfig | None = None) -> CpuSandboxConfig:
    """Build sandbox config from env and lint config."""
    if config is None:
        config = load_plumber_lint_config()
    env = os.environ
    enabled = _map_bool(env, "MATRYCA_CPU_SANDBOX", config.low_priority_mode)
    explicit = _parse_cpu_affinity_env()
    topology = probe_cpu_topology() if enabled else None
    affinity = explicit if explicit is not None else (
        topology.recommended_plumber_cpus if topology is not None else None
    )
    nice_level = max(0, min(19, _env_int("MATRYCA_PLUMBER_NICE_LEVEL", 19)))
    return CpuSandboxConfig(
        enabled=enabled and config.low_priority_mode,
        affinity_cpus=affinity,
        nice_level=nice_level,
        ionice_idle=_map_bool(env, "MATRYCA_PLUMBER_IONICE_IDLE", True),
    )


def _try_ionice_lowest() -> bool:
    try:
        import psutil
    except ImportError:
        return False
    try:
        proc = psutil.Process()
        if hasattr(proc, "ionice"):
            proc.ionice(psutil.IOPRIO_CLASS_IDLE)
            logger.debug("Applied psutil IOPRIO_CLASS_IDLE")
            return True
    except (AttributeError, OSError, ValueError) as exc:
        logger.debug("ionice not applied: {}", exc)
    return False


def _apply_windows_idle_priority() -> bool:
    try:
        import psutil
    except ImportError:
        return False
    try:
        proc = psutil.Process()
        idle = getattr(psutil, "IDLE_PRIORITY_CLASS", None)
        if idle is not None and hasattr(proc, "nice"):
            proc.nice(idle)
            logger.debug("Applied Windows IDLE_PRIORITY_CLASS")
            return True
    except (AttributeError, OSError, ValueError) as exc:
        logger.debug("Windows idle priority not applied: {}", exc)
    return False


def _apply_cpu_affinity(cpus: tuple[int, ...]) -> tuple[int, ...] | None:
    try:
        import psutil
    except ImportError:
        return None
    try:
        proc = psutil.Process()
        proc.cpu_affinity(list(cpus))
        read_back = proc.cpu_affinity()
        applied = tuple(read_back) if read_back is not None else tuple(cpus)
        logger.info("Plumber CPU affinity set to {}", applied)
        return applied
    except (AttributeError, OSError, ValueError) as exc:
        logger.debug("cpu_affinity not applied: {}", exc)
        return None


def apply_cpu_sandbox(sandbox: CpuSandboxConfig) -> AppliedPriorityReport:
    """Apply nice, ionice, and optional CPU affinity for edge hosts."""
    topology = probe_cpu_topology() if sandbox.enabled else None
    nice_applied = False
    if sandbox.enabled and hasattr(os, "nice"):
        with contextlib.suppress(OSError):
            os.nice(sandbox.nice_level)
            nice_applied = True
    elif sandbox.enabled and sys.platform == "win32":
        nice_applied = _apply_windows_idle_priority()

    ionice_applied = sandbox.ionice_idle and _try_ionice_lowest() if sandbox.enabled else False

    affinity: tuple[int, ...] | None = None
    if sandbox.enabled and sandbox.affinity_cpus:
        affinity = _apply_cpu_affinity(sandbox.affinity_cpus)

    if topology is not None and sandbox.enabled:
        logger.info(
            "CPU topology: physical={} logical={} plumber_cpus={}",
            topology.physical_cores,
            topology.logical_cores,
            affinity or sandbox.affinity_cpus,
        )

    return AppliedPriorityReport(
        nice_applied=nice_applied,
        ionice_applied=ionice_applied,
        affinity_cpus=affinity,
        topology=topology,
    )


def apply_plumber_priority(config: PlumberLintConfig | None = None) -> None:
    """Lower process priority when ``low_priority_mode`` is enabled (legacy entry)."""
    sandbox = resolve_cpu_sandbox_config(config)
    if sandbox.enabled:
        apply_cpu_sandbox(sandbox)
        return
    if config is None:
        config = load_plumber_lint_config()
    if not config.low_priority_mode:
        return
    if hasattr(os, "nice"):
        with contextlib.suppress(OSError):
            os.nice(19)
    elif sys.platform == "win32":
        _apply_windows_idle_priority()
    _try_ionice_lowest()


__all__ = [
    "AppliedPriorityReport",
    "CpuSandboxConfig",
    "CpuTopology",
    "apply_cpu_sandbox",
    "apply_plumber_priority",
    "probe_cpu_topology",
    "resolve_cpu_sandbox_config",
]
