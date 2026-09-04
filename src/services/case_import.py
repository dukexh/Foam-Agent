"""Controlled import and execution of an existing native OpenFOAM case.

This is intentionally the single business-service boundary for Existing Case:
source materialisation, restricted Allrun parsing, non-numeric repairs, and
attempt reporting live together.  The graph node only adapts this public API.
"""

from __future__ import annotations

from difflib import unified_diff
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import shlex
import stat
import tempfile
from typing import Any, Iterable, Optional
import zipfile

from models import CaseImportError, CaseManifest, ExecutionStep
from openfoam_target import ESI_V2006, FOUNDATION_V10, controlled_allrun_runtime_guard, normalise_openfoam_target
from utils import check_foam_errors, run_command
from .allrun_commands import (
    is_standard_application_variable,
    normalise_shell_lines,
    strip_shell_comment,
)
from .openfoam_commands import MESH_MUTATING_COMMANDS
from .output_safety import (
    OutputDirectorySafetyError,
    prepare_output_directory,
    validate_output_path,
)
from .run_local import (
    validate_openfoam_case_postflight,
    validate_openfoam_case_preflight,
)


MAX_ARCHIVE_FILES = 10_000
MAX_ARCHIVE_BYTES = 1_000_000_000
TEXT_SAMPLE_BYTES = 128_000
_APPLICATION_RE = re.compile(r"\bapplication\s+([^\s;]+)\s*;")
_VERSION_RE = re.compile(r"\bVersion\s*:\s*([vV]?\d+(?:\.\d+)?)")
_CONTROL_ENTRY_RE = re.compile(r"\b(?P<key>startFrom|startTime)\s+(?P<value>[^\s;]+)\s*;")
_TIME_DIRECTORY_RE = re.compile(r"^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?$")
_SAFE_COMMAND_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_SAFE_ARGUMENT_RE = re.compile(r"^[A-Za-z0-9_./:=+@%$,-]+$")
_SHELL_META_RE = re.compile(r'[;&|`<>]')
_NUMBER_RE = re.compile(r"(?<![A-Za-z_])[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?(?![A-Za-z_])")
_FORBIDDEN_DYNAMIC_CODE_RE = re.compile(
    r"#\s*(?:codeStream|include|includeEtc|includeFunc)\b|"
    r"\b(?:coded|codeExecute|codeInclude|codeLibs|codeOptions)\b",
    re.IGNORECASE,
)
_LIBRARY_DECLARATION_RE = re.compile(r"\blibs\s*\(", re.IGNORECASE)
_ALLOWED_UTILITIES = frozenset({
    "blockMesh", "checkMesh", "createBaffles", "createNonConformalCouples",
    "decomposePar", "fluentMeshToFoam", "gmshToFoam", "reconstructPar",
    "setFields", "snappyHexMesh", "splitBaffles", "splitMeshRegions", "topoSet",
})
_ALLOWED_UTILITIES_BY_PLATFORM = {
    FOUNDATION_V10: _ALLOWED_UTILITIES,
    "foundation-v10-compatible": _ALLOWED_UTILITIES,
    "esi": _ALLOWED_UTILITIES,
    ESI_V2006: _ALLOWED_UTILITIES,
}
_NON_NUMERIC_SEMICOLON_KEYS = frozenset({
    "application", "continuousPhase", "continuousPhaseName", "runTimeModifiable",
    "simulationType", "startFrom", "stopAt", "transportModel", "viscosityModel",
    "writeControl", "writeFormat",
})


def _read_text(path: Path, *, limit: Optional[int] = None) -> str:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit) if limit is not None else handle.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _strip_comments(content: str) -> str:
    return re.sub(r"//.*$", "", re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL), flags=re.MULTILINE)


def iter_regular_files(root: Path) -> Iterable[Path]:
    """Yield files without following links; imported links are never trusted."""
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise CaseImportError(f"Symbolic links are not supported in imported cases: {path}")
        if path.is_file():
            yield path


def _validate_tree(root: Path) -> None:
    if not root.is_dir():
        raise CaseImportError(f"Case path is not a directory: {root}")
    size = 0
    count = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise CaseImportError(f"Symbolic links are not supported in imported cases: {path}")
        if path.name == ".foamagent" or ".foamagent" in path.parts:
            raise CaseImportError("Imported cases cannot contain the reserved .foamagent directory.")
        if not path.is_file():
            continue
        count += 1
        size += path.stat().st_size
        if count > MAX_ARCHIVE_FILES or size > MAX_ARCHIVE_BYTES:
            raise CaseImportError("Case exceeds the existing-case import safety limit.")


