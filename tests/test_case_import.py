"""Focused regression coverage for Existing Case import mode."""

from __future__ import annotations

import sys
from pathlib import Path
import zipfile

import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from config import Config  # noqa: E402
import main  # noqa: E402
import nodes.imported_case_node as imported_case_node  # noqa: E402
from models import CaseImportError, ExecutionStep  # noqa: E402
from openfoam_target import (  # noqa: E402
    ESI_V2006,
    database_path_for_config,
    generation_convention,
    uses_legacy_esi_translation,
)
from services.case_import import (  # noqa: E402
    _clear_attempt_artifacts,
    apply_safe_repairs,
    import_case,
    render_controlled_allrun,
    numeric_signature,
)
from services.plan import _crop_file_content  # noqa: E402
from services.run_local import _cleanup_run_artifacts  # noqa: E402
from utils import remove_file  # noqa: E402
from services.output_safety import (  # noqa: E402
    OutputDirectorySafetyError,
    prepare_output_directory,
)
from translation.esi_translator import convert_case_to_esi_if_needed  # noqa: E402


def test_existing_esi_case_uses_only_controlled_import_path(tmp_path, monkeypatch) -> None:
    """The public entry bypasses planning/LLM work and creates its audit tree."""
    source = tmp_path / "source"
    (source / "system").mkdir(parents=True)
    (source / "constant").mkdir()
    header = "Website: https://www.openfoam.com\nVersion: v2006\n"
    (source / "system" / "controlDict").write_text(
        header + "application icoFoam;\nstartTime 0;\nendTime 1;\n",
        encoding="utf-8",
    )
    (source / "system" / "blockMeshDict").write_text("vertices ();\n", encoding="utf-8")
    (source / "Allrun").write_text(
        'cd "${0%/*}" || exit\n'
        '. "${WM_PROJECT_DIR:?}/bin/tools/RunFunctions"\n'
        "runApplication blockMesh\n"
        "runApplication icoFoam\n",
        encoding="utf-8",
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("prompt-only node/LLM path must not run for Existing Case")

    monkeypatch.setattr(main, "planner_node", unexpected)
    monkeypatch.setattr(main, "meshing_node", unexpected)
    monkeypatch.setattr(main, "input_writer_node", unexpected)
    monkeypatch.setattr(main, "LLMService", unexpected)
    monkeypatch.setattr(imported_case_node, "execute_imported_case", lambda *_args, **_kwargs: [])

    config = Config()
    config.openfoam_target = "esi-v2006"
    config.case_dir = str(tmp_path / "output")
    result = main.main_imported_case(str(source), config)

    assert result["status"] == "success"
    assert result["manifest"]["platform"] == "esi-v2006"
    assert [step["command"] for step in result["manifest"]["execution_plan"]] == [
        "blockMesh",
        "checkMesh",
        "icoFoam",
    ]
    assert (tmp_path / "output" / "original" / "system" / "controlDict").is_file()
    assert (tmp_path / "output" / "work" / "system" / "controlDict").is_file()
    assert (tmp_path / "output" / "report" / "case_manifest.json").is_file()
    assert (tmp_path / "output" / "report" / "attempts.json").is_file()


def test_existing_case_rejects_reserved_metadata_before_overwriting_output(tmp_path) -> None:
    source = tmp_path / "source"
    (source / "system").mkdir(parents=True)
    (source / "constant").mkdir()
    (source / ".foamagent").mkdir()
    (source / "system" / "controlDict").write_text(
        "application icoFoam;\nstartTime 0;\nendTime 1;\n",
        encoding="utf-8",
    )

    output = tmp_path / "output"
    output.mkdir()
    sentinel = output / "prior-result.txt"
    sentinel.write_text("keep me", encoding="utf-8")

    with pytest.raises(CaseImportError, match="reserved .foamagent"):
        import_case(source, output, overwrite=True)

    assert sentinel.read_text(encoding="utf-8") == "keep me"


def test_native_esi_contract_combines_corpus_isolation_and_runtime_guard(tmp_path, monkeypatch) -> None:
    config = Config(
        database_path=tmp_path / "database",
        openfoam_fork="esi",
        openfoam_target=ESI_V2006,
    )

    assert database_path_for_config(config) == tmp_path / "database" / ESI_V2006
    assert generation_convention(config) == ESI_V2006
    assert not uses_legacy_esi_translation(config)

    class TranslatorMustNotRun:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("native esi-v2006 must bypass the legacy translator")

    monkeypatch.setattr("translation.esi_translator.ESITranslator", TranslatorMustNotRun)
    convert_case_to_esi_if_needed(tmp_path / "case", config)

    rendered = render_controlled_allrun(
        [ExecutionStep("checkMesh"), ExecutionStep("icoFoam")],
        platform=ESI_V2006,
    )
    assert "v2006|2006" in rendered
    assert "WM_PROJECT_VERSION=10" not in rendered


def test_output_marker_symlink_is_rejected_without_writing_through_it(tmp_path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    external = tmp_path / "external-marker"
    external.write_text("sentinel", encoding="utf-8")
    (output / ".foamagent-output").symlink_to(external)

    with pytest.raises(OutputDirectorySafetyError, match="ownership marker"):
        prepare_output_directory(output, overwrite=False)

    assert external.read_text(encoding="utf-8") == "sentinel"


def test_zip_traversal_is_rejected_before_existing_output_is_changed(tmp_path) -> None:
    archive = tmp_path / "case.zip"
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("../outside.txt", "must not be extracted")

    output = tmp_path / "output"
    output.mkdir()
    sentinel = output / "prior-result.txt"
    sentinel.write_text("keep me", encoding="utf-8")

    with pytest.raises(CaseImportError, match="escapes the import directory"):
        import_case(archive, output, overwrite=True)

    assert sentinel.read_text(encoding="utf-8") == "keep me"
    assert not (tmp_path / "outside.txt").exists()


def test_safe_import_repair_preserves_numeric_tokens(tmp_path) -> None:
    work = tmp_path / "work"
    (work / "system").mkdir(parents=True)
    control_dict = work / "system" / "controlDict"
    control_dict.write_text(
        "FoamFile { object wrongName; }\napplication icoFoam;\nendTime 2;\n",
        encoding="utf-8",
    )
    before = numeric_signature(control_dict.read_text(encoding="utf-8"))

    repairs = apply_safe_repairs(work, [{"error_content": "FoamFile object mismatch"}])

    assert any(repair["status"] == "applied" for repair in repairs)
    assert "object controlDict;" in control_dict.read_text(encoding="utf-8")
    assert numeric_signature(control_dict.read_text(encoding="utf-8")) == before


def test_cleanup_unlinks_output_symlinks_without_touching_targets(tmp_path) -> None:
    case = tmp_path / "case"
    case.mkdir()
    external = tmp_path / "external.log"
    external.write_text("keep", encoding="utf-8")
    (case / "Allrun.out").symlink_to(external)
    (case / "Allrun.err").symlink_to(external)
    (case / "12").symlink_to(tmp_path / "external-time", target_is_directory=True)
    (case / "Allrun.import.out").symlink_to(tmp_path / "missing-output")
    dangling_hpc_output = case / "HPC.out"
    dangling_hpc_output.symlink_to(tmp_path / "missing-hpc-output")

    _cleanup_run_artifacts(str(case))
    _clear_attempt_artifacts(case)
    remove_file(str(dangling_hpc_output))

    assert not (case / "Allrun.out").is_symlink()
    assert not (case / "Allrun.err").is_symlink()
    assert not (case / "12").is_symlink()
    assert not (case / "Allrun.import.out").is_symlink()
    assert not dangling_hpc_output.is_symlink()
    assert external.read_text(encoding="utf-8") == "keep"


def test_reference_crop_keeps_content_within_allocated_budget() -> None:
    content = "important reference content\n" * 20
    result = _crop_file_content(content, 80)

    assert len(result) <= 80
    assert result.startswith("important reference")
    assert "reference content omitted" in result
