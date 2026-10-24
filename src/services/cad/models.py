"""CAD contracts migrated from CFD-Agent-Integrated (MIT)."""
from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DetailView = Literal['Selected', 'OwnerContext', 'SelectedProxy']

class OpeningSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection_kind: Literal["face", "loop", "edges"]
    object_ids: list[str] = Field(min_length=1)
    role: str
    name: str
    description: str
    reason: str

    @model_validator(mode="after")
    def selection_is_explicit(self):
        if self.selection_kind in {"face", "loop"} and len(self.object_ids) != 1:
            raise ValueError("face and loop opening selections require exactly one object ID")
        return self

class CadSelectionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["selected", "ambiguous", "not_found"]
    reference_view: Literal["Front", "Back", "Top", "Bottom", "Right", "Left", "Isometric"]
    openings: list[OpeningSelection] = Field(default_factory=list)
    seed_inner_wall_id: str | None = None
    fluid_body_id: str | None = None
    fluid_body_ids: list[str] = Field(default_factory=list)
    extraction_strategy: Literal["faces", "edges"] | None = None
    explanation: str
    missing_information: list[str] = Field(default_factory=list)
    fluid_domain_action: Literal["extract", "reuse", "ambiguous"] = "extract"
    fluid_domain_evidence: str = ""

    @model_validator(mode="after")
    def selected_has_objects(self):
        if self.status != "selected":
            return self
        if self.fluid_domain_action == "extract":
            if not self.openings or not self.seed_inner_wall_id or not self.extraction_strategy:
                raise ValueError(
                    "extract requires openings, seed_inner_wall_id, and extraction_strategy"
                )
            if self.fluid_body_id is not None or self.fluid_body_ids:
                raise ValueError("extract must not include fluid_body_id or fluid_body_ids")
            expected_kinds = {"face"} if self.extraction_strategy == "faces" else {"loop", "edges"}
            if any(opening.selection_kind not in expected_kinds for opening in self.openings):
                raise ValueError("opening selection_kind must match extraction_strategy")
        elif self.fluid_domain_action == "reuse":
            if bool(self.fluid_body_id) == bool(self.fluid_body_ids):
                raise ValueError("reuse requires fluid_body_id or fluid_body_ids, exclusively")
            if len(self.fluid_body_ids) != len(set(self.fluid_body_ids)):
                raise ValueError("reuse body IDs must be unique")
            if self.openings or self.seed_inner_wall_id or self.extraction_strategy:
                raise ValueError("reuse must select only fluid_body_id or fluid_body_ids")
        else:
            raise ValueError("selected requires extract or reuse fluid_domain_action")
        names = [item.name for item in self.openings]
        if len(names) != len(set(names)):
            raise ValueError("boundary names must be unique")
        return self

class CandidateDetailRequest(BaseModel):
    """One candidate whose visual evidence is needed before final selection."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    purpose: Literal["opening", "seed", "body", "boundary"]
    views: list[DetailView] = Field(default_factory=list)
    reason: str

    @field_validator("views")
    @classmethod
    def views_are_unique(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("detail views must be unique")
        return value

class CadSelectionScreening(BaseModel):
    """First LLM pass: request visual evidence without making a final selection."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["needs_details", "ambiguous", "not_found"]
    reference_view: Literal["Front", "Back", "Top", "Bottom", "Right", "Left", "Isometric"]
    candidates: list[CandidateDetailRequest] = Field(default_factory=list)
    explanation: str
    missing_information: list[str] = Field(default_factory=list)
    fluid_domain_action: Literal["extract", "reuse", "ambiguous"] = "extract"
    fluid_domain_evidence: str = ""

    @model_validator(mode="after")
    def needs_details_has_candidates(self):
        if self.status == "needs_details" and not self.candidates:
            raise ValueError("needs_details requires at least one candidate")
        return self

