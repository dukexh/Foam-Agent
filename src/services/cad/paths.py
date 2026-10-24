"""CLI CAD input paths may use either Windows drive paths or WSL paths."""
from pathlib import Path
from .bridge import linux_path


def input_path(value):
    return Path(linux_path(str(value))).expanduser().resolve()
