"""Version wiring: the package exposes a version, and it is single-sourced.

These guard the packaging contract — that ``fplan.__version__`` exists and
that the installed distribution metadata agrees with the in-package literal,
i.e. the version flows from one source through the build backend.
"""

import importlib.metadata

import fplan


def test_version_is_exposed():
    assert isinstance(fplan.__version__, str)
    assert fplan.__version__


def test_version_matches_installed_metadata():
    assert importlib.metadata.version("fplan") == fplan.__version__
