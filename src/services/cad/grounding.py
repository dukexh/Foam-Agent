"""Geometry planning migrated from CFD-Agent-Integrated (MIT)."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Callable, Iterable
from .llm import GroundingLLMClient
from .settings import RuntimeConfig
from .models import *
from .errors import PipelineError
_DEFAULT_DETAIL_VIEWS = {"opening": ("Selected",), "seed": ("OwnerContext", "SelectedProxy"), "body": ("Selected",), "boundary": ("Selected", "OwnerContext")}
def _model_images(catalog: GeometryCatalog) -> list[Path]:
    references: dict[str, Path] = {}
    for row in catalog.images:
        path = Path(str(row.get("path", "")))
        if not path.is_file():
            continue
        view = row.get("view")
        if view in {"Front", "Top", "Right", "Isometric"} and not row.get("candidate_id"):
            references[str(view)] = path
    return [
        references[name] for name in ("Front", "Top", "Right", "Isometric") if name in references
    ]

def _opening_candidate_context(catalog: GeometryCatalog) -> dict[str, Any]:
    """Describe explicit native argument choices without pre-judging geometry."""
    return {
        "face_candidates": [row.id for row in catalog.faces],
        "loop_candidates": [row.id for row in catalog.loops],
        "edge_candidates": [row.id for row in catalog.edges],
        "body_candidates": [row.id for row in catalog.bodies],
        "seed_face_candidates": [row.id for row in catalog.faces],
        "selection_guidance": (
            "For extract, explicitly choose extraction_strategy=faces with face object IDs, "
            "or extraction_strategy=edges with exact loop IDs or exact edge IDs. Never infer "
            "an inner or outer loop from a face. SpaceClaim evaluates geometric feasibility. "
            "For reuse, include all supplied positive-volume fluid bodies, whether connected or independent, "
            "using fluid_body_id for one or fluid_body_ids for multiple bodies. Do not select openings or a seed. "
            "Merging is a separate prompt-defined operation; multiple bodies do not imply a union."
        ),
    }

def _candidate_details(
    requests: Iterable[CandidateDetailRequest], limit: int = 12
) -> list[dict[str, Any]]:
    """Apply stable default views and combine requests for the same candidate."""

    combined: dict[str, dict[str, Any]] = {}
    for request in requests:
        item = combined.setdefault(
            request.candidate_id,
            {
                "candidate_id": request.candidate_id,
                "purpose": request.purpose,
                "detail_views": [],
                "reason": request.reason,
            },
        )
        for view in request.views or _DEFAULT_DETAIL_VIEWS[request.purpose]:
            if view not in item["detail_views"]:
                item["detail_views"].append(view)
    if len(combined) > limit:
        raise PipelineError(
            "CAD_DETAIL_REQUEST_LIMIT",
            f"This detail request contains {len(combined)} candidates, exceeding the {limit}-candidate limit.",
            stage="understand_prompt",
            suggested_action="Narrow the candidate set before requesting more detail images.",
        )
    return list(combined.values())

def _validate_detail_requests(catalog: GeometryCatalog, requests: list[dict[str, Any]]) -> None:
    objects = catalog.by_id()
    for request in requests:
        candidate_id = request["candidate_id"]
        candidate = objects.get(candidate_id)
        if candidate is None:
            raise PipelineError(
                "CAD_CANDIDATE_UNKNOWN",
                "The model requested a candidate absent from the current geometry catalog.",
                stage="understand_prompt",
                objects=[{"candidate_id": candidate_id}],
                suggested_action="Reload the CAD and select an object present in the catalog.",
            )
        if request["purpose"] == "seed" and candidate.kind != "face":
            raise PipelineError(
                "CAD_SEED_NOT_FACE",
                "The inner-wall seed must be a face object.",
                stage="understand_prompt",
                objects=[{"candidate_id": candidate_id}],
                suggested_action="Choose a face on the internal flow-path wall as the seed.",
            )
        if request["purpose"] == "opening" and candidate.kind not in {"face", "loop", "edge"}:
            raise PipelineError(
                "CAD_OPENING_UNSUPPORTED_OBJECT",
                "An opening candidate must be a face, loop, or edge.",
                stage="understand_prompt",
                objects=[{"candidate_id": candidate_id}],
                suggested_action="Choose a face, loop, or edge that represents the requested native argument.",
            )
        if request["purpose"] == "body" and candidate.kind != "body":
            raise PipelineError(
                "CAD_BODY_NOT_BODY",
                "A fluid-body candidate must be a body object.",
                stage="select_fluid_body",
                objects=[{"candidate_id": candidate_id}],
                suggested_action="Choose a positive-volume solid body from the current catalog.",
            )
        if request["purpose"] == "boundary" and candidate.kind != "face":
            raise PipelineError(
                "CAD_BOUNDARY_NOT_FACE",
                "A boundary-group candidate must be a face object.",
                stage="plan_boundary_groups",
                objects=[{"candidate_id": candidate_id}],
                suggested_action="Choose a face on the selected fluid body.",
            )

def _validate_final_selection(
    catalog: GeometryCatalog, answer: CadSelectionPlan
) -> CadSelectionPlan:
    if answer.status != "selected" or answer.fluid_domain_action == "ambiguous":
        return answer
    universe = catalog.by_id()
    if answer.fluid_domain_action == "reuse":
        body_ids = answer.fluid_body_ids or [str(answer.fluid_body_id)]
        for body_id in body_ids:
            body = universe.get(body_id)
            if body is None or body.kind != "body":
                raise PipelineError(
                    "CAD_FLUID_BODY_INVALID",
                    "The requested reusable fluid domain is not an existing body.",
                    stage="understand_prompt",
                    objects=[{"candidate_id": body_id}],
                    suggested_action="Choose existing positive-volume bodies for reuse.",
                )
        body_ids = [
            body.id
            for body in catalog.bodies
            if body.solid_or_sheet == "solid" and (body.volume_m3 or 0.0) > 0.0
        ]
        return answer.model_copy(
            update={
                "fluid_body_id": body_ids[0] if len(body_ids) == 1 else None,
                "fluid_body_ids": body_ids if len(body_ids) != 1 else [],
            }
        )
    requested = [value for item in answer.openings for value in item.object_ids]
    requested.append(str(answer.seed_inner_wall_id))
    unknown = [candidate_id for candidate_id in requested if candidate_id not in universe]
    if unknown:
        raise PipelineError(
            "CAD_CANDIDATE_UNKNOWN",
            "The model returned a final candidate absent from the current geometry catalog.",
            stage="understand_prompt",
            objects=[{"candidate_id": value} for value in unknown],
            suggested_action="Reload the CAD and confirm the candidate objects again.",
        )
    if universe[str(answer.seed_inner_wall_id)].kind != "face":
        raise PipelineError(
            "CAD_SEED_NOT_FACE",
            "The model selected a fluid-domain seed that is not a face.",
            stage="understand_prompt",
            objects=[{"candidate_id": str(answer.seed_inner_wall_id)}],
            suggested_action="Choose a face on the internal flow-path wall as the seed.",
        )
    expected_kinds = {"face": "face", "loop": "loop", "edges": "edge"}
    unsupported = [
        object_id
        for opening in answer.openings
        for object_id in opening.object_ids
        if universe[object_id].kind != expected_kinds[opening.selection_kind]
    ]
    if unsupported:
        raise PipelineError(
            "CAD_OPENING_UNSUPPORTED_OBJECT",
            "Selected extraction objects do not match the explicit extraction method.",
            stage="understand_prompt",
            objects=[{"candidate_id": candidate_id} for candidate_id in unsupported],
            suggested_action="Use faces for face extraction, or loops and edges for edge extraction.",
        )
    return answer

def _clarification_plan(
    *,
    status: str,
    reference_view: str,
    explanation: str,
    missing_information: list[str],
    fluid_domain_action: str = "ambiguous",
    fluid_domain_evidence: str = "",
) -> CadSelectionPlan:
    return CadSelectionPlan(
        status=status,
        reference_view=reference_view,
        explanation=explanation,
        missing_information=missing_information,
        fluid_domain_action=fluid_domain_action,
        fluid_domain_evidence=fluid_domain_evidence,
    )

def plan_cad_selection(
    *,
    catalog: GeometryCatalog,
    user_prompt: str,
    audit_dir: str | Path,
    config: RuntimeConfig | None = None,
    failure_context: list[dict[str, Any]] | None = None,
    detail_renderer: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> CadSelectionPlan:
    settings = config or RuntimeConfig()
    client = GroundingLLMClient.from_runtime_config(
        config=settings,
        audit_dir=Path(audit_dir) / "selection",
    )
    images = _model_images(catalog)
    if images:
        probe = client.probe_vision()
        if probe.status.value != "verified":
            raise RuntimeError(settings.model + " image input is not available: " + probe.reason)
    context = catalog.public_dict()
    if failure_context:
        context["previous_failed_attempts"] = failure_context
        context["failure_guidance"] = (
            "Use native failure evidence to reconsider the previous selection or extraction method. "
            "Reviewer diagnoses are hypotheses, not geometric proof. Do not silently repeat an "
            "unchanged failed plan; request more detail or clarification when evidence is insufficient."
        )
    context["opening_candidates"] = _opening_candidate_context(catalog)
    prompt = (
        "USER REQUEST:\n"
        + user_prompt
        + "\n\nCANDIDATE CATALOG (metres, global SpaceClaim XYZ):\n"
        + json.dumps(context, ensure_ascii=False)
        + "\n\nIMAGE ORDER: Front, Top, Right, Isometric. These are global reference views only."
    )
    # Preserve the original single-pass public helper for API callers that do
    # not provide a SpaceClaim detail renderer.
    if detail_renderer is None:
        answer = client.invoke(
            system_prompt=(
                "You translate a user's CAD intent into executable SpaceClaim object references.\n"
                "\n"
                "Use only the supplied neutral candidate catalog and images. Candidate IDs are temporary\n"
                "references for this run. Match descriptions using geometry type, size, position in the\n"
                "declared reference view, ownership, adjacency and visible shape. Never use file names,\n"
                "named groups or prior case knowledge as answers.\n"
                "\n"
                'Also return fluid_domain_action. Use "reuse" only when the user explicitly states that the\n'
                'supplied CAD is already the fluid domain. Use "extract" when the user does not make that\n'
                'declaration or asks for volume extraction. Use "ambiguous" when the request conflicts or\n'
                "cannot be resolved. fluid_domain_evidence must briefly quote or faithfully identify the user\n"
                "statement that supports this action. The host validates topology; never infer reuse from a\n"
                "closed solid alone.\n"
                "\n"
                "For extract, return every requested opening, one seed face, and one explicit extraction_strategy.\n"
                "For extraction_strategy=faces, each opening must explicitly select a face. For\n"
                "extraction_strategy=edges, each opening must explicitly select a loop or one or more edges.\n"
                "Do not replace a selected face with its inner or outer loop, do not infer closure from shape,\n"
                "and do not decide geometric feasibility for SpaceClaim. Preserve user boundary names and roles.\n"
                "\n"
                'When fluid_domain_action is "reuse", include all supplied positive-volume fluid bodies, '
                "using fluid_body_id for one or fluid_body_ids for multiple bodies. Do not infer merging.\n"
                "Do not return openings, a seed, or an extraction strategy for reuse; actual boundary faces\n"
                "will be selected after all target bodies are retained.\n"
                "Do not invent a role. If the request cannot be mapped uniquely, return ambiguous or\n"
                "not_found instead of guessing.\n"
            ),
            user_prompt=prompt,
            images=images,
            response_model=CadSelectionPlan,
        )
        return _validate_final_selection(catalog, answer)

    screening = client.invoke(
        system_prompt=(
            "Read the user's CAD request, the neutral geometry catalog and the four global reference views.\n"
            "\n"
            "Do not make a final geometry selection in this pass. Return `needs_details` and request only\n"
            "the candidate opening objects, inner-wall seed faces, or reusable bodies whose detailed images are necessary.\n"
            "Each candidate must be a real catalog ID. Use purpose `opening` for face, loop, or edge candidates;\n"
            "use purpose `seed` only for a face and purpose `body` only for a reusable body.\n"
            "\n"
            "Choose the smallest useful set. The application will render only the configured number of distinct objects in\n"
            f"one round ({settings.selection_max_candidates_per_round} objects maximum). An opening normally receives Selected evidence. A seed normally receives OwnerContext\n"
            "and SelectedProxy evidence. Request another supported detail view only when it resolves a concrete\n"
            "ambiguity. Never use filenames, existing group names or prior-case knowledge.\n"
            "\n"
            "If the request cannot be narrowed from the supplied evidence, return ambiguous or not_found and\n"
            "state the missing information. Set fluid_domain_action to reuse only when the user explicitly says\n"
            "that the supplied CAD already is the fluid domain.\n"
        ),
        user_prompt=prompt,
        images=images,
        response_model=CadSelectionScreening,
    )
    if screening.status != "needs_details":
        return _clarification_plan(
            status=screening.status,
            reference_view=screening.reference_view,
            explanation=screening.explanation,
            missing_information=screening.missing_information,
            fluid_domain_action=screening.fluid_domain_action,
            fluid_domain_evidence=screening.fluid_domain_evidence,
        )

    cached: dict[tuple[str, str], dict[str, Any]] = {}
    history = [{"screening": screening.model_dump(mode="json")}]
    requests = screening.candidates
    for round_index in range(1, settings.selection_max_detail_rounds + 1):
        details = _candidate_details(requests, settings.selection_max_candidates_per_round)
        _validate_detail_requests(catalog, details)
        missing = [
            {
                **item,
                "detail_views": [
                    view
                    for view in item["detail_views"]
                    if (item["candidate_id"], view) not in cached
                ],
            }
            for item in details
            if any((item["candidate_id"], view) not in cached for view in item["detail_views"])
        ]
        if not missing:
            return _clarification_plan(
                status="ambiguous",
                reference_view=screening.reference_view,
                explanation="The model requested only candidate detail evidence that was already supplied.",
                missing_information=[
                    "Clarify the object feature that distinguishes the remaining candidates."
                ],
                fluid_domain_action=screening.fluid_domain_action,
                fluid_domain_evidence=screening.fluid_domain_evidence,
            )
        rendered = detail_renderer(missing)
        for row in rendered:
            candidate_id = row.get("candidate_id")
            view = row.get("view")
            path = Path(str(row.get("path", "")))
            if candidate_id and view and path.is_file():
                cached[(str(candidate_id), str(view))] = dict(row)
        supplied = [
            cached[(item["candidate_id"], view)]
            for item in details
            for view in item["detail_views"]
            if (item["candidate_id"], view) in cached
        ]
        absent = [
            {"candidate_id": item["candidate_id"], "view": view}
            for item in details
            for view in item["detail_views"]
            if (item["candidate_id"], view) not in cached
        ]
        if absent:
            raise PipelineError(
                "CAD_DETAIL_RENDER_FAILED",
                "SpaceClaim did not produce every requested candidate detail image.",
                stage="understand_prompt",
                substep="candidate-detail rendering",
                objects=absent,
                suggested_action="Check whether the CAD objects remain valid and inspect the SpaceClaim rendering record.",
                evidence={"missing_details": absent},
            )
        detail_prompt = (
            "USER REQUEST:\n"
            + user_prompt
            + "\n\nCANDIDATE CATALOG (metres, global SpaceClaim XYZ):\n"
            + json.dumps(context, ensure_ascii=False)
            + "\n\nPREVIOUS DECISIONS:\n"
            + json.dumps(history, ensure_ascii=False)
            + "\n\nAVAILABLE EVIDENCE:\n"
            + json.dumps([{"candidate_id": key[0], "view": key[1]} for key in cached])
            + f"\nMAXIMUM OBJECTS PER ROUND: {settings.selection_max_candidates_per_round}"
            + "\n\nCURRENT REQUESTS:\n"
            + json.dumps(details, ensure_ascii=False)
            + "\n\nDETAIL ROUND: "
            + str(round_index)
            + " of "
            + str(settings.selection_max_detail_rounds)
            + "\nIMAGE ORDER: Front, Top, Right, Isometric, then these candidate details:\n"
            + json.dumps(
                [
                    {
                        "candidate_id": row["candidate_id"],
                        "view": row["view"],
                        "purpose": row.get("purpose"),
                    }
                    for row in supplied
                ],
                ensure_ascii=False,
            )
        )
        review = client.invoke(
            system_prompt=(
                "Use the global reference views, the neutral geometry catalog and only the requested candidate\n"
                "detail images to make the final CAD selection.\n"
                "\n"
                "For extract, return real IDs for every requested opening, a seed face, and one explicit method.\n"
                "For faces, select faces only; for edges, select loops or explicit edges only. Preserve roles and\n"
                "names and never infer an inner or outer loop. For reuse, include all supplied positive-volume fluid bodies, "
                "using fluid_body_id for one or fluid_body_ids for multiple bodies, with no opening\n"
                "or seed. Do not infer reuse merely because a body is closed: reuse requires the user's statement.\n"
                "\n"
                "If more candidate evidence is necessary, return needs_details and request only new object/view\n"
                "pairs. Do not request evidence already supplied. If the evidence remains insufficient, return\n"
                "ambiguous or not_found and explain exactly what the user must clarify. Never guess an object.\n"
            ),
            user_prompt=detail_prompt,
            images=[*images, *(Path(str(row["path"])) for row in supplied)],
            response_model=CadSelectionReview,
        )
        if review.status == "selected":
            return _validate_final_selection(catalog, review.selection)
        if review.status in {"ambiguous", "not_found"}:
            return _clarification_plan(
                status=review.status,
                reference_view=screening.reference_view,
                explanation=review.explanation,
                missing_information=review.missing_information,
                fluid_domain_action=screening.fluid_domain_action,
                fluid_domain_evidence=screening.fluid_domain_evidence,
            )
        history.append({"round": round_index, "review": review.model_dump(mode="json")})
        requests = review.detail_requests
    return _clarification_plan(
        status="ambiguous",
        reference_view=screening.reference_view,
        explanation=f"{settings.selection_max_detail_rounds} candidate-detail rounds did not uniquely identify the extraction objects.",
        missing_information=[
            "Clarify the location, shape, or ownership feature for these unresolved objects: "
            + ", ".join(request.candidate_id + " (" + request.purpose + ")" for request in requests)
        ],
        fluid_domain_action=screening.fluid_domain_action,
        fluid_domain_evidence=screening.fluid_domain_evidence,
    )

def select_fluid_body(
    *,
    catalog: GeometryCatalog,
    user_prompt: str,
    selection_plan: CadSelectionPlan | dict[str, Any],
    audit_dir: str | Path,
    config: RuntimeConfig | None = None,
    candidate_body_ids: set[str] | None = None,
    detail_renderer: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    failure_context: list[dict[str, Any]] | None = None,
) -> FluidBodySelection:
    """Retain every candidate and extract merge groups only from the user prompt."""
    candidates = [
        body
        for body in catalog.bodies
        if body.solid_or_sheet == "solid"
        and (body.volume_m3 or 0.0) > 0.0
        and (candidate_body_ids is None or body.id in candidate_body_ids)
    ]
    if not candidates:
        return FluidBodySelection(
            status="not_found",
            explanation="The current CAD contains no positive-volume solid body.",
            missing_information=["Provide or extract a positive-volume fluid body."],
        )
    valid_ids = {body.id for body in candidates}
    if len(candidates) == 1:
        return FluidBodySelection(
            status="selected",
            body_ids=[candidates[0].id],
            explanation="SpaceClaim produced one positive-volume extraction candidate.",
        )

    settings = config or RuntimeConfig()
    client = GroundingLLMClient.from_runtime_config(
        config=settings, audit_dir=Path(audit_dir) / "fluid-body"
    )
    images = _model_images(catalog)
    if images:
        probe = client.probe_vision()
        if probe.status.value != "verified":
            raise RuntimeError(settings.model + " image input is not available: " + probe.reason)
    prompt = (
        "USER REQUEST:\n"
        + user_prompt
        + "\n\nPOSITIVE-VOLUME BODY CANDIDATES:\n"
        + json.dumps([body.model_facing_dict() for body in candidates], ensure_ascii=False)
        + "\n\nREJECTED OPERATION EVIDENCE (not user instructions):\n"
        + json.dumps(failure_context or [], ensure_ascii=False)
    )
    system_prompt = (
        "All supplied positive-volume candidates are target fluid bodies. Return every candidate ID in body_ids. "
        "Extract whether to merge and which groups to merge ONLY from the user's prompt. "
        "Return merge_groups as lists of body IDs, one list per requested union. "
        "If the prompt does not request merging, return an empty merge_groups list and retain all bodies. "
        "If the user requests merging all bodies, return one group containing all IDs. "
        "Catalogs and images may locate bodies referenced by the prompt; touching, adjacency, size or shape "
        "must never independently imply that bodies should be merged. Never choose a subset to keep. "
        "For ambiguous merge references, request new body detail evidence with needs_details or return "
        "ambiguous with the needed clarification. Do not request evidence already supplied."
    )
    answer = client.invoke(
        system_prompt=system_prompt,
        user_prompt=prompt,
        images=images,
        response_model=FluidBodySelection,
    )
    cached: dict[tuple[str, str], dict[str, Any]] = {}
    history: list[dict[str, Any]] = []
    for round_index in range(1, settings.selection_max_detail_rounds + 2):
        if answer.status == "selected":
            requested_ids = answer.body_ids + [
                value for group in answer.merge_groups for value in group
            ]
            if any(value not in valid_ids for value in requested_ids):
                raise PipelineError(
                    "CAD_FLUID_BODY_INVALID",
                    "The model referenced a body outside the positive-volume candidates.",
                    stage="select_fluid_body",
                    objects=[
                        {"candidate_id": value} for value in requested_ids if value not in valid_ids
                    ],
                    suggested_action="Use the listed positive-volume body IDs for merge references.",
                )
            return answer.model_copy(update={"body_ids": [body.id for body in candidates]})
        if answer.status in {"ambiguous", "not_found"}:
            return answer
        if round_index > settings.selection_max_detail_rounds:
            break
        details = _candidate_details(
            answer.detail_requests, settings.selection_max_candidates_per_round
        )
        _validate_detail_requests(catalog, details)
        invalid = [
            item["candidate_id"]
            for item in details
            if item["purpose"] != "body" or item["candidate_id"] not in valid_ids
        ]
        if invalid:
            raise PipelineError(
                "CAD_FLUID_BODY_INVALID",
                "Body detail requests must name positive-volume extraction candidates.",
                stage="select_fluid_body",
                objects=[{"candidate_id": value} for value in invalid],
                suggested_action="Request views only for listed positive-volume body candidates.",
            )
        missing = [
            {
                **item,
                "detail_views": [
                    view
                    for view in item["detail_views"]
                    if (item["candidate_id"], view) not in cached
                ],
            }
            for item in details
        ]
        missing = [item for item in missing if item["detail_views"]]
        if not missing:
            return FluidBodySelection(
                status="ambiguous",
                explanation="The model requested only target-body detail evidence that was already supplied.",
                missing_information=["Clarify which fluid bodies the prompt asks to merge."],
            )
        for row in detail_renderer(missing) if detail_renderer else []:
            path = Path(str(row.get("path", "")))
            if row.get("candidate_id") and row.get("view") and path.is_file():
                cached[(str(row["candidate_id"]), str(row["view"]))] = dict(row)
        if any(
            (item["candidate_id"], view) not in cached
            for item in details
            for view in item["detail_views"]
        ):
            return FluidBodySelection(
                status="ambiguous",
                explanation="Requested target-body detail evidence was not produced.",
                missing_information=[
                    "Inspect the candidate bodies and clarify the requested merge groups."
                ],
            )
        history.append({"round": round_index, "review": answer.model_dump(mode="json")})
        supplied = [
            cached[(item["candidate_id"], view)]
            for item in details
            for view in item["detail_views"]
        ]
        answer = client.invoke(
            system_prompt=system_prompt,
            user_prompt=(
                prompt
                + "\n\nPREVIOUS DECISIONS:\n"
                + json.dumps(history, ensure_ascii=False)
                + "\n\nAVAILABLE DETAIL EVIDENCE:\n"
                + json.dumps(
                    [
                        {"candidate_id": row["candidate_id"], "view": row["view"]}
                        for row in supplied
                    ],
                    ensure_ascii=False,
                )
            ),
            images=[*images, *(Path(str(row["path"])) for row in supplied)],
            response_model=FluidBodySelection,
        )
    return FluidBodySelection(
        status="ambiguous",
        explanation="Prompt-defined merge references remain ambiguous after the configured detail rounds.",
        missing_information=["Clarify which fluid bodies the prompt asks to merge."],
    )

def _validate_boundary_groups(
    catalog: GeometryCatalog, target_body_ids: list[str], plan: BoundaryGroupPlan
) -> list[str]:
    if plan.target_catalog_id != catalog.catalog_id:
        return ["The group plan references a different target catalog."]
    target_faces = {face.id for face in catalog.faces if face.body_id in target_body_ids}
    assigned: dict[str, str] = {}
    errors: list[str] = []
    names: set[str] = set()
    if not plan.groups:
        errors.append("At least one boundary group is required.")
    for group in plan.groups:
        if not group.name.strip():
            errors.append("Boundary group name must not be blank.")
        if group.name in names:
            errors.append(f"Duplicate boundary group name: {group.name}.")
        names.add(group.name)
        if not group.face_ids:
            errors.append(f"Group {group.name!r} is empty.")
        for face_id in group.face_ids:
            if face_id not in target_faces:
                errors.append(
                    f"{group.name} contains face {face_id}, which is not on the target body."
                )
            elif face_id in assigned:
                errors.append(
                    f"Face {face_id} is assigned to both {assigned[face_id]} and {group.name}."
                )
            else:
                assigned[face_id] = group.name
    missing = sorted(target_faces - set(assigned))
    if missing:
        errors.append("Boundary groups omit target faces: " + ", ".join(missing))
    return errors

def plan_boundary_groups(
    *,
    catalog: GeometryCatalog,
    target_body_ids: list[str],
    user_prompt: str,
    selection_plan: CadSelectionPlan | dict[str, Any] | None = None,
    audit_dir: str | Path,
    config: RuntimeConfig | None = None,
    detail_renderer: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    failure_context: list[dict[str, Any]] | None = None,
) -> BoundaryGroupPlan:
    """Identify boundary faces across all retained fluid bodies."""

    settings = config or RuntimeConfig()
    prior_selection = (
        CadSelectionPlan.model_validate(selection_plan)
        if isinstance(selection_plan, dict)
        else selection_plan
    )
    target_faces = [face for face in catalog.faces if face.body_id in target_body_ids]
    if not target_faces:
        raise PipelineError(
            "CAD_TARGET_BODY_FACES_MISSING",
            "The selected target body has no catalogued faces.",
            stage="plan_boundary_groups",
            objects=[{"candidate_id": value} for value in target_body_ids],
            suggested_action="Re-query the isolated target body before selecting boundary groups.",
        )
    client = GroundingLLMClient.from_runtime_config(
        config=settings, audit_dir=Path(audit_dir) / "boundary-groups"
    )
    images = _model_images(catalog)
    if images:
        probe = client.probe_vision()
        if probe.status.value != "verified":
            raise RuntimeError(settings.model + " image input is not available: " + probe.reason)
    context = {
        "target_catalog_id": catalog.catalog_id,
        "rejected_operation_evidence": failure_context or [],
        "target_body_ids": target_body_ids,
        "target_bodies": [catalog.by_id()[value].model_facing_dict() for value in target_body_ids],
        "target_faces": [face.model_facing_dict() for face in target_faces],
        "prior_boundary_intent": [
            {
                "name": opening.name,
                "role": opening.role,
                "description": opening.description,
                "reason": opening.reason,
            }
            for opening in (prior_selection.openings if prior_selection else [])
        ],
    }
    prompt = (
        "USER REQUEST:\n"
        + user_prompt
        + "\n\nTARGET BODY CATALOG:\n"
        + json.dumps(context, ensure_ascii=False)
    )
    system_prompt = (
        "Assign every listed target face to one named boundary group. A group may contain multiple faces. "
        "Return every group with its physical role based on the user request; do not create a default wall group. "
        "Preserve group names explicitly supplied by the user. Otherwise generate lower-case ASCII group names "
        "with underscores, compatible with Fluent import labels. "
        "Use the prior boundary names, roles, and descriptions as semantic intent only; their old object IDs "
        "do not identify faces in this target catalog. Use only target face IDs. If local visual evidence is needed, request purpose=boundary for face IDs. "
        "Return ambiguous or not_found instead of guessing."
    )
    review = client.invoke(
        system_prompt=system_prompt,
        user_prompt=prompt,
        images=images,
        response_model=BoundaryGroupReview,
    )
    cached: dict[tuple[str, str], dict[str, Any]] = {}
    history: list[dict[str, Any]] = []
    for attempt in range(1, settings.selection_max_detail_rounds + 2):
        supplied = []
        if review.status == "selected":
            errors = _validate_boundary_groups(catalog, target_body_ids, review.selection)
            if not errors:
                return review.selection
            history.append(
                {
                    "attempt": attempt,
                    "review": review.model_dump(mode="json"),
                    "host_validation_errors": errors,
                }
            )
            if attempt > settings.selection_max_detail_rounds:
                break
        elif review.status in {"ambiguous", "not_found"}:
            return BoundaryGroupPlan(
                status=review.status,
                target_catalog_id=catalog.catalog_id,
                explanation=review.explanation,
                missing_information=review.missing_information,
            )
        else:
            if attempt > settings.selection_max_detail_rounds:
                break
            details = _candidate_details(
                review.detail_requests, settings.selection_max_candidates_per_round
            )
            _validate_detail_requests(catalog, details)
            invalid = [
                item["candidate_id"]
                for item in details
                if item["purpose"] != "boundary"
                or item["candidate_id"] not in {face.id for face in target_faces}
            ]
            if invalid:
                raise PipelineError(
                    "CAD_BOUNDARY_NOT_TARGET_FACE",
                    "Boundary detail requests must name faces on the selected target body.",
                    stage="plan_boundary_groups",
                    objects=[{"candidate_id": value} for value in invalid],
                    suggested_action="Request detail views only for target-body faces.",
                )
            missing = [
                {
                    **item,
                    "detail_views": [
                        view
                        for view in item["detail_views"]
                        if (item["candidate_id"], view) not in cached
                    ],
                }
                for item in details
            ]
            missing = [item for item in missing if item["detail_views"]]
            if not missing:
                return BoundaryGroupPlan(
                    status="ambiguous",
                    target_catalog_id=catalog.catalog_id,
                    explanation="Only previously supplied boundary evidence was requested.",
                    missing_information=["Clarify the unresolved boundary faces."],
                )
            if missing:
                for row in detail_renderer(missing) if detail_renderer else []:
                    path = Path(str(row.get("path", "")))
                    if row.get("candidate_id") and row.get("view") and path.is_file():
                        cached[(str(row["candidate_id"]), str(row["view"]))] = dict(row)
            if any(
                (item["candidate_id"], view) not in cached
                for item in details
                for view in item["detail_views"]
            ):
                return BoundaryGroupPlan(
                    status="ambiguous",
                    target_catalog_id=catalog.catalog_id,
                    explanation="Requested target-face detail evidence was not produced.",
                    missing_information=[
                        "Inspect the target CAD faces and provide boundary clarification."
                    ],
                )
            history.append({"attempt": attempt, "review": review.model_dump(mode="json")})
            supplied = [
                cached[(item["candidate_id"], view)]
                for item in details
                for view in item["detail_views"]
            ]
        review = client.invoke(
            system_prompt=system_prompt,
            user_prompt=(
                prompt
                + "\n\nPREVIOUS DECISIONS AND HOST FEEDBACK:\n"
                + json.dumps(history, ensure_ascii=False)
                + "\n\nAVAILABLE DETAIL EVIDENCE:\n"
                + json.dumps([{"candidate_id": key[0], "view": key[1]} for key in cached])
            ),
            images=[*images, *(Path(str(row["path"])) for row in supplied)],
            response_model=BoundaryGroupReview,
        )
    return BoundaryGroupPlan(
        status="ambiguous",
        target_catalog_id=catalog.catalog_id,
        explanation="Boundary groups were not complete and unambiguous within the configured review budget.",
        missing_information=["Clarify the intended boundary groups on the isolated target body."],
    )
