"""Tests for SCIP LP-backend detection (`fplan.l2.backend`)."""

from __future__ import annotations

from fplan.l2 import backend


def test_lp_algorithm_code_maps_known_labels() -> None:
    assert backend.lp_algorithm_code("barrier") == "b"
    assert backend.lp_algorithm_code("simplex") == "s"


def test_lp_algorithm_code_unknown_falls_back_to_simplex() -> None:
    # Total at the call site: a bad label maps to the simplex code, not a raise.
    assert (
        backend.lp_algorithm_code("nonsense") == backend.LP_ALGORITHM_CODES["simplex"]
    )


def test_detect_highs_prefers_barrier(monkeypatch) -> None:
    banner = "SCIP version 10.0.2 [precision: 8 byte] [LP solver: HiGHS 1.14.0] [x]"
    monkeypatch.setattr(backend, "_scip_version_banner", lambda: banner)
    b = backend.detect_backend()
    assert b.available and b.is_highs
    assert b.lp_solver == "HiGHS 1.14.0"
    assert b.lp_algorithm == "barrier"


def test_detect_soplex_prefers_simplex(monkeypatch) -> None:
    banner = "SCIP version 10.0.2 [LP solver: SoPlex 8.0.2] [GitHash: NoGitInfo]"
    monkeypatch.setattr(backend, "_scip_version_banner", lambda: banner)
    b = backend.detect_backend()
    assert b.available and not b.is_highs
    assert b.lp_solver == "SoPlex 8.0.2"
    assert b.lp_algorithm == "simplex"


def test_detect_unavailable_when_no_banner(monkeypatch) -> None:
    # pyscipopt missing / native libs unresolved → graceful, never a crash.
    monkeypatch.setattr(backend, "_scip_version_banner", lambda: None)
    b = backend.detect_backend()
    assert not b.available
    assert b.lp_solver is None
    assert b.lp_algorithm == backend.DEFAULT_LP_ALGORITHM == "simplex"


def test_detect_unrecognized_banner_is_unavailable(monkeypatch) -> None:
    # A banner without the `[LP solver: …]` field is treated as undetectable.
    monkeypatch.setattr(backend, "_scip_version_banner", lambda: "SCIP version 10.0.2")
    b = backend.detect_backend()
    assert not b.available and b.lp_algorithm == "simplex"


def test_banner_returns_none_when_model_construction_raises(monkeypatch) -> None:
    # Mirrors a broken install (pyscipopt importable but native libs unresolved):
    # the banner capture swallows it and returns None rather than crashing.
    import pyscipopt

    class _Boom:
        def __init__(self) -> None:
            raise RuntimeError("native SCIP library not found")

    monkeypatch.setattr(pyscipopt, "Model", _Boom)
    assert backend._scip_version_banner() is None


def test_banner_captures_real_scip_version() -> None:
    # Integration with the installed pyscipopt: the banner names an LP solver and
    # detection classifies it. (CI always has pyscipopt; if it ever doesn't, the
    # capture returns None and detection still yields a clean simplex default.)
    b = backend.detect_backend()
    if b.available:
        assert b.lp_solver
        assert b.lp_algorithm in backend.VALID_LP_ALGORITHMS
    else:
        assert b.lp_algorithm == "simplex"