def _normalise_zip_member(entry: zipfile.ZipInfo) -> Optional[PurePosixPath]:
    """Validate one ZIP member before anything is written to disk."""
    name = entry.filename
    if (
        not name
        or "\x00" in name
        or "\\" in name
        or re.match(r"^[A-Za-z]:", name)
    ):
        raise CaseImportError(f"ZIP entry uses an unsupported path: {name!r}")
    member = PurePosixPath(name)
    if member.is_absolute() or name.startswith(("/", "\\")) or ".." in member.parts:
        raise CaseImportError(f"ZIP entry escapes the import directory: {name!r}")
    if stat.S_ISLNK(entry.external_attr >> 16):
        raise CaseImportError(f"ZIP symbolic links are not supported: {name!r}")
    if not member.parts:
        if entry.is_dir():
            return None
        raise CaseImportError(f"ZIP entry has an empty path: {name!r}")
    return member


def _extract_zip(archive: Path, destination: Path) -> None:
    """Extract an archive with traversal, link, collision, and size checks."""
    try:
        with zipfile.ZipFile(archive) as zip_file:
            entries = zip_file.infolist()
            if len(entries) > MAX_ARCHIVE_FILES:
                raise CaseImportError(
                    f"ZIP contains {len(entries)} entries; the limit is {MAX_ARCHIVE_FILES}."
                )
            if sum(entry.file_size for entry in entries) > MAX_ARCHIVE_BYTES:
                raise CaseImportError("ZIP uncompressed size exceeds the 1 GB import safety limit.")

            destination_root = destination.resolve()
            seen: set[PurePosixPath] = set()
            extracted_bytes = 0
            for entry in entries:
                member = _normalise_zip_member(entry)
                if member is None:
                    continue
                if member in seen:
                    raise CaseImportError(f"ZIP contains duplicate member path: {entry.filename!r}")
                seen.add(member)
                target = destination.joinpath(*member.parts)
                try:
                    target.resolve().relative_to(destination_root)
                except ValueError as exc:
                    raise CaseImportError(
                        f"ZIP entry escapes the import directory: {entry.filename!r}"
                    ) from exc

                if entry.is_dir():
                    if target.exists() and not target.is_dir():
                        raise CaseImportError(f"ZIP member conflicts with an existing path: {entry.filename!r}")
                    target.mkdir(parents=True, exist_ok=True)
                    continue

                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        raise CaseImportError(f"ZIP member conflicts with an existing path: {entry.filename!r}")
                    with zip_file.open(entry, "r") as source, target.open("xb") as output:
                        while chunk := source.read(64 * 1024):
                            extracted_bytes += len(chunk)
                            if extracted_bytes > MAX_ARCHIVE_BYTES:
                                raise CaseImportError(
                                    "ZIP uncompressed size exceeds the 1 GB import safety limit."
                                )
                            output.write(chunk)
                except CaseImportError:
                    target.unlink(missing_ok=True)
                    raise
                except OSError as exc:
                    target.unlink(missing_ok=True)
                    raise CaseImportError(
                        f"Unable to extract ZIP member {entry.filename!r}: {exc}"
                    ) from exc
    except zipfile.BadZipFile as exc:
        raise CaseImportError(f"Invalid ZIP archive: {archive}") from exc


