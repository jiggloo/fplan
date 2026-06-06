"""Detect which LP solver the active SCIP (pyscipopt) is linked against.

SCIP binds exactly one LP solver at *build* time — its LP interface is chosen
when SCIP is compiled, and cannot be switched at runtime. This module reads that
solver's identity from SCIP's own version banner so `fplan init` can record a
matching solver preference (see :mod:`fplan.config`).

The distinction that matters to fplan: **HiGHS** ships a barrier (interior-point)
LP method, which handles the larger `rates solve` models better than simplex;
other solvers (e.g. **SoPlex**) offer simplex only. The right LP method is
therefore a property of whichever solver the environment provides.

Detection reads the solver's *identity*, not its *behavior*: a SoPlex-linked
SCIP silently accepts the barrier setting and falls back to simplex, so probing
whether `lp/initalgorithm='b'` is "accepted" would wrongly report HiGHS.

This is the only module besides :mod:`fplan.l2.solve` that imports pyscipopt, and
it does so lazily (inside the probe) so importing it never forces the dependency
or its native libraries to load — `fplan init` must degrade cleanly when SCIP is
missing or broken, never crash.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass

# Human-facing LP-method labels (what the config stores) → the SCIP code for
# `lp/initalgorithm` / `lp/resolvealgorithm`. 's' is primal simplex (SCIP's
# default, available on every backend); 'b' is the barrier/interior-point method
# (HiGHS only). These two are the choices fplan exposes.
LP_ALGORITHM_CODES = {"simplex": "s", "barrier": "b"}
# The labels a config / CLI may name, in preference order (best first).
VALID_LP_ALGORITHMS = tuple(LP_ALGORITHM_CODES)

# The safe default when no HiGHS barrier is available (or detection fails):
# simplex works on every SCIP build.
DEFAULT_LP_ALGORITHM = "simplex"

_LP_SOLVER_RE = re.compile(r"\[LP solver:\s*([^\]]+)\]")


@dataclass(frozen=True)
class Backend:
    """The detected SCIP LP backend and the LP method fplan should prefer for it.

    ``lp_solver`` is the solver's self-reported name+version (e.g. ``"HiGHS
    1.14.0"`` / ``"SoPlex 8.0.2"``), or ``None`` when SCIP isn't importable or
    the banner couldn't be read. ``available`` mirrors that: ``False`` means
    detection failed and ``lp_algorithm`` is the safe simplex default.
    """

    lp_solver: str | None
    lp_algorithm: str
    available: bool

    @property
    def is_highs(self) -> bool:
        return self.lp_solver is not None and "highs" in self.lp_solver.lower()


def lp_algorithm_code(label: str) -> str:
    """Map an LP-method label (``"barrier"``/``"simplex"``) to its SCIP code.

    Unknown labels fall back to the simplex code rather than raising — the value
    is validated when the config loads, so this stays total at the call site.
    """
    return LP_ALGORITHM_CODES.get(label, LP_ALGORITHM_CODES[DEFAULT_LP_ALGORITHM])


def _scip_version_banner() -> str | None:
    """Return SCIP's version banner text, or ``None`` if it can't be obtained.

    ``Model.printVersion()`` writes to C-level stdout (not Python's ``sys.stdout``),
    so the banner is captured by redirecting file descriptor 1 to a temp file for
    the duration of the call. Any failure — pyscipopt absent, native libraries
    unresolved, the fd dance erroring — returns ``None`` so callers degrade
    cleanly. A temp file (not a pipe) is used so a full OS pipe buffer can never
    deadlock the write.
    """
    try:
        from pyscipopt import Model
    except Exception:
        return None
    try:
        model = Model()
    except Exception:
        return None
    try:
        with tempfile.TemporaryFile(mode="w+") as tf:
            saved = os.dup(1)
            try:
                os.dup2(tf.fileno(), 1)
                model.printVersion()
            finally:
                os.dup2(saved, 1)
                os.close(saved)
            tf.seek(0)
            return tf.read()
    except (OSError, ValueError):
        return None


def detect_backend() -> Backend:
    """Identify the active SCIP's LP backend and the LP method to prefer for it.

    HiGHS → ``barrier``; any other (or undetectable) backend → ``simplex``.
    Never raises: an unavailable/unreadable SCIP yields ``Backend(None,
    "simplex", available=False)``.
    """
    banner = _scip_version_banner()
    match = _LP_SOLVER_RE.search(banner) if banner else None
    if match is None:
        return Backend(
            lp_solver=None, lp_algorithm=DEFAULT_LP_ALGORITHM, available=False
        )
    name = match.group(1).strip()
    algorithm = "barrier" if "highs" in name.lower() else "simplex"
    return Backend(lp_solver=name, lp_algorithm=algorithm, available=True)
