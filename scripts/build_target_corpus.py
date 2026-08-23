#!/usr/bin/env python3
"""Build the committed native ESI v2006 corpus for every supported embedding.

Run this only from a real ESI/OpenCFD v2006 installation.  The resulting
``database/esi-v2006`` directory is the versioned, default corpus shipped with
the project; deployment may still override it with
``FOAMAGENT_ESI_V2006_DATABASE_PATH``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
INDEX_CONFIGURATIONS = (
    ("huggingface", "Qwen/Qwen3-Embedding-0.6B"),
    ("huggingface", "Qwen/Qwen3-Embedding-8B"),
    ("openai", "text-embedding-3-small"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build all committed FAISS indices for one native OpenFOAM target."
    )
    parser.add_argument("--openfoam-path", required=True)
    parser.add_argument(
        "--database-path",
        default=str(ROOT / "database" / "esi-v2006"),
        help="Target corpus root (default: database/esi-v2006).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate tutorial raw data and the first configured index.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    for index, (provider, model) in enumerate(INDEX_CONFIGURATIONS):
        command = [
            sys.executable,
            str(ROOT / "init_database.py"),
            "--openfoam_target",
            "esi-v2006",
            "--openfoam_path",
            args.openfoam_path,
            "--database_path",
            args.database_path,
            "--embedding_provider",
            provider,
            "--embedding_model",
            model,
        ]
        if args.force and index == 0:
            command.append("--force")
        print("Building", provider, model)
        subprocess.run(command, check=True, cwd=ROOT)

    manifest_path = Path(args.database_path).expanduser().resolve() / "raw" / "foamagent_target.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["embedding_indices"] = [
        {"provider": provider, "model": model}
        for provider, model in INDEX_CONFIGURATIONS
    ]
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