class CadSelectionReview(BaseModel):
    """LLM decision after examining only the requested candidate detail images."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["selected", "needs_details", "ambiguous", "not_found"]
    selection: CadSelectionPlan | None = None
    detail_requests: list[CandidateDetailRequest] = Field(default_factory=list)
    explanation: str
    missing_information: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def review_has_consistent_payload(self):
        if self.status == "selected":
            if self.selection is None or self.selection.status != "selected":
                raise ValueError("selected review requires a selected final plan")
        elif self.status == "needs_details" and not self.detail_requests:
            raise ValueError("needs_details review requires detail_requests")
        elif self.selection is not None:
            raise ValueError("only selected review may include selection")
        return self

class BoundaryFaceGroup(BaseModel):
    """A real face set on the selected fluid body, ready for SpaceClaim grouping."""

    model_config = ConfigDict(extra="forbid")

    name: str
    role: str
    face_ids: list[str]
    reason: str

class BoundaryGroupPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["selected", "ambiguous", "not_found"]
    target_catalog_id: str
    groups: list[BoundaryFaceGroup] = Field(default_factory=list)
    explanation: str
    missing_information: list[str] = Field(default_factory=list)

class BoundaryGroupReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["selected", "needs_details", "ambiguous", "not_found"]
    selection: BoundaryGroupPlan | None = None
    detail_requests: list[CandidateDetailRequest] = Field(default_factory=list)
    explanation: str
    missing_information: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def review_has_consistent_payload(self):
        if self.status == "selected":
            if self.selection is None or self.selection.status != "selected":
                raise ValueError("selected review requires a selected boundary-group plan")
        elif self.status == "needs_details" and not self.detail_requests:
            raise ValueError("needs_details review requires detail_requests")
        elif self.selection is not None:
            raise ValueError("only selected review may include selection")
        return self

class FluidBodySelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["selected", "needs_details", "ambiguous", "not_found"]
    body_ids: list[str] = Field(default_factory=list)
    merge_groups: list[list[str]] = Field(default_factory=list)
    detail_requests: list[CandidateDetailRequest] = Field(default_factory=list)
    explanation: str
    missing_information: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def selected_has_body(self):
        if self.status == "selected" and not self.body_ids:
            raise ValueError("selected requires body_ids")
        if self.status == "needs_details" and not self.detail_requests:
            raise ValueError("needs_details requires detail_requests")
        if self.status != "selected" and self.body_ids:
            raise ValueError("only selected may include body_ids")
        return self

class BBox(BaseModel):
    min_m: list[float] = Field(min_length=3, max_length=3)
    max_m: list[float] = Field(min_length=3, max_length=3)
    center_m: list[float] = Field(min_length=3, max_length=3)

class CatalogObject(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    kind: Literal["body", "face", "edge", "loop"]
    moniker: str | None = Field(default=None, json_schema_extra={"model_visible": False})
    body_id: str | None = None
    face_id: str | None = None
    surface_type: str | None = None
    curve_type: str | None = None
    solid_or_sheet: str | None = None
    volume_m3: float | None = None
    area_m2: float | None = None
    perimeter_m: float | None = None
    length_m: float | None = None
    radius_m: float | None = None
    bbox: BBox | None = None
    centroid_m: list[float] | None = None
    normal: list[float] | None = None
    axis: list[float] | None = None
    axis_origin_m: list[float] | None = None
    start_m: list[float] | None = None
    end_m: list[float] | None = None
    half_angle_rad: float | None = None
    face_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    loop_ids: list[str] = Field(default_factory=list)
    adjacent_face_ids: list[str] = Field(default_factory=list)
    is_outer: bool | None = None
    closed: bool | None = None

    def model_facing_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, definition in type(self).model_fields.items():
            if (definition.json_schema_extra or {}).get("model_visible") is False:
                continue
            value = getattr(self, name)
            if value is None or value == []:
                continue
            result[name] = value.model_dump() if isinstance(value, BaseModel) else value
        return result

class GeometryCatalog(BaseModel):
    model_config = ConfigDict(extra="allow")

    catalog_id: str
    geometry_id: str
    coordinate_unit: Literal["m"] = "m"
    bodies: list[CatalogObject] = Field(default_factory=list)
    faces: list[CatalogObject] = Field(default_factory=list)
    edges: list[CatalogObject] = Field(default_factory=list)
    loops: list[CatalogObject] = Field(default_factory=list)
    images: list[dict[str, Any]] = Field(default_factory=list)
    candidate_render_results: list[dict[str, Any]] = Field(default_factory=list)
    candidate_render_summary: dict[str, int] = Field(default_factory=dict)

    def all_objects(self) -> list[CatalogObject]:
        return [*self.bodies, *self.faces, *self.edges, *self.loops]

    def by_id(self) -> dict[str, CatalogObject]:
        return {item.id: item for item in self.all_objects()}

    def public_dict(self) -> dict[str, Any]:
        return {
            "coordinate_unit": self.coordinate_unit,
            "bodies": [item.model_facing_dict() for item in self.bodies],
            "faces": [item.model_facing_dict() for item in self.faces],
            "edges": [item.model_facing_dict() for item in self.edges],
            "loops": [item.model_facing_dict() for item in self.loops],
        }

class SelectionExecution(BaseModel):
    ok: bool
    selected_ids: list[str] = Field(default_factory=list)
    selected_monikers: list[str] = Field(default_factory=list)
    active_selection_verified: bool = False
    images: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    record_path: str | None = None
