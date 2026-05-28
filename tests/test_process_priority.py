"""Tests for CPU sandbox and process priority."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from src.agent.plumber_config import PlumberLintConfig
from src.agent.process_priority import (
    probe_cpu_topology,
    resolve_cpu_sandbox_config,
)


def test_probe_cpu_topology_recommends_tail_cpus() -> None:
    topo = probe_cpu_topology()
    assert topo.logical_cores >= 1
    assert topo.recommended_plumber_cpus


def test_resolve_cpu_sandbox_disabled_when_low_priority_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MATRYCA_CPU_SANDBOX", raising=False)
    cfg = PlumberLintConfig(low_priority_mode=False)
    sandbox = resolve_cpu_sandbox_config(cfg)
    assert sandbox.enabled is False


def test_apply_cpu_sandbox_affinity(monkeypatch: pytest.MonkeyPatch) -> None:
    psutil = MagicMock()
    proc = MagicMock()
    proc.cpu_affinity.return_value = [0]
    psutil.Process.return_value = proc
    psutil.IOPRIO_CLASS_IDLE = 3
    monkeypatch.setitem(__import__("sys").modules, "psutil", psutil)

    from src.agent import process_priority as mod

    monkeypatch.setattr(mod, "probe_cpu_topology", lambda: mod.CpuTopology(4, 8, (0,)))
    sandbox = mod.CpuSandboxConfig(enabled=True, affinity_cpus=(0,))
    report = mod.apply_cpu_sandbox(sandbox)
    assert report.affinity_cpus == (0,)
    proc.cpu_affinity.assert_any_call([0])
