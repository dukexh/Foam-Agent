#!/usr/bin/env python3
"""Build a Foam-Agent image for one explicit OpenFOAM target.

The default remains Foundation v10.  Native ESI v2006 is selected only by
``--openfoam-target esi-v2006``; environment variables at container runtime
cannot swap an image's compiled OpenFOAM installation.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parent.parent
DOCKERFILES = {
    "foundation-v10": ROOT / "docker" / "Dockerfile",
    "esi-v2006": ROOT / "docker" / "Dockerfile.esi-v2006",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a target-specific Foam-Agent Docker image.")
    parser.add_argument(
        "--openfoam-target",
        choices=sorted(DOCKERFILES),
        default="foundation-v10",
    )
    parser.add_argument("--tag", default="")
    args = parser.parse_args()
    tag = args.tag or f"foamagent:{args.openfoam_target}"
    subprocess.run(
        ["docker", "build", "--file", str(DOCKERFILES[args.openfoam_target]), "--tag", tag, str(ROOT)],
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
