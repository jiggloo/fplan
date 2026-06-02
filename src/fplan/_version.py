"""Single source of truth for the package version.

This literal is read at build time by Hatchling (see ``[tool.hatch.version]``
in ``pyproject.toml``) and re-exported as ``fplan.__version__`` for runtime
access. Keep this the *only* place the version literal appears — every other
consumer (the installed distribution metadata, release tooling, docs) must
derive from it.
"""

__version__ = "0.0.3"