def _find_case_root(root: Path, case_subdir: Optional[str]) -> Path:
    if case_subdir:
        relative = PurePosixPath(case_subdir.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise CaseImportError("--case_subdir must be relative to the imported input.")
        selected = (root / Path(*relative.parts)).resolve()
        try:
            selected.relative_to(root.resolve())
        except ValueError as exc:
            raise CaseImportError("--case_subdir escapes the imported input.") from exc
        if not (selected / "system" / "controlDict").is_file():
            raise CaseImportError(f"--case_subdir={case_subdir!r} does not contain system/controlDict.")
        return selected
    cases = sorted(path.parent.parent for path in root.rglob("controlDict") if path.parent.name == "system")
    if not cases:
        raise CaseImportError("No system/controlDict found in the imported case.")
    if len(cases) != 1:
        raise CaseImportError("Multiple OpenFOAM cases found in the import. Specify --case_subdir.")
    return cases[0]


def _application(case_root: Path) -> str:
    match = _APPLICATION_RE.search(_strip_comments(_read_text(case_root / "system" / "controlDict")))
    if not match or not _SAFE_COMMAND_RE.fullmatch(match.group(1)):
        raise CaseImportError("system/controlDict must define a safe application token.")
    return match.group(1)


def _detect_platform(case_root: Path) -> tuple[str, Optional[str]]:
    evidence = [case_root / "system" / "controlDict"]
    evidence.extend(
        path
        for path in iter_regular_files(case_root)
        if path.name in {"blockMeshDict", "fvSolution", "fvSchemes"} and path not in evidence
    )
    text = "\n".join(_read_text(path, limit=TEXT_SAMPLE_BYTES) for path in evidence)
    version_match = _VERSION_RE.search(text)
    detected_version = version_match.group(1) if version_match else None
    lowered = text.lower()
    if "openfoam.com" in lowered:
        return "esi", detected_version
    if "openfoam.org" in lowered:
        return (FOUNDATION_V10 if detected_version and detected_version.lstrip("vV") == "10" else "foundation-other", detected_version)
    if any((case_root / "constant").glob("momentumTransport*")):
        return "foundation-v10-compatible", detected_version
    return "unknown", detected_version


def _mesh_state(case_root: Path) -> str:
    if (case_root / "constant" / "polyMesh").is_dir():
        return "existing-polyMesh"
    if (case_root / "system" / "blockMeshDict").is_file():
        return "blockMesh"
    return "missing"


def _synthesised_startup_issue(case_root: Path) -> Optional[str]:
    """Reject inferred plans whose requested restart time is not present."""
    content = _strip_comments(_read_text(case_root / "system" / "controlDict"))
    entries = {
        match.group("key"): match.group("value")
        for match in _CONTROL_ENTRY_RE.finditer(content)
    }
    start_from = entries.get("startFrom")
    start_time = entries.get("startTime")
    time_directories = [
        path.name
        for path in case_root.iterdir()
        if path.is_dir() and _TIME_DIRECTORY_RE.fullmatch(path.name)
    ]

    if start_from in {"latestTime", "firstTime"}:
        if not time_directories:
            return (
                "Allrun is missing, but controlDict requests "
                f"startFrom {start_from} with no numeric time directory. "
                "Provide an explicit Allrun/setup stage."
            )
        return None
    if start_from not in {None, "startTime"} or not start_time:
        return None
    try:
        expected_time = float(start_time)
    except ValueError:
        return (
            "Allrun is missing and controlDict has an unsupported startTime "
            f"token {start_time!r}; provide an explicit Allrun."
        )
    if expected_time == 0 or any(
        abs(float(directory) - expected_time) <= 1e-12
        for directory in time_directories
    ):
        return None
    return (
        "Allrun is missing, but controlDict starts from time "
        f"{start_time} and that numeric time directory is absent. "
        "Provide the required setup stage or an explicit Allrun."
    )


def _hash_files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in iter_regular_files(root)
    }


def _dependency_scan_files(case_root: Path) -> Iterable[Path]:
    """Yield only dictionary inputs that can affect imported execution."""
    for path in iter_regular_files(case_root):
        relative = path.relative_to(case_root)
        if path.name == "Allrun":
            yield path
        elif relative.parts and relative.parts[0] in {"0", "constant", "system"}:
            yield path
        elif relative.parts and _TIME_DIRECTORY_RE.fullmatch(relative.parts[0]):
            yield path


def _iter_comment_free_chunks(path: Path) -> Iterable[str]:
    """Stream OpenFOAM text while ignoring comments across chunk boundaries."""
    in_block_comment = False
    in_line_comment = False
    pending = ""
    try:
        with path.open("rb") as handle:
            while raw_chunk := handle.read(64 * 1024):
                text = pending + raw_chunk.decode("utf-8", errors="replace")
                pending = ""
                output: list[str] = []
                index = 0
                while index < len(text) - 1:
                    character, following = text[index], text[index + 1]
                    if in_line_comment:
                        if character == "\n":
                            in_line_comment = False
                            output.append(character)
                        index += 1
                    elif in_block_comment:
                        if character == "*" and following == "/":
                            in_block_comment = False
                            index += 2
                        else:
                            if character == "\n":
                                output.append(character)
                            index += 1
                    elif character == "/" and following == "/":
                        in_line_comment = True
                        index += 2
                    elif character == "/" and following == "*":
                        in_block_comment = True
                        index += 2
                    else:
                        output.append(character)
                        index += 1
                pending = text[index:]
                if output:
                    yield "".join(output)
            if pending and not in_block_comment and not in_line_comment:
                yield pending
    except OSError as exc:
        raise CaseImportError(f"Unable to inspect imported file {path}: {exc}") from exc


def _file_contains_pattern(path: Path, pattern: re.Pattern[str]) -> bool:
    overlap = ""
    for chunk in _iter_comment_free_chunks(path):
        text = overlap + chunk
        if pattern.search(text):
            return True
        overlap = text[-512:]
    return False


def _extract_libraries(case_root: Path) -> list[str]:
    libraries: set[str] = set()
    for path in _dependency_scan_files(case_root):
        content = _strip_comments(_read_text(path, limit=TEXT_SAMPLE_BYTES))
        for block in re.findall(r"\blibs\s*\((.*?)\)", content, re.DOTALL):
            libraries.update(re.findall(r"['\"]([^'\"]+\.so)['\"]", block))
    return sorted(libraries)


