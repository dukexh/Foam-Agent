"""Thin adapter for mesh planning in the main workflow."""

from services.meshing import plan_meshing


def meshing_planner_node(state):
    if state.get("geometry_path"):
        from services.cad.workflow import plan_cad
        return plan_cad(state)
    return plan_meshing(state)
