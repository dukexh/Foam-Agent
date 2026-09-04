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
CORPUS_SCHEMA_VERSION = 1
ESI_V2006_SOURCE_URL = "https://dl.openfoam.com/source/v2006/OpenFOAM-v2006.tgz"
ESI_V2006_SOURCE_MD5 = "1226d48e74a4c78f12396cb586c331d8"
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
        "--tutorials-path",
        default="",
        help="Optional OpenFOAM-v2006 tutorials root when it is outside the runtime tree.",
    )
    parser.add_argument(
        "--database-path",
        default=str(ROOT / "database" / "esi-v2006"),
        help="Target corpus root (default: database/esi-v2006).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate tutorial raw data and every configured FAISS index.",
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
        if args.tutorials_path:
            command.extend(["--tutorials_path", args.tutorials_path])
        if args.force:
            command.append("--force")
        print("Building", provider, model)
        subprocess.run(command, check=True, cwd=ROOT)

    manifest_path = Path(args.database_path).expanduser().resolve() / "raw" / "foamagent_target.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["corpus_schema_version"] = CORPUS_SCHEMA_VERSION
    manifest["source"] = {
        "archive_url": ESI_V2006_SOURCE_URL,
        "archive_md5": ESI_V2006_SOURCE_MD5,
        "release": "ESI/OpenCFD OpenFOAM v2006",
    }
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
