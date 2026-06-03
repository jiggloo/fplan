"""Multi-seed search for L2 — solve a set of SCIP seeds, serial or parallel.

Each L2 solve is a heavy, single-threaded nonconvex NLP, so concurrency here
is **process-level** (one worker process per concurrent seed), not threaded:
pyscipopt is not thread-safe and the solve is CPU-bound, so threads would just
serialize on the GIL/native locks. The solver-neutral instance and the game
model pickle cheaply, so they are shipped to each worker **once** (via the pool
initializer) and reused across every seed that worker handles — only SCIP's
``randomseedshift`` varies per solve.

This module owns the fan-out and returns lightweight results; ranking, the
summary file, the promotion prompts, and all user-facing output stay in the CLI
(``fplan/cli/rates.py``), which consumes :func:`run_search` as a stream.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SeedResult:
    """One seed's outcome. ``objective_s is None`` means no feasible incumbent
    (infeasible or unstarted); ``error`` is set when the solve/write raised
    (the seed is then never promotable, but the search continues)."""

    seed: int
    status: str
    objective_s: float | None
    solve_time_s: float | None
    file: str | None
    error: str | None = None


@contextlib.contextmanager
def _redirect_fds(path: Path) -> Iterator[None]:
    """Redirect this process's stdout+stderr to ``path`` at the file-descriptor
    level (``dup2`` on fd 1/2), so a C library's output — SCIP's live progress
    table, written below Python — is captured too, not just Python ``print``s.
    Restores on exit: pool workers are reused across seeds, so the redirect must
    be scoped to a single task or the next seed would append to this seed's log.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    # Acquire inside the try (with separate assignments) so a failure partway —
    # the second os.dup, or open() on a full/read-only fs — can't leak an
    # already-duped fd. Workers are long-lived, so a recurring leak would
    # eventually exhaust the fd table.
    saved_out: int | None = None
    saved_err: int | None = None
    logf = None
    try:
        saved_out = os.dup(1)
        saved_err = os.dup(2)
        logf = open(path, "w")
        os.dup2(logf.fileno(), 1)
        os.dup2(logf.fileno(), 2)
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        if saved_out is not None:
            os.dup2(saved_out, 1)
            os.close(saved_out)
        if saved_err is not None:
            os.dup2(saved_err, 2)
            os.close(saved_err)
        if logf is not None:
            logf.close()


def _solve_and_write(
    inst, model, seed: int, solver_kwargs: dict, cand_path: Path, log_path=None
):
    """Solve one seed and write its candidate. Never raises — a solve or write
    failure comes back as an errored :class:`SeedResult` so one bad seed can't
    sink the whole search (a disk problem still surfaces fatally later, when the
    parent writes the summary).

    When ``log_path`` is given, the whole solve's output (Python + SCIP's
    C-level progress) is redirected there — the mechanism that keeps parallel
    seeds from interleaving in the console.
    """
    from fplan.l2 import solve as l2_solve

    ctx = _redirect_fds(log_path) if log_path is not None else contextlib.nullcontext()
    try:
        with ctx:
            sol, _m, _handles = l2_solve.solve(inst, model, **solver_kwargs, seed=seed)
            sol.seed = seed
            l2_solve.write_solution(inst, sol, model, cand_path)
    except Exception as exc:  # solve OR candidate-write failure → errored seed
        return SeedResult(seed, "error", None, None, None, error=str(exc))
    obj = float(sol.objective) if sol.objective is not None else None
    return SeedResult(seed, sol.status, obj, float(sol.solve_time_s), cand_path.name)


# Per-worker state: set once by the pool initializer, reused for every seed that
# lands on the worker (so the model/instance are pickled once per worker, not
# once per task).
_W_INST = None
_W_MODEL = None


def _worker_init(inst, model) -> None:
    global _W_INST, _W_MODEL
    _W_INST, _W_MODEL = inst, model


def _worker_task(seed: int, solver_kwargs: dict, cand_path_str: str, log_path_str: str):
    return _solve_and_write(
        _W_INST, _W_MODEL, seed, solver_kwargs, Path(cand_path_str), Path(log_path_str)
    )


def resolve_jobs(jobs: int | None, n_seeds: int) -> int:
    """How many seeds to solve concurrently. ``None`` → as many as CPU cores,
    capped by the seed count; an explicit value is clamped to ``[1, n_seeds]``."""
    if jobs is not None:
        return max(1, min(jobs, n_seeds))
    return max(1, min(n_seeds, os.cpu_count() or 1))


def run_search(
    inst,
    model,
    seeds,
    *,
    solver_kwargs: dict,
    search_dir: Path,
    jobs: int,
) -> Iterator[SeedResult]:
    """Yield a :class:`SeedResult` per seed as solves complete.

    ``jobs <= 1`` solves serially in-process, preserving the given seed order
    and leaving output on the console (SCIP itself stays quiet, as in a single
    solve). ``jobs > 1`` fans the seeds across that many worker processes,
    redirects each seed's output to its own ``seed-<N>.log`` (no interleaving),
    and yields in **completion** order (not seed order). A hard worker crash
    (``BrokenProcessPool`` from e.g. a native SCIP segfault) is contained — the
    search finishes rather than aborting — but it marks *all* still-outstanding
    seeds as errored, not just the one that crashed.

    Note: the genuine multi-process path (real pickling across the process
    boundary, completion-order yielding, cross-seed fd isolation) is exercised
    only by the manual integration test; CI uses an in-process executor
    stand-in plus a pickle-roundtrip guard.
    """

    def cand(sd: int) -> Path:
        return search_dir / f"seed-{sd}.yaml"

    def log(sd: int) -> Path:
        return search_dir / f"seed-{sd}.log"

    if jobs <= 1:
        for sd in seeds:
            yield _solve_and_write(inst, model, sd, solver_kwargs, cand(sd))
        return

    with ProcessPoolExecutor(
        max_workers=jobs, initializer=_worker_init, initargs=(inst, model)
    ) as ex:
        futs = {
            ex.submit(_worker_task, sd, solver_kwargs, str(cand(sd)), str(log(sd))): sd
            for sd in seeds
        }
        for fut in as_completed(futs):
            sd = futs[fut]
            try:
                yield fut.result()
            except Exception as exc:  # process died / result unpicklable
                yield SeedResult(sd, "error", None, None, None, error=str(exc))
