"""Tests for the L2 multi-seed search fan-out (`fplan.l2.search`).

The real solve is mocked. The parallel path is exercised with an inline,
in-process stand-in for ``ProcessPoolExecutor`` — real worker processes (spawned
on macOS / forkserver on Linux) would not see the monkeypatched solve, and a
genuine multi-process SCIP run is a manual integration test, not a CI one. The
inline executor still drives the exact ``submit`` / ``as_completed`` /
initializer wiring the real pool uses.
"""

from __future__ import annotations

import concurrent.futures as cf
import types
from pathlib import Path

from fplan.l2 import search
from fplan.l2 import solve as l2_solve


class _InlineExecutor:
    """Synchronous stand-in for ProcessPoolExecutor: runs the initializer and
    each task in-process, returning already-resolved Futures."""

    def __init__(self, max_workers=None, initializer=None, initargs=()):
        self.max_workers = max_workers
        if initializer is not None:
            initializer(*initargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def submit(self, fn, *args, **kw):
        fut: cf.Future = cf.Future()
        try:
            fut.set_result(fn(*args, **kw))
        except Exception as exc:  # mirror a worker raising
            fut.set_exception(exc)
        return fut


class _CrashingExecutor(_InlineExecutor):
    """Stand-in whose every task future fails — models a dead worker pool."""

    def submit(self, fn, *args, **kw):
        fut: cf.Future = cf.Future()
        fut.set_exception(RuntimeError("pool died"))
        return fut


def _mock_solve(monkeypatch, objective_of=lambda seed: float(seed)):
    def _solve(inst, model, *, seed, **kw):
        sol = types.SimpleNamespace(
            objective=objective_of(seed), status="optimal", solve_time_s=1.0, seed=None
        )
        return sol, None, None

    monkeypatch.setattr(l2_solve, "solve", _solve)
    monkeypatch.setattr(
        l2_solve, "write_solution", lambda i, s, m, p: Path(p).write_text("stub\n")
    )


# --- resolve_jobs ----------------------------------------------------------


def test_resolve_jobs_explicit_clamped() -> None:
    assert search.resolve_jobs(1, 5) == 1
    assert search.resolve_jobs(3, 5) == 3
    assert search.resolve_jobs(10, 4) == 4  # capped at the seed count
    assert search.resolve_jobs(0, 5) == 1  # floored at 1


def test_resolve_jobs_auto() -> None:
    auto = search.resolve_jobs(None, 2)
    assert 1 <= auto <= 2  # cores, capped by seed count


# --- _solve_and_write ------------------------------------------------------


def test_solve_and_write_records(monkeypatch, tmp_path) -> None:
    _mock_solve(monkeypatch, objective_of=lambda s: 200.0)
    res = search._solve_and_write(None, None, 7, {}, tmp_path / "seed-7.yaml")
    assert res.seed == 7 and res.objective_s == 200.0 and res.status == "optimal"
    assert res.file == "seed-7.yaml" and res.error is None
    assert (tmp_path / "seed-7.yaml").exists()


def test_solve_and_write_infeasible(monkeypatch, tmp_path) -> None:
    def _solve(inst, model, *, seed, **kw):
        return (
            types.SimpleNamespace(
                objective=None, status="infeasible", solve_time_s=0.5, seed=None
            ),
            None,
            None,
        )

    monkeypatch.setattr(l2_solve, "solve", _solve)
    monkeypatch.setattr(
        l2_solve, "write_solution", lambda i, s, m, p: Path(p).write_text("stub\n")
    )
    res = search._solve_and_write(None, None, 4, {}, tmp_path / "seed-4.yaml")
    assert res.objective_s is None and res.status == "infeasible" and res.error is None
    assert res.file == "seed-4.yaml"


def test_solve_and_write_error_captured(monkeypatch, tmp_path) -> None:
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(l2_solve, "solve", boom)
    res = search._solve_and_write(None, None, 3, {}, tmp_path / "seed-3.yaml")
    assert res.status == "error" and res.error == "kaboom"
    assert res.objective_s is None and res.file is None


def test_solve_and_write_write_failure_is_errored(monkeypatch, tmp_path) -> None:
    _mock_solve(monkeypatch)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(l2_solve, "write_solution", boom)
    res = search._solve_and_write(None, None, 9, {}, tmp_path / "seed-9.yaml")
    assert res.status == "error" and "disk full" in (res.error or "")


# --- run_search ------------------------------------------------------------


def test_run_search_serial_preserves_order(monkeypatch, tmp_path) -> None:
    _mock_solve(monkeypatch)
    res = list(
        search.run_search(
            None, None, [3, 1, 2], solver_kwargs={}, search_dir=tmp_path, jobs=1
        )
    )
    assert [r.seed for r in res] == [3, 1, 2]
    assert all(r.error is None for r in res)
    assert (tmp_path / "seed-1.yaml").exists()


def test_run_search_parallel_inline(monkeypatch, tmp_path) -> None:
    _mock_solve(monkeypatch)
    monkeypatch.setattr(search, "ProcessPoolExecutor", _InlineExecutor)
    res = list(
        search.run_search(
            "INST", "MODEL", [1, 2, 3], solver_kwargs={}, search_dir=tmp_path, jobs=2
        )
    )
    assert {r.seed for r in res} == {1, 2, 3}
    assert all(r.error is None for r in res)
    assert (tmp_path / "seed-2.yaml").exists()


def test_run_search_parallel_worker_crash(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(search, "ProcessPoolExecutor", _CrashingExecutor)
    res = list(
        search.run_search("I", "M", [5], solver_kwargs={}, search_dir=tmp_path, jobs=2)
    )
    assert len(res) == 1 and res[0].seed == 5
    assert res[0].status == "error" and "pool died" in (res[0].error or "")