def _reject_dynamic_dependencies(case_root: Path, allrun: str) -> list[str]:
    issues: list[str] = []
    if any(path.name in {"Allwmake", "Make"} for path in case_root.rglob("*")):
        issues.append("case contains a Make/ or Allwmake build dependency")
    allrun_code = "\n".join(strip_shell_comment(line) for line in allrun.splitlines())
    if re.search(r"\b(?:wmake|make|cmake)\b", allrun_code):
        issues.append("Allrun requests compilation")

    for path in _dependency_scan_files(case_root):
        if _file_contains_pattern(path, _FORBIDDEN_DYNAMIC_CODE_RE):
            issues.append(f"dynamic code found in {path.relative_to(case_root)}")
            break
    libraries = _extract_libraries(case_root)
    if libraries or any(
        _file_contains_pattern(path, _LIBRARY_DECLARATION_RE)
        for path in _dependency_scan_files(case_root)
    ):
        issues.append(
            "case declares dynamically loaded libraries, which are not supported "
            "by safe case-import mode: "
            + (", ".join(libraries) if libraries else "unparsed declaration")
        )
    return issues


def _is_supported_setup_line(line: str) -> bool:
    normalised = re.sub(r"\s+", " ", line).strip()
    return normalised in {
        ". $WM_PROJECT_DIR/bin/tools/RunFunctions",
        '. "$WM_PROJECT_DIR/bin/tools/RunFunctions"',
        '. ${WM_PROJECT_DIR:?}/bin/tools/RunFunctions',
        '. "${WM_PROJECT_DIR:?}/bin/tools/RunFunctions"',
        "source $WM_PROJECT_DIR/bin/tools/RunFunctions",
        'source "$WM_PROJECT_DIR/bin/tools/RunFunctions"',
        "source ${WM_PROJECT_DIR:?}/bin/tools/RunFunctions",
        'source "${WM_PROJECT_DIR:?}/bin/tools/RunFunctions"',
        "cd ${0%/*}",
        'cd "${0%/*}"',
        "cd ${0%/*} || exit",
        'cd "${0%/*}" || exit',
        "cd ${0%/*} || exit 1",
        'cd "${0%/*}" || exit 1',
    }


def _safe_tokens(command: str, args: Iterable[str]) -> None:
    if not _SAFE_COMMAND_RE.fullmatch(command):
        raise CaseImportError(f"Unsafe command in Allrun: {command!r}")
    for arg in args:
        if arg == "-case" or arg.startswith("-case="):
            raise CaseImportError("Allrun -case arguments are not supported in safe case-import mode.")
        if not _SAFE_ARGUMENT_RE.fullmatch(arg) or _SHELL_META_RE.search(arg):
            raise CaseImportError(f"Unsafe argument in Allrun: {arg!r}")
        values = (arg.split("=", 1)[-1],) if "=" in arg else (arg,)
        if any(value.startswith("/") for value in values):
            raise CaseImportError(f"Absolute paths are not allowed in imported Allrun: {arg!r}")
        if any(".." in PurePosixPath(value).parts for value in values):
            raise CaseImportError(f"Parent-directory traversal is not allowed in Allrun: {arg!r}")
        if "$" in arg:
            raise CaseImportError(f"Unsupported shell expansion in Allrun: {arg!r}")


def _parse_execution_line(line: str, application: str, platform: str) -> ExecutionStep:
    try:
        tokens = shlex.split(line, posix=True)
    except ValueError as exc:
        raise CaseImportError(f"Unable to parse Allrun line {line!r}: {exc}") from exc
    if not tokens:
        raise CaseImportError(f"Unable to parse Allrun command: {line!r}")
    launcher = tokens.pop(0)
    parallel = launcher == "runParallel"
    if launcher in {"runApplication", "runParallel"}:
        if not tokens:
            raise CaseImportError(f"Allrun command lacks an application: {line!r}")
        command = tokens.pop(0)
    else:
        command = launcher
    if is_standard_application_variable(command):
        command = application
    _safe_tokens(command, tokens)
    allowed_utilities = _ALLOWED_UTILITIES_BY_PLATFORM.get(platform, _ALLOWED_UTILITIES)
    if command != application and command not in allowed_utilities:
        raise CaseImportError(f"Unsupported command in user Allrun: {command}")
    if parallel and command != application:
        raise CaseImportError(f"runParallel may only execute the controlDict application, not {command}.")
    return ExecutionStep(command, tuple(tokens), parallel)


def _parse_allrun(
    content: str,
    application: str,
    *,
    platform: str = FOUNDATION_V10,
) -> list[ExecutionStep]:
    steps: list[ExecutionStep] = []
    try:
        lines = normalise_shell_lines(content)
    except ValueError as exc:
        raise CaseImportError(str(exc)) from exc
    for raw_line in lines:
        line = strip_shell_comment(raw_line)
        if not line or line.startswith("#!"):
            continue
        if line.startswith("application="):
            if not re.fullmatch(r"application\s*=\s*\$?\(?getApplication\)?", line):
                raise CaseImportError(f"Unsupported application assignment in Allrun: {line!r}")
            continue
        if line.startswith((".", "source ", "cd ")):
            if not _is_supported_setup_line(line):
                raise CaseImportError(
                    "Unsupported shell setup in Allrun. Only the standard OpenFOAM "
                    "RunFunctions source and case-directory cd are allowed."
                )
            continue
        steps.append(_parse_execution_line(line, application, platform))
    if not steps:
        raise CaseImportError("Allrun contains no supported OpenFOAM execution commands.")
    return _ensure_mesh_check(steps, application)


