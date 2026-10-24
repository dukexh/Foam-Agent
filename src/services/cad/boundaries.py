"""Read and validate actual saved SpaceClaim boundary groups."""
from __future__ import annotations
from typing import Any
from .models import GeometryCatalog
from .errors import PipelineError
def named_groups(catalog: GeometryCatalog) -> dict[str, list[str]]:
    native = getattr(catalog, "native_catalog", None)
    if not isinstance(native, dict):
        raise ValueError("Confirmed catalog has no native group data")
    raw_groups = native.get("internal", {}).get("raw_groups", [])
    result: dict[str, list[str]] = {}
    for row in raw_groups:
        name = str(row.get("raw_name", "")).strip()
        members = [str(item) for item in row.get("member_ids", [])]
        if not name:
            continue
        if name in result:
            raise ValueError("Duplicate SpaceClaim group name: " + name)
        result[name] = members
    return result

def confirm_roles(
    *, catalog: GeometryCatalog, proposed: dict[str, str], previous: dict[str, str]
) -> dict[str, str]:
    groups = named_groups(catalog)
    extra = sorted(set(proposed) - set(groups))
    roles = {name: proposed.get(name, previous.get(name, "")) for name in groups}
    missing = sorted(name for name, role in roles.items() if not role)
    if missing:
        raise ValueError("Boundary roles are missing for confirmed groups: " + ", ".join(missing))
    if extra:
        raise ValueError("Boundary roles refer to absent confirmed groups: " + ", ".join(extra))
    return roles

def validate_confirmed_cad(*, catalog: GeometryCatalog, roles: dict[str, str]) -> dict[str, Any]:
    """Validate the actual saved CAD before Fluent receives it.

    SpaceClaim groups are editable during human confirmation.  Verify the
    topological and grouping invariants again from a fresh catalog rather than
    trusting the groups produced before the handoff pause.
    """

    bodies = list(catalog.bodies)
    positive = [
        body for body in bodies if body.solid_or_sheet == "solid" and (body.volume_m3 or 0.0) > 0.0
    ]
    if not positive or len(positive) != len(bodies):
        raise PipelineError(
            "CAD_CONFIRMED_SOLID_INVALID",
            "The confirmed CAD must contain positive-volume fluid solids only.",
            stage="reload_confirmed_cad",
            substep="solid validation",
            objects=[{"candidate_id": body.id} for body in bodies],
            suggested_action="Repair non-solid or zero-volume bodies, then save the CAD again.",
            evidence={
                "body_count": len(bodies),
                "positive_solid_ids": [body.id for body in positive],
            },
        )
    body_ids = {body.id for body in positive}
    groups = named_groups(catalog)
    if set(groups) != set(roles):
        raise PipelineError(
            "CAD_CONFIRMED_GROUP_ROLE_MISMATCH",
            "The confirmed boundary groups do not match the confirmed role names.",
            stage="reload_confirmed_cad",
            substep="boundary-group validation",
            suggested_action="Assign roles again for every current boundary group.",
            evidence={"group_names": sorted(groups), "role_names": sorted(roles)},
        )
    face_ids = {face.id for face in catalog.faces if face.body_id in body_ids}
    assigned: dict[str, str] = {}
    for name, members in groups.items():
        if not members:
            raise PipelineError(
                "CAD_CONFIRMED_GROUP_EMPTY",
                "A confirmed boundary group is empty.",
                stage="reload_confirmed_cad",
                substep="boundary-group validation",
                objects=[{"name": name, "role": roles.get(name, "")}],
                suggested_action="Add fluid-body faces to that group, or remove the empty group and confirm roles again.",
            )
        non_faces = [member for member in members if member not in face_ids]
        if non_faces:
            raise PipelineError(
                "CAD_CONFIRMED_GROUP_MEMBER_INVALID",
                "A confirmed boundary group contains an object outside the fluid body.",
                stage="reload_confirmed_cad",
                substep="boundary-group validation",
                objects=[{"name": name, "candidate_id": member} for member in non_faces],
                suggested_action="Boundary groups may contain only faces from the fluid body.",
            )
        overlap = [member for member in members if member in assigned]
        if overlap:
            raise PipelineError(
                "CAD_CONFIRMED_GROUP_OVERLAP",
                "Confirmed boundary groups contain overlapping faces.",
                stage="reload_confirmed_cad",
                substep="boundary-group validation",
                objects=[{"name": assigned[member], "candidate_id": member} for member in overlap]
                + [{"name": name, "candidate_id": member} for member in overlap],
                suggested_action="Assign each fluid face to exactly one boundary group.",
            )
        assigned.update({member: name for member in members})
    missing_faces = sorted(face_ids - set(assigned))
    if missing_faces:
        raise PipelineError(
            "CAD_CONFIRMED_GROUP_COVERAGE_INCOMPLETE",
            "Confirmed boundary groups do not cover every fluid face.",
            stage="reload_confirmed_cad",
            substep="boundary-group validation",
            objects=[{"candidate_id": face_id} for face_id in missing_faces],
            suggested_action="Assign every ungrouped fluid face to a boundary group with its intended role.",
        )
    return {
        "positive_volume": True,
        "nonempty_groups": True,
        "nonoverlapping_groups": True,
        "all_faces_grouped": True,
        "roles_complete": True,
        "body_ids": [body.id for body in positive],
        "face_count": len(face_ids),
        "group_count": len(groups),
    }
