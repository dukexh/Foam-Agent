"""Contracts for the opt-in native ESI/OpenCFD v2006 target."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from config import Config  # noqa: E402
from openfoam_target import (  # noqa: E402
    ESI_V2006,
    database_path_for_config,
    generation_convention,
    native_openfoam_target,
    require_target_corpus,
)
from services.case_import import import_case  # noqa: E402
from services.case_import_allrun import render_controlled_allrun  # noqa: E402
from services.case_import_models import ExecutionStep  # noqa: E402
from services.input_writer import _generation_contract  # noqa: E402
from services.run_hpc import _add_esi_v2006_runtime_guard  # noqa: E402
from services.run_local import validate_openfoam_case_preflight  # noqa: E402
from translation.esi_translator import convert_case_to_esi_if_needed  # noqa: E402
from utils import run_openfoam_utility  # noqa: E402


def _write_esi_v2006_case(root: Path) -> Path:
    (root / "system").mkdir(parents=True)
    (root / "constant").mkdir()
    header = """/*--------------------------------*- C++ -*----------------------------------*\\
| OpenFOAM: The Open Source CFD Toolbox
| Website:  www.openfoam.com
| Version:  v2006
\\*---------------------------------------------------------------------------*/
"""
    (root / "system" / "controlDict").write_text(
        header
        + "FoamFile { object controlDict; }\napplication icoFoam;\nstartTime 0;\nendTime 1;\n",
        encoding="utf-8",
    )
    (root / "system" / "blockMeshDict").write_text(
        header + "FoamFile { object blockMeshDict; }\n",
        encoding="utf-8",
    )
    return root


def test_esi_v2006_uses_an_isolated_corpus_and_native_generation_contract(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("FOAMAGENT_OPENFOAM_TARGET", raising=False)
    config = Config(
        database_path=tmp_path / "database",
        openfoam_fork="esi",
        openfoam_target=ESI_V2006,
    )

    assert database_path_for_config(config) == tmp_path / "database" / ESI_V2006
    assert generation_convention(config) == ESI_V2006
    contract = _generation_contract(generation_convention(config))
    assert "native ESI v2006 case directly" in contract
    assert "post-generation translator" in contract


def test_native_target_defaults_to_foundation_but_explicit_target_takes_priority() -> None:
    assert native_openfoam_target(SimpleNamespace(openfoam_fork="foundation")) == "foundation-v10"
    assert native_openfoam_target(
        SimpleNamespace(openfoam_fork="esi", openfoam_target=ESI_V2006)
    ) == ESI_V2006


def test_native_v2006_requires_a_target_manifest(tmp_path: Path) -> None:
    config = SimpleNamespace(
        openfoam_target=ESI_V2006,
        database_path=tmp_path / "database",
        esi_v2006_database_path="",
    )

    with pytest.raises(FileNotFoundError, match="target-scoped corpus manifest"):
        require_target_corpus(config)

    manifest_dir = tmp_path / "database" / ESI_V2006 / "raw"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "foamagent_target.json").write_text(
        '{"openfoam_target": "esi-v2006", "wm_project_version": "v2006"}',
        encoding="utf-8",
    )
    with pytest.raises(FileNotFoundError, match="corpus is incomplete"):
        require_target_corpus(config)

    for filename in (
        "openfoam_case_stats.json",
        "openfoam_command_help.txt",
        "openfoam_allrun_scripts.txt",
        "openfoam_tutorials_structure.txt",
        "openfoam_tutorials_details.txt",
    ):
        (manifest_dir / filename).write_text("fixture", encoding="utf-8")
    for index in (
        "openfoam_command_help",
        "openfoam_allrun_scripts",
        "openfoam_tutorials_structure",
        "openfoam_tutorials_details",
    ):
        index_dir = (
            tmp_path
            / "database"
            / ESI_V2006
            / "faiss"
            / "Qwen_Qwen3-Embedding-0.6B"
            / index
        )
        index_dir.mkdir(parents=True)
        (index_dir / "index.faiss").write_text("fixture", encoding="utf-8")
        (index_dir / "index.pkl").write_text("fixture", encoding="utf-8")
    require_target_corpus(config)


def test_legacy_esi_translation_is_not_called_for_native_v2006(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("FOAMAGENT_OPENFOAM_TARGET", raising=False)
    config = Config(openfoam_fork="esi", openfoam_target=ESI_V2006)
    case = tmp_path / "case"
    case.mkdir()

    class TranslatorMustNotRun:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("native esi-v2006 must bypass the legacy translator")

    monkeypatch.setattr("translation.esi_translator.ESITranslator", TranslatorMustNotRun)
    convert_case_to_esi_if_needed(case, config)


def test_esi_v2006_import_is_opt_in_and_uses_its_runtime_guard(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("FOAMAGENT_OPENFOAM_TARGET", raising=False)
    source = _write_esi_v2006_case(tmp_path / "source")

    manifest = import_case(
        source,
        tmp_path / "output",
        openfoam_target=ESI_V2006,
    )

    assert manifest.supported
    assert manifest.platform == ESI_V2006
    rendered = render_controlled_allrun(
        [ExecutionStep("checkMesh"), ExecutionStep("icoFoam")],
        platform=manifest.platform,
    )
    assert "v2006|2006" in rendered
    assert "WM_PROJECT_VERSION=10" not in rendered


def test_esi_v2006_preflight_does_not_apply_foundation_momentum_transport_rule(
    tmp_path: Path,
) -> None:
    case = tmp_path / "case"
    (case / "constant").mkdir(parents=True)
    (case / "system").mkdir()
    (case / "system" / "controlDict").write_text("application icoFoam;\n", encoding="utf-8")
    (case / "constant" / "momentumTransport").write_text("laminar\n", encoding="utf-8")
    script = "runApplication blockMesh\nrunApplication checkMesh\nrunApplication icoFoam\n"

    legacy_errors = validate_openfoam_case_preflight(str(case), script)
    v2006_errors = validate_openfoam_case_preflight(
        str(case),
        script,
        openfoam_target=ESI_V2006,
    )

    assert any("missing the required Foundation" in error["error_content"] for error in legacy_errors)
    assert not any("missing the required Foundation" in error["error_content"] for error in v2006_errors)


def test_esi_v2006_preflight_requires_its_native_turbulence_dictionary_shape(
    tmp_path: Path,
) -> None:
    case = tmp_path / "case"
    (case / "constant").mkdir(parents=True)
    (case / "system").mkdir()
    (case / "system" / "controlDict").write_text("application icoFoam;\n", encoding="utf-8")
    turbulence = case / "constant" / "turbulenceProperties"
    turbulence.write_text("FoamFile {}\n", encoding="utf-8")
    script = "runApplication blockMesh\nrunApplication checkMesh\nrunApplication icoFoam\n"

    errors = validate_openfoam_case_preflight(
        str(case), script, openfoam_target=ESI_V2006
    )

    assert any("simulationType" in error["error_content"] for error in errors)
    turbulence.write_text("simulationType laminar;\n", encoding="utf-8")
    assert not validate_openfoam_case_preflight(
        str(case), script, openfoam_target=ESI_V2006
    )


def test_native_mesh_utility_sources_and_validates_the_v2006_bashrc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import utils

    openfoam_root = tmp_path / "OpenFOAM-v2006"
    (openfoam_root / "etc").mkdir(parents=True)
    (openfoam_root / "etc" / "bashrc").write_text("# test", encoding="utf-8")
    monkeypatch.setenv("WM_PROJECT_DIR", str(openfoam_root))
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(utils.subprocess, "run", fake_run)
    run_openfoam_utility(
        ["gmshToFoam", "geometry.msh"],
        working_dir=str(tmp_path),
        timeout=12,
        openfoam_target=ESI_V2006,
    )

    command = captured["command"]
    assert command[:3] == ["bash", "-c", command[2]]
    assert command[3:] == [
        "foamagent-utility",
        str(openfoam_root / "etc" / "bashrc"),
        ESI_V2006,
        "gmshToFoam",
        "geometry.msh",
    ]
    assert 'source "$1"' in command[2]
    assert "esi-v2006:v2006|esi-v2006:2006" in command[2]


def test_legacy_mesh_utility_keeps_the_direct_subprocess_shape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import utils

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        utils.subprocess,
        "run",
        lambda command, **kwargs: captured.update(command=command, kwargs=kwargs)
        or SimpleNamespace(stdout="", stderr="", returncode=0),
    )
    run_openfoam_utility(["checkMesh"], working_dir=str(tmp_path), timeout=12)

    assert captured["command"] == ["checkMesh"]


def test_native_hpc_script_sources_a_configured_v2006_runtime() -> None:
    with pytest.raises(ValueError, match="FOAMAGENT_HPC_OPENFOAM_BASHRC"):
        _add_esi_v2006_runtime_guard("#!/bin/bash\necho run\n", ESI_V2006)

    script = _add_esi_v2006_runtime_guard(
        "#!/bin/bash\necho run\n",
        ESI_V2006,
        openfoam_bashrc="/opt/OpenFOAM/OpenFOAM-v2006/etc/bashrc",
    )

    assert "source /opt/OpenFOAM/OpenFOAM-v2006/etc/bashrc" in script
    assert "v2006|2006" in script