def _synthesise_plan(application: str, mesh_state: str) -> list[ExecutionStep]:
    if mesh_state == "existing-polyMesh":
        steps = [ExecutionStep("checkMesh", origin="generated_missing_allrun")]
        if application != "checkMesh":
            steps.append(ExecutionStep(application, origin="generated_missing_allrun"))
        return steps
    if mesh_state == "blockMesh":
        steps = [
            ExecutionStep("blockMesh", origin="generated_missing_allrun"),
            ExecutionStep("checkMesh", origin="generated_missing_allrun"),
        ]
        if application not in {"blockMesh", "checkMesh"}:
            steps.append(ExecutionStep(application, origin="generated_missing_allrun"))
        return steps
    raise CaseImportError(
        "Allrun is missing and the case has neither constant/polyMesh nor "
        "system/blockMeshDict. A safe execution plan cannot be inferred."
    )


def _ensure_mesh_check(steps: list[ExecutionStep], application: str) -> list[ExecutionStep]:
    if not any(step.command == application for step in steps):
        raise CaseImportError("Allrun never runs the application declared in system/controlDict.")

    mesh_indexes = [
        index for index, step in enumerate(steps)
        if step.command in MESH_MUTATING_COMMANDS
    ]
    if mesh_indexes:
        last_mesh_index = max(mesh_indexes)
        steps = [
            step for index, step in enumerate(steps)
            if not (step.command == "checkMesh" and index <= last_mesh_index)
        ]
        mesh_indexes = [
            index for index, step in enumerate(steps)
            if step.command in MESH_MUTATING_COMMANDS
        ]

    check_indexes = [
        index for index, step in enumerate(steps) if step.command == "checkMesh"
    ]
    required_after = max(mesh_indexes) if mesh_indexes else -1
    if application in {"checkMesh", *MESH_MUTATING_COMMANDS}:
        if any(index > required_after for index in check_indexes):
            return steps
        repaired = list(steps)
        repaired.insert(required_after + 1, ExecutionStep("checkMesh", origin="foamagent_mesh_gate"))
        return repaired

    solver_index = next(index for index, step in enumerate(steps) if step.command == application)
    if any(index > solver_index for index in mesh_indexes):
        raise CaseImportError(
            "Allrun modifies the mesh after the configured solver starts; "
            "safe case-import mode requires mesh validation before the solver."
        )
    if any(required_after < index < solver_index for index in check_indexes):
        return steps

    repaired = list(steps)
    repaired.insert(
        required_after + 1 if mesh_indexes else solver_index,
        ExecutionStep("checkMesh", origin="foamagent_mesh_gate"),
    )
    return repaired


def render_controlled_allrun(steps: list[ExecutionStep], *, platform: str = FOUNDATION_V10) -> str:
    """Render the validated plan; the uploaded shell program is never executed."""
    lines = [
        "#!/bin/sh",
        'cd "${0%/*}/.." || exit 1',
        '. "$WM_PROJECT_DIR/bin/tools/RunFunctions"',
        *controlled_allrun_runtime_guard(platform),
        "foamagent_require_stock_command() {",
        '    foamagent_command_path=$(command -v "$1") || {',
        '        echo "Required OpenFOAM command is unavailable: $1" >&2',
        "        return 64",
        "    }",
        '    case "$foamagent_command_path" in',
        '        "$FOAM_APPBIN"/*) return 0 ;;',
        '        *) echo "Custom or non-stock OpenFOAM command is not allowed: $1 ($foamagent_command_path)" >&2; return 64 ;;',
        "    esac",
        "}",
        "foamagent_require_mesh_ok() {",
        '    foamagent_mesh_log="log.checkMesh"',
        '    if [ ! -f "$foamagent_mesh_log" ]; then',
        '        echo "checkMesh did not produce $foamagent_mesh_log" >&2',
        "        return 65",
        "    fi",
        '    if grep -Eiq "Failed[[:space:]]+[1-9][0-9]*[[:space:]]+mesh checks?" "$foamagent_mesh_log" || ! grep -Eiq "Mesh[[:space:]]+OK" "$foamagent_mesh_log"; then',
        '        echo "checkMesh did not establish a usable mesh; refusing to start a solver." >&2',
        "        return 65",
        "    fi",
        "}",
        "",
    ]
    for step in steps:
        invocation = " ".join((step.command, *step.args)).strip()
        lines.append(f"foamagent_require_stock_command {step.command} || exit $?")
        runner = "runParallel" if step.parallel else "runApplication"
        lines.append(f"{runner} {invocation}")
        lines.append("status=$?")
        lines.append("[ $status -eq 0 ] || exit $status")
        if step.command == "checkMesh":
            lines.append("foamagent_require_mesh_ok || exit $?")
    return "\n".join(lines) + "\n"


