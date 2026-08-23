import os
import subprocess
import sys
import argparse
import shlex
import json
from pathlib import Path

def parse_args():
    parser = argparse.ArgumentParser(description="Initialize database for Foam-Agent project")
    parser.add_argument(
        '--openfoam_path',
        type=str,
        default=os.getenv("WM_PROJECT_DIR"),
        help="Path to OpenFOAM installation (WM_PROJECT_DIR)"
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help="Force re-generate raw tutorial dumps + FAISS indices even if files exist"
    )
    parser.add_argument(
        '--openfoam_target',
        type=str,
        default=None,
        choices=['esi-v2006'],
        help=(
            "Build a target-scoped corpus. Use esi-v2006 for native ESI/OpenCFD "
            "v2006 tutorials; omit to preserve the Foundation database path."
        ),
    )
    parser.add_argument(
        '--database_path',
        type=str,
        default='',
        help="Override the database root. Defaults to database/esi-v2006 for the ESI target.",
    )
    parser.add_argument(
        '--embedding_provider',
        type=str,
        default='',
        help="Optional embedding provider forwarded to the FAISS builders.",
    )
    parser.add_argument(
        '--embedding_model',
        type=str,
        default='',
        help="Optional embedding model forwarded to the FAISS builders.",
    )
    return parser.parse_args()

def run_command(command_str):
    """
    Execute a command string using the current terminal's input/output,
    with the working directory set to the directory of the current file.
    
    Parameters:
        command_str (str): The command to execute, e.g. "python main.py --output_dir xxxx" 
                           or "bash xxxxx.sh".
    """
    # Split the command string into a list of arguments
    args = shlex.split(command_str)
    # Set the working directory to the directory of the current file
    cwd = os.path.dirname(os.path.abspath(__file__))
    
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            check=True,
            stdout=sys.stdout,
            stderr=sys.stderr,
            stdin=sys.stdin
        )
        print(f"Finished command: Return Code {result.returncode}")
    except subprocess.CalledProcessError as e:
        print(f"Error running command: {e}")
        sys.exit(e.returncode)


def _embedding_index_dir(database_dir: Path, embedding_model: str) -> Path:
    """Return the FAISS model directory using the runtime's naming rule."""
    return database_dir / "faiss" / embedding_model.replace("/", "_").replace(":", "_")


def _read_openfoam_version(openfoam_root: str) -> str:
    """Read WM_PROJECT_VERSION from a trusted OpenFOAM installation bashrc."""
    bashrc = Path(openfoam_root).expanduser() / "etc" / "bashrc"
    if not bashrc.is_file():
        raise ValueError(f"OpenFOAM bashrc not found at: {bashrc}")
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1" && printf "%s" "${WM_PROJECT_VERSION:-}"',
            "foamagent-database-version",
            str(bashrc),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()

def main():
    args = parse_args()
    print(args)

    # Set environment variables
    WM_PROJECT_DIR = args.openfoam_path
    if not WM_PROJECT_DIR:
        raise ValueError("--openfoam_path (or WM_PROJECT_DIR) is required.")
    
    # Get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    print(f"script_dir: {script_dir}")

    default_database = Path(script_dir) / "database"
    database_dir = (
        Path(args.database_path).expanduser().resolve()
        if args.database_path
        else default_database / args.openfoam_target
        if args.openfoam_target == "esi-v2006"
        else default_database
    )
    raw_dir = database_dir / "raw"
    database_arg = shlex.quote(str(database_dir))
    output_arg = shlex.quote(str(raw_dir))
    openfoam_arg = shlex.quote(str(WM_PROJECT_DIR))
    embedding_args = ""
    if args.embedding_provider:
        embedding_args += f" --embedding_provider={shlex.quote(args.embedding_provider)}"
    if args.embedding_model:
        embedding_args += f" --embedding_model={shlex.quote(args.embedding_model)}"

    selected_model = args.embedding_model or "Qwen/Qwen3-Embedding-0.6B"
    selected_index_dir = _embedding_index_dir(database_dir, selected_model)

    if args.openfoam_target == "esi-v2006":
        runtime_version = _read_openfoam_version(str(WM_PROJECT_DIR))
        if runtime_version not in {"v2006", "2006"}:
            raise ValueError(
                "--openfoam_target esi-v2006 requires an ESI/OpenCFD v2006 "
                f"installation, but its bashrc reported WM_PROJECT_VERSION={runtime_version!r}."
            )
        # The parser uses FOAM_TUTORIALS for a small set of shared tutorial
        # resources.  Point it at the supplied v2006 tree even if the caller's
        # parent shell currently has another OpenFOAM distribution sourced.
        os.environ["WM_PROJECT_DIR"] = str(Path(WM_PROJECT_DIR).expanduser().resolve())
        os.environ["FOAM_TUTORIALS"] = str(
            Path(WM_PROJECT_DIR).expanduser().resolve() / "tutorials"
        )
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / "foamagent_target.json").write_text(
            json.dumps(
                {
                    "openfoam_target": "esi-v2006",
                    "wm_project_dir": str(Path(WM_PROJECT_DIR).expanduser().resolve()),
                    "wm_project_version": runtime_version,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    SCRIPTS = []
    
    # Preprocess the OpenFOAM tutorials
    if args.force or not (raw_dir / "openfoam_tutorials_details.txt").exists():
        SCRIPTS.append(
            "python database/script/tutorial_parser.py "
            f"--output_dir={output_arg} --wm_project_dir={openfoam_arg}"
        )

    # (Re)build FAISS indices
    # The embedding-model directory is part of the index identity.  Checking
    # only for *any* FAISS index made a second provider/model silently reuse a
    # missing target index instead of building it.
    if args.force or not (selected_index_dir / "openfoam_command_help").is_dir():
        SCRIPTS.append(f"python database/script/faiss_command_help.py --database_path={database_arg}{embedding_args}")
    if args.force or not (selected_index_dir / "openfoam_allrun_scripts").is_dir():
        SCRIPTS.append(f"python database/script/faiss_allrun_scripts.py --database_path={database_arg}{embedding_args}")
    if args.force or not (selected_index_dir / "openfoam_tutorials_structure").is_dir():
        SCRIPTS.append(f"python database/script/faiss_tutorials_structure.py --database_path={database_arg}{embedding_args}")
    if args.force or not (selected_index_dir / "openfoam_tutorials_details").is_dir():
        SCRIPTS.append(f"python database/script/faiss_tutorials_details.py --database_path={database_arg}{embedding_args}")

    if not SCRIPTS:
        print("All database files already exist. No initialization needed.")
        print("Tip: pass --force to rebuild.")
        return

    print("Starting database initialization...")
    for script in SCRIPTS:
        run_command(script)
    print("Database initialization completed successfully.")

if __name__ == "__main__":
    ## python init_database.py --openfoam_path $WM_PROJECT_DIR
    main()
