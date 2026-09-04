from dataclasses import asdict, dataclass, field
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

from openfoam_target import ESI_V2006


class CreateCaseIn(BaseModel):
    user_prompt: str
    output_dir: Optional[str] = None


class CreateCaseOut(BaseModel):
    case_id: str
    case_dir: str


# NOTE: These models are deprecated and replaced by FastMCP server models in src/mcp/fastmcp_server.py
# Use PlanRequest/PlanResponse and GenerateFilesRequest/GenerateFilesResponse instead
class PlanIn(BaseModel):
    """Deprecated: Use PlanRequest in fastmcp_server.py instead."""
    case_id: str  # Deprecated: case_id is no longer required, use user_requirement only


class Subtask(BaseModel):
    file: str
    folder: str


class PlanOut(BaseModel):
    """Deprecated: Use PlanResponse in fastmcp_server.py instead."""
    plan: List[Subtask]
    case_info: Dict  # Deprecated: case_info is now expanded to case_name, case_solver, case_domain, case_category


class GenerateFileIn(BaseModel):
    """Deprecated: Use GenerateFilesRequest in fastmcp_server.py instead."""
    case_id: str  # Deprecated: use case_name instead
    file: str
    folder: str
    write: bool = True
    overwrite: bool = True


class GenerateFileOut(BaseModel):
    content: str
    written_path: Optional[str] = None


class MeshIn(BaseModel):
    case_id: str
    mesh_config: Dict


class MeshOut(BaseModel):
    job_id: Optional[str] = None
    status: str


class HPCScriptIn(BaseModel):
    case_id: str
    hpc_config: Dict


class HPCScriptOut(BaseModel):
    script_content: str
    script_path: str


class RunIn(BaseModel):
    case_id: str
    environment: str  # "local" | "hpc"
    extra: Optional[Dict] = None


class RunOut(BaseModel):
    job_id: Optional[str]
    status: str  # "submitted" | "completed" | "failed"


class JobStatusIn(BaseModel):
    job_id: str


class JobStatusOut(BaseModel):
    status: str
    details: Optional[Dict] = None


class LogsIn(BaseModel):
    case_id: str
    job_id: Optional[str] = None


class LogsOut(BaseModel):
    logs: Dict[str, str]


class ApplyFixIn(BaseModel):
    case_id: str
    foamfiles: Optional[Any] = None  # FoamPydantic object
    error_logs: List[str] = []
    review_analysis: str = ""
    user_requirement: str = ""
    dir_structure: Optional[Dict] = None


class ApplyFixOut(BaseModel):
    status: str
    written: List[str]
    updated_dir_structure: Optional[Dict] = None
    updated_foamfiles: Optional[Any] = None  # FoamPydantic object
    cleared_error_logs: List[str] = []


class VisualizationIn(BaseModel):
    case_id: str
    quantity: str
    extra: Optional[Dict] = None


class VisualizationOut(BaseModel):
    job_id: Optional[str]
    artifacts: List[str]


class CaseImportError(ValueError):
    """Raised when a case cannot safely enter controlled import mode."""


@dataclass(frozen=True)
class ExecutionStep:
    """One validated OpenFOAM command in a controlled execution plan."""

    command: str
    args: tuple[str, ...] = ()
    parallel: bool = False
    origin: str = "user_allrun"

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "args": list(self.args),
            "parallel": self.parallel,
            "origin": self.origin,
        }


@dataclass
class CaseManifest:
    """Selected existing case and its data-only execution plan."""

    source: str
    case_root: str
    output_root: str
    platform: str
    version: Optional[str]
    application: str
    allrun_provided: bool
    mesh_state: str
    execution_plan: list[ExecutionStep] = field(default_factory=list)
    detected_libraries: list[str] = field(default_factory=list)
    blocking_issues: list[str] = field(default_factory=list)
    original_hashes: dict[str, str] = field(default_factory=dict)

    @property
    def supported(self) -> bool:
        return not self.blocking_issues and self.platform in {
            "foundation-v10",
            "foundation-v10-compatible",
            ESI_V2006,
        }

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["execution_plan"] = [step.to_dict() for step in self.execution_plan]
        result["supported"] = self.supported
        return result