def _make_read_only(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode & ~0o222)


def _make_writable(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | stat.S_IWUSR)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def import_case(case_path: str | Path, output_dir: str | Path, *, case_subdir: Optional[str] = None, overwrite: bool = False, openfoam_target: str = "") -> CaseManifest:
    """Materialise a directory/ZIP as immutable ``original`` and writable ``work``."""
    target = normalise_openfoam_target(openfoam_target)
    source = Path(case_path).expanduser().resolve()
    if not source.exists() or not source.is_dir() and source.suffix.lower() != ".zip":
        raise CaseImportError("--case_path must reference an existing directory or .zip archive.")
    try:
        output_root = validate_output_path(output_dir, source_path=source)
    except OutputDirectorySafetyError as exc:
        raise CaseImportError(str(exc)) from exc
    with tempfile.TemporaryDirectory(prefix="foamagent-case-import-") as temporary:
        staging = Path(temporary) / "source"
        if source.is_dir():
            _validate_tree(source)
            shutil.copytree(source, staging)
        else:
            staging.mkdir()
            _extract_zip(source, staging)
            _validate_tree(staging)
        selected = _find_case_root(staging, case_subdir)
        platform, version = _detect_platform(selected)
        application = _application(selected)
        source_allrun = selected / "Allrun"
        allrun = _read_text(source_allrun) if source_allrun.is_file() else ""
        issues = _reject_dynamic_dependencies(selected, allrun)
        try:
            mesh_state = _mesh_state(selected)
            plan = (
                _parse_allrun(
                    allrun,
                    application,
                    platform=ESI_V2006 if target == ESI_V2006 else platform,
                )
                if source_allrun.is_file()
                else _synthesise_plan(application, mesh_state)
            )
            if not source_allrun.is_file() and (startup_issue := _synthesised_startup_issue(selected)):
                issues.append(startup_issue)
            plan = _ensure_mesh_check(plan, application)
        except CaseImportError as exc:
            plan, issues = [], [*issues, str(exc)]
        if target == ESI_V2006:
            if platform == "esi" and version and version.lstrip("vV") == "2006":
                platform = ESI_V2006
            else:
                issues.append("Configured target is ESI/OpenCFD v2006, but the case is not ESI v2006.")
        elif platform not in {FOUNDATION_V10, "foundation-v10-compatible"}:
            issues.append("Only Foundation OpenFOAM v10 cases are supported without the ESI v2006 target.")
        try:
            output_root = prepare_output_directory(output_root, overwrite=overwrite, source_path=source)
        except (FileExistsError, OutputDirectorySafetyError) as exc:
            raise CaseImportError(str(exc)) from exc
        original, work, report = output_root / "original", output_root / "work", output_root / "report"
        original_staging = output_root / ".original-staging"
        try:
            shutil.copytree(selected, original_staging)
            original_staging.rename(original)
        except OSError as exc:
            shutil.rmtree(original_staging, ignore_errors=True)
            raise CaseImportError(f"Unable to materialise imported case: {exc}") from exc
        manifest = CaseManifest(
            source=str(source), case_root=str(selected.relative_to(staging)), output_root=str(output_root),
            platform=platform, version=version, application=application, allrun_provided=bool(allrun),
            mesh_state=_mesh_state(original), execution_plan=plan,
            detected_libraries=_extract_libraries(original),
            blocking_issues=list(dict.fromkeys(issues)), original_hashes=_hash_files(original),
        )
        shutil.copytree(original, work)
        _make_read_only(original)
        _write_json(report / "case_manifest.json", manifest.to_dict())
        return manifest


def _clear_attempt_artifacts(case_dir: Path) -> None:
    for path in case_dir.iterdir():
        if path.is_file() and (path.name.startswith("log.") or path.name in {"Allrun.import.out", "Allrun.import.err"}):
            path.unlink()


