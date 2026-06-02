"""File references for provenance.

A *reference* records where an input came from without copying its content:
its path plus a content hash. Downstream artifacts keep references to their
inputs so drift is detectable — the source changed after the reference was
recorded. Two consumers share this shape:

  - a tech-order's ``scenario:`` field (the scenario it was built from), and
  - a run manifest's ``inputs`` bindings (scenario / tech-order / map).

Keeping the shape in one place is the point: ``{path, sha256}`` (with an
optional human-readable ``name``) means "the file at this path hashed to this
at record time". Paths are stored verbatim as given on the command line —
typically repository-relative — and re-resolved relative to the working
directory when a reference is re-checked.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_of(path: str | Path) -> str:
    """Hex SHA-256 of a file's bytes."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def file_ref(path: str | Path, *, name: str | None = None) -> dict:
    """A ``{path, sha256}`` reference for ``path`` (with an optional ``name``).

    ``name`` is leading metadata for readability (e.g. a scenario's own
    ``name:``); it never participates in drift detection.
    """
    ref: dict = {"path": str(path), "sha256": sha256_of(path)}
    if name:
        return {"name": name, **ref}
    return ref


def is_current(ref: object) -> bool | None:
    """Whether the referenced file still matches its recorded hash.

    ``True`` matches, ``False`` drifted. ``None`` means "can't tell" — the
    reference isn't a mapping, the file is missing, or there's no recorded
    hash — so callers can render a distinct state rather than conflating it
    with a match.
    """
    if not isinstance(ref, dict):
        return None
    path = ref.get("path")
    sha = ref.get("sha256")
    if not path or not sha:
        return None
    p = Path(path)
    if not p.exists():
        return None
    return sha256_of(p) == sha
