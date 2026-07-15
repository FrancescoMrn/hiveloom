"""Well-known hiveloom filesystem locations.

Everything user-level (extensions, ``models.yaml``, the Hive DB default, and —
later — blueprints and the trust store) lives under one home directory so it
can be relocated with a single environment variable (useful for tests and CI).
"""

from __future__ import annotations

import os
from pathlib import Path


def hiveloom_home() -> Path:
    """The user-level hiveloom directory: ``$HIVELOOM_HOME`` or ``~/.hiveloom``."""
    return Path(os.environ.get("HIVELOOM_HOME", "~/.hiveloom")).expanduser()


def user_extensions_dir() -> Path:
    """Where user-level extension modules live (``*.py`` files)."""
    return hiveloom_home() / "extensions"


def models_yaml_path() -> Path:
    """The user-level model/provider declaration file."""
    return hiveloom_home() / "models.yaml"