def execute_imported_case(work_dir: str | Path, manifest: CaseManifest, *, timeout: int) -> list[Any]:
    """Execute the rendered plan after target-specific preflight checks."""
    case_dir = Path(work_dir)
    controlled = case_dir / ".foamagent" / "Allrun.controlled"
    controlled.parent.mkdir(exist_ok=True)
    script_content = render_controlled_allrun(
        manifest.execution_plan,
        platform=manifest.platform,
    )
    controlled.write_text(script_content, encoding="utf-8")

    preflight = validate_openfoam_case_preflight(
        str(case_dir),
        script_content,
        openfoam_target=manifest.platform,
    )
    if preflight:
        return preflight

    _clear_attempt_artifacts(case_dir)
    out_file = case_dir / "Allrun.import.out"
    err_file = case_dir / "Allrun.import.err"
    try:
        command_kwargs = {"openfoam_target": ESI_V2006} if manifest.platform == ESI_V2006 else {}
        result = run_command(
            str(controlled),
            str(out_file),
            str(err_file),
            str(case_dir),
            timeout,
            **command_kwargs,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return [{"file": "Allrun", "error_content": str(exc)}]
    errors: list[Any] = []
    if result.get("timed_out"):
        errors.append({"file": "Allrun", "error_content": f"Controlled execution exceeded {timeout} seconds."})
    elif result.get("returncode") not in {None, 0}:
        errors.append({"file": "Allrun", "error_content": f"Controlled execution exited with {result.get('returncode')}."})
    errors.extend(check_foam_errors(str(case_dir)))
    errors.extend(validate_openfoam_case_postflight(str(case_dir), script_content))

    deduplicated: list[Any] = []
    seen: set[str] = set()
    for error in errors:
        marker = json.dumps(error, sort_keys=True, default=str)
        if marker not in seen:
            seen.add(marker)
            deduplicated.append(error)
    return deduplicated


def numeric_signature(content: str) -> dict[str, Any]:
    return {"tokens": _NUMBER_RE.findall(content), "bindings": [(_NUMBER_RE.sub("<number>", line), tuple(_NUMBER_RE.findall(line))) for line in content.splitlines() if _NUMBER_RE.search(line)]}


def numeric_snapshot(case_dir: Path) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    for path in iter_regular_files(case_dir):
        if ".foamagent" in path.parts:
            continue
        snapshot[path.relative_to(case_dir).as_posix()] = numeric_signature(_read_text(path))
    return snapshot


def _foamfile_object_match(content: str) -> Optional[re.Match[str]]:
    """Find ``object`` inside the actual FoamFile header, not field data."""
    foam_file = re.search(r"\bFoamFile\b", content)
    if not foam_file:
        return None
    opening = content.find("{", foam_file.end())
    if opening < 0:
        return None
    depth = 0
    closing = -1
    for index in range(opening, len(content)):
        if content[index] == "{":
            depth += 1
        elif content[index] == "}":
            depth -= 1
            if depth == 0:
                closing = index
                break
    if closing < 0:
        return None
    header = content[opening + 1 : closing]
    comment_free = re.sub(
        r"/\*.*?\*/|//[^\n]*",
        lambda match: " " * len(match.group(0)),
        header,
        flags=re.DOTALL,
    )
    match = re.search(r"\bobject\s+([^\s;]+)(\s*;)", comment_free)
    if match is None:
        return None
    return re.compile(r"\bobject\s+([^\s;]+)(\s*;)").search(
        content,
        opening + 1 + match.start(),
        opening + 1 + match.end(),
    )


def _repair_object_headers(case_dir: Path) -> list[tuple[Path, str, str]]:
    repairs: list[tuple[Path, str, str]] = []
    for path in iter_regular_files(case_dir):
        if ".foamagent" in path.parts or path.name in {"Allrun", "Allclean"}:
            continue
        old = _read_text(path)
        match = _foamfile_object_match(old)
        if match and match.group(1) != path.name:
            repairs.append((path, old, old[:match.start(1)] + path.name + old[match.end(1):]))
    return repairs


def _repair_missing_semicolons(case_dir: Path) -> list[tuple[Path, str, str]]:
    keys = "|".join(sorted(_NON_NUMERIC_SEMICOLON_KEYS))
    statement = re.compile(
        rf"^(?P<entry>\s*(?:{keys})\s+[^;{{}}()\n]+?)(?P<comment>\s*//.*)?$",
        re.MULTILINE,
    )
    repairs = []
    for path in iter_regular_files(case_dir):
        if ".foamagent" in path.parts or path.name in {"Allrun", "Allclean"}:
            continue
        old = _read_text(path)
        new = statement.sub(
            lambda match: f"{match.group('entry').rstrip()};{match.group('comment') or ''}",
            old,
        )
        if new != old:
            repairs.append((path, old, new))
    return repairs


def apply_safe_repairs(work_dir: str | Path, errors: list[Any]) -> list[dict[str, Any]]:
    case_dir, error_text = Path(work_dir), "\n".join(map(str, errors)).lower()
    candidates = _repair_object_headers(case_dir) if "object" in error_text or "foamfile" in error_text else []
    if "expected ';'" in error_text or "expected ;" in error_text:
        candidates.extend(_repair_missing_semicolons(case_dir))
    before = numeric_snapshot(case_dir)
    records: list[dict[str, Any]] = []
    for path, old, new in candidates:
        relative = path.relative_to(case_dir).as_posix()
        if numeric_signature(old) != numeric_signature(new):
            records.append({"file": relative, "status": "rejected", "reason": "repair would alter numeric tokens or their line bindings"})
            continue
        path.write_text(new, encoding="utf-8")
        records.append({"file": relative, "status": "applied", "reason": "deterministic non-numeric syntax/header repair", "diff": "".join(unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=f"before/{relative}", tofile=f"after/{relative}"))})
    if before != numeric_snapshot(case_dir):
        raise RuntimeError("Safe repair violated the numeric-invariant check; work copy must be inspected.")
    return records


