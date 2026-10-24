"""Durable CAD-only stage execution, with explicit human confirmation.

The parent Foam-Agent graph owns Preparation, Meshing Planner and Meshing.
This service stores native-step receipts and serializable continuation state.
There is no automatic repair loop and no solver or Fluent invocation.
"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import uuid

from . import grounding
from .boundaries import named_groups, confirm_roles, validate_confirmed_cad
from .bridge import WindowsBridge, linux_path
from .models import GeometryCatalog
from .settings import RuntimeConfig

STEPS = ('prepare', 'plan_selection', 'verify_selection', 'extract_volume',
         'select_fluid_body', 'plan_boundary_groups', 'label_faces', 'validate_cad', 'confirmation', 'reload')
CONFIG_FIELDS = ('windows_python', 'ansys_root', 'windows_runtime_root', 'spaceclaim_timeout_s',
                 'cad_ui_mode', 'model_provider', 'model_version', 'vision_model', 'vision_model_provider', 'temperature')
ENV_OVERRIDES = {'windows_python': 'FOAMAGENT_WINDOWS_PYTHON', 'ansys_root': 'FOAMAGENT_ANSYS_ROOT',
                 'model_provider': 'FOAMAGENT_MODEL_PROVIDER', 'model_version': 'FOAMAGENT_MODEL_VERSION',
                 'vision_model': 'FOAMAGENT_VISION_MODEL', 'vision_model_provider': 'FOAMAGENT_VISION_PROVIDER'}


def save(state):
    root = Path(state['run_dir'])
    root.mkdir(parents=True, exist_ok=True)
    destination = root / 'cad-state.json'
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(destination)
    return graph_update(state)


def load(root):
    return json.loads((Path(root) / 'cad-state.json').read_text(encoding='utf-8'))


def graph_update(state):
    return {'cad_state': state, 'case_dir': state['run_dir'], 'workflow_status': state['status'],
            'error_logs': [{'file': state.get('step', 'cad'), 'error_content': state['error']}]
            if state.get('error') else [], 'termination_reason': 'cad_failed' if state['status'] == 'failed' else None}


def failure(state, exc):
    state.setdefault('failures', []).append({'step': state.get('step'), 'error': str(exc),
                                            'evidence': getattr(exc, 'evidence', {})})
    state.update(status='failed', error=f'{type(exc).__name__}: {exc}',
                 error_evidence=getattr(exc, 'evidence', {}), error_detail=getattr(exc, 'detail', {}))
    return save(state)


def settings(config):
    value = RuntimeConfig(model=config.vision_model or config.model_version,
                          model_provider=config.vision_model_provider or config.model_provider)
    value._foam_config = config
    return value


def pause(state, question, *, kind='clarification'):
    state.update(status='paused', pause_kind=kind, question=question, error='')
    return save(state)


def prepared(state, config):
    bridge = WindowsBridge(config, state)
    native_root = bridge.root / 'native'
    native_root.mkdir(exist_ok=True)
    original = native_root / 'original.scdoc'
    if original.exists():
        original.chmod(original.stat().st_mode | 0o200)
    # Working copies must remain editable even when OneDrive marks the input read-only.
    shutil.copyfile(state['source_geometry'], original)
    state['original_geometry'] = state['working_geometry'] = str(original)
    state['catalog'] = bridge.call('catalog', geometry=str(original))['catalog']
    state.update(step='plan_selection', status='planning', error='')
    return save(state)


def prepare_cad(graph_state):
    from .paths import input_path
    from services.output_safety import prepare_output_directory
    config = graph_state['config']
    source = input_path(graph_state['geometry_path'])
    if not source.is_file() or source.suffix.lower() != '.scdoc':
        raise ValueError('--geometry_path must be an existing .scdoc file')
    root = prepare_output_directory(config.case_dir, overwrite=config.overwrite_case_dir, source_path=source)
    run_id = uuid.uuid4().hex
    windows_root = config.windows_runtime_root.rstrip('/\\') + '/' + run_id
    state = dict(run_dir=str(root), run_id=run_id, source_geometry=str(source),
                 user_requirement=graph_state['user_requirement'], windows_runtime=windows_root,
                 config={name: getattr(config, name) for name in CONFIG_FIELDS},
                 status='planning', step='prepare', error='', clarifications=[], roles={})
    save(state)
    try:
        return prepared(state, config)
    except Exception as exc:
        return failure(state, exc)


def plan_cad(graph_state):
    state = dict(graph_state['cad_state'])
    if state['status'] in {'failed', 'paused', 'cancelled', 'cad_ready'}:
        return graph_update(state)
    config = graph_state['config']
    try:
        bridge = WindowsBridge(config, state)
        catalog = GeometryCatalog.model_validate(state['catalog'])
        plan = grounding.plan_cad_selection(catalog=catalog, user_prompt=requirement(state),
            audit_dir=Path(state['run_dir']) / 'llm', config=settings(config),
            detail_renderer=lambda requests: bridge.call('details', geometry=state['original_geometry'],
                                                        catalog=state['catalog'], requests=requests))
        if plan.status != 'selected':
            return pause(state, plan.explanation + '\n' + '\n'.join(plan.missing_information))
        state.update(selection_plan=plan.model_dump(mode='json'), step='verify_selection', status='running')
        return save(state)
    except Exception as exc:
        return failure(state, exc)


def requirement(state):
    return '\n\n'.join([state['user_requirement'], *state.get('clarifications', [])])


def execute_cad(graph_state):
    state = dict(graph_state['cad_state'])
    if state['status'] in {'failed', 'paused', 'cancelled', 'cad_ready'}:
        return graph_update(state)
    config = graph_state['config']
    try:
        bridge = WindowsBridge(config, state)
        runtime = bridge.root / 'native'
        cfg = settings(config)
        while state['step'] not in {'confirmation', 'reload'}:
            step = state['step']
            # Persist before every operation. A process loss replays from an immutable prior document.
            save(state)
            plan = state['selection_plan']
            if step == 'verify_selection':
                ids = (plan.get('fluid_body_ids') or [plan['fluid_body_id']]) if plan['fluid_domain_action'] == 'reuse' else [
                    value for opening in plan['openings'] for value in opening['object_ids']] + [plan['seed_inner_wall_id']]
                result = bridge.call('select', geometry=state['original_geometry'], catalog=state['catalog'],
                                     ids=ids, views=list(dict.fromkeys([plan['reference_view'], 'Isometric'])))
                if not result.get('active_selection_verified'):
                    raise ValueError('SpaceClaim selection does not match the requested objects')
                state['native_selection'] = result
            elif step == 'extract_volume':
                original_catalog = GeometryCatalog.model_validate(state['catalog'])
                if plan['fluid_domain_action'] == 'reuse':
                    ids = plan.get('fluid_body_ids') or [plan['fluid_body_id']]
                    state['candidate_geometry'] = state['original_geometry']
                    state['candidate_monikers'] = [original_catalog.by_id()[item].moniker for item in ids]
                    state['extraction_catalog'] = state['catalog']
                else:
                    path = str(runtime / 'extraction-candidates.scdoc')
                    result = bridge.call('extract_volume', source=state['original_geometry'], output=path,
                                         catalog=original_catalog.native_catalog, selection_plan=plan)
                    state['extraction'] = result
                    state['candidate_geometry'] = path
                    state['candidate_monikers'] = [row['moniker'] for row in result['transfer']['candidate_bodies']]
                    state['extraction_catalog'] = bridge.call('catalog', geometry=path)['catalog']
            elif step == 'select_fluid_body':
                catalog = GeometryCatalog.model_validate(state['extraction_catalog'])
                allowed = {body.id for body in catalog.bodies if body.moniker in state['candidate_monikers']}
                choice = grounding.select_fluid_body(catalog=catalog, user_prompt=requirement(state),
                    selection_plan=plan, audit_dir=Path(state['run_dir']) / 'llm', config=cfg,
                    candidate_body_ids=allowed,
                    detail_renderer=lambda requests: bridge.call('details', geometry=state['candidate_geometry'],
                                                                catalog=state['extraction_catalog'], requests=requests))
                if choice.status != 'selected':
                    return pause(state, choice.explanation + '\n' + '\n'.join(choice.missing_information))
                state['body_plan'] = choice.model_dump(mode='json')
                source = state['candidate_geometry']
                monikers = {body.id: body.moniker for body in catalog.bodies}
                targets = [monikers[item] for item in choice.body_ids]
                for index, group in enumerate(choice.merge_groups):
                    current = {body.moniker: body.id for body in catalog.bodies}
                    group_monikers = [monikers[item] for item in group]
                    destination = str(runtime / f'merged-fluid-{index}.scdoc')
                    merged = bridge.call('merge_bodies', source=source, output=destination,
                        catalog=catalog.native_catalog, body_ids=[current[item] for item in group_monikers])
                    targets = [item for item in targets if item not in group_monikers] + [merged['target_body']['moniker']]
                    source = destination
                    catalog = GeometryCatalog.model_validate(bridge.call('catalog', geometry=source)['catalog'])
                current = {body.moniker: body.id for body in catalog.bodies}
                destination = str(runtime / 'target-fluid.scdoc')
                result = bridge.call('isolate_bodies', source=source, output=destination,
                    catalog=catalog.native_catalog, target_body_ids=[current[item] for item in targets])
                state['target_catalog'] = bridge.call('catalog', geometry=destination)['catalog']
                state['target_geometry'] = state['working_geometry'] = destination
                bodies = {body.moniker: body for body in GeometryCatalog.model_validate(state['target_catalog']).bodies}
                state['target_bodies'] = [bodies[row['moniker']].model_dump(mode='json') for row in result['target_bodies']]
            elif step == 'plan_boundary_groups':
                groups = grounding.plan_boundary_groups(
                    catalog=GeometryCatalog.model_validate(state['target_catalog']),
                    target_body_ids=[body['id'] for body in state['target_bodies']],
                    user_prompt=requirement(state), selection_plan=plan, config=cfg,
                    audit_dir=Path(state['run_dir']) / 'llm',
                    detail_renderer=lambda requests: bridge.call('details', geometry=state['target_geometry'],
                        catalog=state['target_catalog'], requests=requests))
                if groups.status != 'selected':
                    return pause(state, groups.explanation + '\n' + '\n'.join(groups.missing_information))
                state['boundary_plan'] = groups.model_dump(mode='json')
            elif step == 'label_faces':
                destination = str(runtime / 'labeled.scdoc')
                result = bridge.call('label_faces', source=state['target_geometry'], output=destination,
                    catalog=state['target_catalog']['native_catalog'],
                    target_body_ids=[body['id'] for body in state['target_bodies']],
                    groups=state['boundary_plan']['groups'], keep_editor_open=config.cad_ui_mode == 'gui')
                state['labeling'] = result
                state['roles'] = {row['name']: row['role'] for row in result['groups']}
                state['working_geometry'] = destination
            elif step == 'validate_cad':
                catalog = GeometryCatalog.model_validate(bridge.call('catalog', geometry=state['working_geometry'])['catalog'])
                state['cad_validation'] = validate_confirmed_cad(catalog=catalog, roles=state['roles'])
            else:
                raise ValueError('Unknown CAD step: ' + step)
            state['step'] = STEPS[STEPS.index(step) + 1]
            save(state)
        if state['step'] == 'confirmation':
            return pause(state, 'Inspect or edit the working CAD, then explicitly confirm.', kind='cad_confirmation')
        return reload_cad(state, config)
    except Exception as exc:
        return failure(state, exc)


def reload_cad(state, config):
    try:
        bridge = WindowsBridge(config, state)
        # Never open a disk copy as a fallback after a failed live-document save.
        if config.cad_ui_mode == 'gui' and not state.get('saved_before_reload'):
            state['save_receipt'] = bridge.call('save', labeling=state['labeling'], geometry=state['working_geometry'])
        state['saved_before_reload'] = False
        catalog = GeometryCatalog.model_validate(bridge.call('catalog', geometry=state['working_geometry'])['catalog'])
        state['confirmed_catalog'] = catalog.model_dump(mode='json')
        groups = named_groups(catalog)
        roles = {name: role for name, role in state['roles'].items() if name in groups}
        roles.update(state.pop('proposed_roles', {}))
        missing = [name for name in groups if not roles.get(name)]
        if missing:
            state['roles'] = {name: roles.get(name, '') for name in groups}
            state['saved_before_reload'] = True
            return pause(state, 'Assign a role for saved groups: ' + ', '.join(missing), kind='boundary_roles')
        roles = confirm_roles(catalog=catalog, proposed=roles, previous={})
        validation = validate_confirmed_cad(catalog=catalog, roles=roles)
        artifacts = Path(state['run_dir']) / 'artifacts'
        artifacts.mkdir(exist_ok=True)
        confirmed = artifacts / 'confirmed.scdoc'
        shutil.copy2(state['working_geometry'], confirmed)
        (artifacts / 'confirmed-catalog.json').write_text(catalog.model_dump_json(indent=2), encoding='utf-8')
        result = dict(status='cad_ready', confirmed_geometry=str(confirmed), boundary_roles=roles,
                      groups=groups, bodies=[body.model_dump(mode='json') for body in catalog.bodies],
                      coordinate_unit=catalog.coordinate_unit, cad_validation=validation)
        (Path(state['run_dir']) / 'cad-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        state.update(result=result, status='cad_ready', step='complete', roles=roles, error='', pause_kind='')
        return save(state)
    except Exception as exc:
        return failure(state, exc)


def resume_cad(config, root, *, action=None, supplement='', roles=None):
    state = load(root)
    for key, value in state['config'].items():
        override = os.getenv(ENV_OVERRIDES.get(key, ''), '')
        setattr(config, key, override or value)
    state['config'] = {name: getattr(config, name) for name in CONFIG_FIELDS}
    if state['status'] in {'cad_ready', 'cancelled'}:
        return graph_update(state)
    if action == 'cancel':
        state.update(status='cancelled', error='', pause_kind='')
        return save(state)
    if action in (None, 'exit'):
        if state['status'] in {'running', 'planning'}:
            return pause(state, 'The previous process stopped during this step. Explicitly retry to continue.',
                         kind='recovery')
        return graph_update(state)
    if action == 'save':
        try:
            if config.cad_ui_mode == 'gui' and state.get('labeling'):
                state['save_receipt'] = WindowsBridge(config, state).call(
                    'save', labeling=state['labeling'], geometry=state['working_geometry'])
            return save(state)
        except Exception as exc:
            return failure(state, exc)
    if action == 'reopen':
        if not state.get('labeling') or state['step'] not in {'confirmation', 'reload'}:
            raise ValueError('Reopening requires an existing labeled CAD awaiting confirmation')
        try:
            editor = WindowsBridge(config, state).call('open_editor', geometry=state['working_geometry'])
            state['labeling'].update(editor)
            state.update(step='confirmation', error='')
            return pause(state, 'The saved CAD was reopened. Inspect and explicitly confirm.', kind='cad_confirmation')
        except Exception as exc:
            return failure(state, exc)
    if action == 'approve':
        if state['status'] != 'paused' or state.get('pause_kind') not in {'cad_confirmation', 'boundary_roles'}:
            raise ValueError('CAD approval requires a pending confirmation')
        state.update(step='reload', status='running', proposed_roles=roles or {})
        save(state)
        return reload_cad(state, config)
    if action not in {'retry', 'clarify'}:
        raise ValueError('Expected approve, save, exit, cancel, retry, reopen or clarify')
    if action == 'clarify' and not supplement.strip():
        raise ValueError('Clarification must be non-empty')
    if supplement.strip():
        state['clarifications'].append(supplement.strip())
    if action == 'clarify':
        # This is an explicit user request to reconsider the affected plan, not automatic repair.
        restart = {'verify_selection': 'plan_selection', 'extract_volume': 'plan_selection',
                   'label_faces': 'plan_boundary_groups', 'validate_cad': 'plan_boundary_groups'}
        state['step'] = restart.get(state['step'], state['step'])
        if state['step'] == 'plan_selection':
            for key in ('selection_plan', 'native_selection', 'extraction', 'extraction_catalog',
                        'target_bodies', 'target_catalog', 'body_plan', 'boundary_plan', 'labeling'):
                state.pop(key, None)
            state['working_geometry'] = state['original_geometry']
        elif state['step'] in {'select_fluid_body', 'plan_boundary_groups'}:
            state.pop('boundary_plan', None)
            state.pop('labeling', None)
    # Only the current failed/paused stage is replayed; each modifying stage uses its prior CAD.
    if state['step'] == 'reload':
        state['step'] = 'confirmation'
        return pause(state, 'Inspect and save the CAD, then confirm again.', kind='cad_confirmation')
    state.update(status='running', error='', pause_kind='')
    save(state)
    graph = {'config': config, 'cad_state': state}
    if state['step'] == 'prepare':
        try:
            update = prepared(state, config)
        except Exception as exc:
            return failure(state, exc)
        graph.update(update)
    if graph['cad_state']['step'] == 'plan_selection':
        graph.update(plan_cad(graph))
    return execute_cad(graph)