def _restore_work(original: Path, work: Path, overrides: dict[str, bytes]) -> None:
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(original, work)
    _make_writable(work)
    for relative, content in overrides.items():
        target = work / Path(relative)
        try:
            target.relative_to(work)
        except ValueError as exc:
            raise RuntimeError(f"Unsafe stored repair path: {relative}") from exc
        if not target.is_file():
            raise RuntimeError(f"Stored repair target disappeared: {relative}")
        target.write_bytes(content)


def _capture_overrides(original: Path, work: Path) -> dict[str, bytes]:
    overrides: dict[str, bytes] = {}
    for path in iter_regular_files(work):
        if ".foamagent" in path.parts:
            continue
        relative, baseline = path.relative_to(work), original / path.relative_to(work)
        if baseline.is_file() and path.read_bytes() != baseline.read_bytes():
            if numeric_signature(_read_text(path)) != numeric_signature(_read_text(baseline)):
                raise RuntimeError(f"Repair changed numeric inputs: {relative}")
            overrides[relative.as_posix()] = path.read_bytes()
    return overrides


def write_import_attempt_report(report_dir: str | Path, attempts: list[dict[str, Any]]) -> None:
    _write_json(Path(report_dir) / "attempts.json", attempts)


def prepare_imported_work_tree(work_dir: str | Path) -> None:
    _make_writable(Path(work_dir))


def _error_fingerprint(errors: list[Any]) -> str:
    return hashlib.sha256(
        json.dumps(errors, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _blocked_import_attempt(
    attempts: list[dict[str, Any]],
    report_dir: str | Path,
    reason: str,
    error_fingerprints: list[str],
) -> dict[str, Any]:
    if attempts:
        attempts[-1] = {**attempts[-1], "terminal_reason": reason}
    write_import_attempt_report(report_dir, attempts)
    return {
        "attempts": attempts,
        "status": "blocked",
        "termination_reason": reason,
        "error_fingerprints": error_fingerprints,
    }


def execute_imported_case_attempt(manifest: CaseManifest, *, original_dir: str | Path, work_dir: str | Path, report_dir: str | Path, attempts: list[dict[str, Any]], overrides: dict[str, bytes], timeout: int, executor: Any = None) -> dict[str, Any]:
    attempts = list(attempts)
    try:
        if attempts:
            _restore_work(Path(original_dir), Path(work_dir), overrides)
        errors = (executor or execute_imported_case)(work_dir, manifest, timeout=timeout)
    except Exception as exc:
        errors = [{"file": "Allrun", "error_content": str(exc)}]
    attempts.append({"attempt": len(attempts) + 1, "errors": errors, "repairs": []})
    write_import_attempt_report(report_dir, attempts)
    return {"attempts": attempts, "status": "success" if not errors else "running", "errors": errors}


def repair_imported_case_attempt(*, original_dir: str | Path, work_dir: str | Path, report_dir: str | Path, attempts: list[dict[str, Any]], errors: list[Any], error_fingerprints: list[str], loop_count: int, max_repairs: int) -> dict[str, Any]:
    attempts = list(attempts)
    known_fingerprints = list(error_fingerprints)
    if not errors:
        return {"attempts": attempts, "status": "success", "error_fingerprints": known_fingerprints}

    fingerprint = _error_fingerprint(errors)
    if fingerprint in known_fingerprints:
        return _blocked_import_attempt(
            attempts, report_dir, "repeated_error_fingerprint", known_fingerprints
        )
    known_fingerprints.append(fingerprint)
    if loop_count >= max_repairs:
        return _blocked_import_attempt(
            attempts, report_dir, "maximum_safe_repair_attempts_reached", known_fingerprints
        )

    repairs = apply_safe_repairs(work_dir, errors)
    if attempts:
        attempts[-1] = {**attempts[-1], "repairs": repairs}
    write_import_attempt_report(report_dir, attempts)
    if not any(item["status"] == "applied" for item in repairs):
        result = _blocked_import_attempt(
            attempts,
            report_dir,
            "no_safe_non_numeric_repair_available",
            known_fingerprints,
        )
        result["repairs"] = repairs
        return result
    return {
        "attempts": attempts,
        "overrides": _capture_overrides(Path(original_dir), Path(work_dir)),
        "error_fingerprints": known_fingerprints,
        "loop_count": loop_count + 1,
        "repairs": repairs,
        "status": "ready",
    }


__all__ = [
    "CaseImportError", "CaseManifest", "ExecutionStep", "apply_safe_repairs",
    "execute_imported_case", "execute_imported_case_attempt", "import_case",
    "iter_regular_files", "numeric_signature", "prepare_imported_work_tree",
    "repair_imported_case_attempt", "render_controlled_allrun", "write_import_attempt_report",
]
