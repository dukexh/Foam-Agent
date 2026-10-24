"""One parameterized feature core: real graph/state/planners, mocked native software/model."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_meshing_workflow import modules


@pytest.mark.parametrize('scenario', [
    'reuse', 'extract', 'multi', 'merge', 'pause_restart', 'save_exit', 'cancel',
    'rename', 'invalid_saved_geometry', 'native_failure', 'prepare_failure',
    'clarification', 'body_clarification', 'boundary_clarification', 'save_failure',
    'cli', 'empty_confirmation', 'no_corpus', 'image_transport', 'planner_details',
    'invalid_combination', 'resume_terminal', 'reopen', 'bridge', 'vision_probe',
    'stream_failure', 'stream_completed', 'resume_model_override',
    'interrupted', 'manual_replan',
])
def test_spaceclaim_cad_preparation(scenario, modules, monkeypatch, tmp_path):
    from config import Config
    from services.cad import workflow as cad, grounding
    from services.cad.models import (CadSelectionPlan, FluidBodySelection, BoundaryGroupPlan,
                                    GeometryCatalog, CadSelectionScreening, CadSelectionReview)
    config = Config(case_dir=str(tmp_path / 'run'), windows_python='C:/python.exe')
    source = tmp_path / '模型 with space.scdoc'
    source.write_bytes(b'original')
    native_calls = []
    controls = {'renamed': False, 'invalid': False, 'fail': None, 'failed_once': False}
    multi = scenario in {'multi', 'merge'}

    def catalog(bodies=None):
        bodies = bodies or ['B1', 'B2'] if multi else bodies or ['B1']
        faces = [{'id': 'F' + body[1:], 'kind': 'face', 'body_id': body} for body in bodies]
        name = 'edited' if controls['renamed'] else 'wall'
        return dict(catalog_id='c', geometry_id='g', bodies=[dict(id=body, kind='body',
                    moniker='m' + body, solid_or_sheet='solid', volume_m3=0 if controls['invalid'] else 1)
                    for body in bodies], faces=faces,
                    native_catalog={'internal': {'raw_groups': [{'raw_name': name,
                                     'member_ids': [face['id'] for face in faces]}]}})

    class Bridge:
        def __init__(self, cfg, state):
            self.root = tmp_path / 'windows'
            self.root.mkdir(exist_ok=True)
            self.state = state

        def call(self, operation, **args):
            native_calls.append(operation)
            if controls['fail'] == operation and not controls['failed_once']:
                controls['failed_once'] = True
                raise RuntimeError('native error evidence')
            if operation == 'catalog':
                return {'catalog': catalog(self.state.get('fake_bodies'))}
            if operation == 'select':
                return {'active_selection_verified': True}
            if operation == 'save':
                return {'saved': True}
            if operation == 'open_editor':
                return {'process_id': 42, 'save_bridge': {'request_path': 'new'}}
            if operation in {'extract_volume', 'merge_bodies', 'isolate_bodies', 'label_faces'}:
                Path(args['output']).write_bytes(operation.encode())
            if operation == 'extract_volume':
                return {'transfer': {'candidate_bodies': [{'moniker': 'mB1'}]}}
            if operation == 'merge_bodies':
                self.state['fake_bodies'] = ['B3']
                return {'target_body': {'moniker': 'mB3'}}
            if operation == 'isolate_bodies':
                return {'target_bodies': [{'moniker': 'm' + body} for body in self.state.get('fake_bodies', ['B1', 'B2'] if multi else ['B1'])]}
            if operation == 'label_faces':
                return {'groups': args['groups'], 'coverage': 1, 'total_faces': 1}
            return []

    monkeypatch.setattr(cad, 'WindowsBridge', Bridge)
    chosen = dict(status='selected', reference_view='Isometric', explanation='user request',
                  fluid_domain_action='reuse', fluid_body_id=None if multi else 'B1',
                  fluid_body_ids=['B1', 'B2'] if multi else [])
    if scenario == 'extract':
        chosen.update(fluid_domain_action='extract', fluid_body_id=None, fluid_body_ids=[],
                      extraction_strategy='faces', seed_inner_wall_id='F1',
                      openings=[dict(selection_kind='face', object_ids=['F1'], name='wall',
                                     role='wall', description='opening', reason='user')])
    monkeypatch.setattr(grounding, 'plan_cad_selection', lambda **kw: CadSelectionPlan(**chosen))
    def body_choice(**kw):
        ids = sorted(kw['candidate_body_ids'])
        return FluidBodySelection(status='selected', body_ids=ids,
                                  merge_groups=[ids] if scenario == 'merge' else [], explanation='user')
    monkeypatch.setattr(grounding, 'select_fluid_body', body_choice)
    def boundary_plan(**kw):
        ids = [face.id for face in kw['catalog'].faces]
        return BoundaryGroupPlan(status='selected', target_catalog_id='c',
                                 groups=[dict(name='wall', role='wall', face_ids=ids,
                                              reason='request')], explanation='user')
    monkeypatch.setattr(grounding, 'plan_boundary_groups', boundary_plan)
    monkeypatch.setattr(modules.preparation, 'load_agent_resources', lambda *_: pytest.fail('CAD loaded solver corpus'))

    if scenario == 'vision_probe':
        import base64
        from io import BytesIO
        from PIL import Image
        from services.cad.llm import GroundingLLMClient
        from services.cad.settings import RuntimeConfig
        calls = []
        class Service:
            def invoke(self, *args, **kw):
                image = kw['images'][0].split(',', 1)[1]
                Image.open(BytesIO(base64.b64decode(image))).verify()
                calls.append(1)
                return SimpleNamespace(description='white image')
        cfg = RuntimeConfig()
        cfg._service = Service()
        client = GroundingLLMClient.from_runtime_config(cfg, tmp_path)
        assert client.probe_vision().status.value == 'verified'
        assert client.probe_vision().status.value == 'verified'
        assert len(calls) == 1
        return
    if scenario in {'stream_failure', 'stream_completed'}:
        from utils import _CodexResponsesWrapper
        wrapper = object.__new__(_CodexResponsesWrapper)
        wrapper._token, wrapper._model, wrapper._account_id = 'test', 'model', None
        wrapper._base_url, wrapper._stream = 'https://chatgpt.com/backend-api/codex', True
        wrapper._temperature, wrapper._instructions = 0, 'test'
        event = {'type': 'error', 'error': {'message': 'image rejected'}} if scenario == 'stream_failure' else {
            'type': 'response.completed', 'response': {'output': [{'content': [{'type': 'output_text', 'text': 'seen'}]}]}}
        response = SimpleNamespace(ok=True, iter_lines=lambda **kw: ['data: ' + json.dumps(event)])
        monkeypatch.setattr('utils.requests.post', lambda *a, **kw: response)
        if scenario == 'stream_failure':
            with pytest.raises(RuntimeError, match='image rejected'):
                wrapper.invoke([{'role': 'user', 'content': 'test'}])
        else:
            assert wrapper.invoke([{'role': 'user', 'content': 'test'}]).content == 'seen'
        return
    if scenario == 'bridge':
        from services.cad import bridge as transport
        windows = tmp_path / 'bridge-runtime'
        real_convert = transport.linux_path
        monkeypatch.setattr(transport, 'linux_path', lambda path: str(windows) if path == 'C:/runtime' else real_convert(path))
        response_image = tmp_path / 'native.png'
        response_image.write_bytes(b'preview')
        def run(command, **kw):
            request = json.loads(Path(command[-1]).read_text())
            assert request['operation'] == 'catalog'
            assert (Path(kw['cwd']) / 'services/cad/workers/spaceclaim_v241.py').is_file()
            assert not (Path(kw['cwd']) / 'services/cad/grounding.py').exists()
            Path(request['response']).write_text(json.dumps({'ok': True, 'result': {'images': [{'path': str(response_image)}]}}))
            return SimpleNamespace(returncode=0)
        monkeypatch.setattr(transport.subprocess, 'run', run)
        (tmp_path / 'run').mkdir()
        actual = transport.WindowsBridge(config, {'windows_runtime': 'C:/runtime', 'run_dir': str(tmp_path / 'run')})
        returned = actual.call('catalog', geometry='C:/model.scdoc')
        archived = Path(returned['images'][0]['path'])
        assert archived.read_bytes() == b'preview' and archived != response_image
        assert transport.linux_path('E:\\CAD inputs\\中文.scdoc') == '/mnt/e/CAD inputs/中文.scdoc'
        assert transport.windows_path('/mnt/e/CAD inputs/中文.scdoc') == 'E:\\CAD inputs\\中文.scdoc'
        return

    if scenario == 'cli':
        import foambench_main
        monkeypatch.setattr('sys.argv', ['foambench_main.py', '--geometry_path', str(source),
                           '--stop-after', 'cad', '--prompt_path', 'request.txt'])
        args = foambench_main.parse_args()
        assert args.geometry_path == str(source)
        for extra in (['--custom_mesh_path', 'x.msh'], ['--case_path', 'case']):
            monkeypatch.setattr('sys.argv', ['foambench_main.py', '--geometry_path', str(source),
                               '--stop-after', 'cad', '--prompt_path', 'request.txt', *extra])
            with pytest.raises(SystemExit):
                foambench_main.parse_args()
        monkeypatch.setattr('sys.argv', ['foambench_main.py', '--resume', str(tmp_path)])
        assert foambench_main.parse_args().resume == str(tmp_path)
        return
    if scenario == 'invalid_combination':
        with pytest.raises(ValueError, match='cannot be combined'):
            modules.main.main('CAD', config, custom_mesh_path='x', geometry_path=str(source), stop_after='cad')
        assert not native_calls
        return
    if scenario == 'image_transport':
        monkeypatch.undo()
        from utils import LLMService, _CodexResponsesWrapper
        service = object.__new__(LLMService)
        service.model_provider = 'openai-codex'
        for name in ('total_calls', 'total_prompt_tokens', 'total_completion_tokens', 'total_tokens', 'failed_calls'):
            setattr(service, name, 0)
        class Model:
            def get_num_tokens(self, text):
                assert isinstance(text, str)
                return 1
            def with_structured_output(self, schema):
                return self
            def invoke(self, messages):
                rich = messages[-1]['content']
                assert rich[0]['text'] == 'describe'
                blocks = _CodexResponsesWrapper._to_responses_input(messages)[-1]['content']
                assert blocks[1]['type'] == 'input_image'
                return SimpleNamespace(answer='seen')
        service.llm = Model()
        assert service.invoke('describe', pydantic_obj=SimpleNamespace, images=['data:image/png;base64,abc']).answer == 'seen'
        return
    if scenario == 'planner_details':
        monkeypatch.undo()
        from services.cad import grounding as g
        image = tmp_path / 'detail.png'
        image.write_bytes(b'image')
        replies = iter([
            CadSelectionScreening(status='needs_details', reference_view='Isometric',
                candidates=[dict(candidate_id='B1', purpose='body', views=['Selected'], reason='view')],
                explanation='need detail', fluid_domain_action='reuse'),
            CadSelectionReview(status='selected', selection=CadSelectionPlan(**chosen), explanation='selected')])
        class Client:
            def invoke(self, **kw):
                return next(replies)
        monkeypatch.setattr(g.GroundingLLMClient, 'from_runtime_config', lambda **kw: Client())
        result = g.plan_cad_selection(catalog=GeometryCatalog.model_validate(catalog()), user_prompt='existing fluid domain',
            audit_dir=tmp_path, detail_renderer=lambda rows: [dict(candidate_id='B1', view='Selected', path=str(image))])
        assert result.fluid_body_id == 'B1'
        return
    if scenario == 'prepare_failure':
        controls['fail'] = 'catalog'
    elif scenario == 'native_failure':
        controls['fail'] = 'isolate_bodies'
    elif scenario in {'clarification', 'body_clarification', 'boundary_clarification'}:
        method, model = {'clarification': ('plan_cad_selection', CadSelectionPlan),
                         'body_clarification': ('select_fluid_body', FluidBodySelection),
                         'boundary_clarification': ('plan_boundary_groups', BoundaryGroupPlan)}[scenario]
        original = getattr(grounding, method)
        count = []
        def ambiguous(**kw):
            count.append(1)
            if len(count) == 1:
                fields = dict(status='ambiguous', explanation='Which object?', missing_information=['clarify'])
                if model is CadSelectionPlan:
                    fields['reference_view'] = 'Isometric'
                if model is BoundaryGroupPlan:
                    fields['target_catalog_id'] = 'c'
                return model(**fields)
            return original(**kw)
        monkeypatch.setattr(grounding, method, ambiguous)

    result = modules.main.main('This is an existing fluid domain. Name all faces wall.', config,
                               geometry_path=str(source), stop_after='cad')
    assert result.get('mesh_info') is None
    assert result.get('solver_planned') is False
    if scenario in {'native_failure', 'prepare_failure'}:
        assert result['workflow_status'] == 'failed'
        assert 'native error' in cad.load(config.case_dir)['error']
        result = cad.resume_cad(config, config.case_dir, action='retry')
    if scenario in {'clarification', 'body_clarification', 'boundary_clarification'}:
        assert result['workflow_status'] == 'paused'
        assert result['cad_state']['pause_kind'] == 'clarification'
        result = cad.resume_cad(config, config.case_dir, action='clarify', supplement='the only fluid region')
    assert result['workflow_status'] == 'paused'
    assert result['cad_state']['pause_kind'] == 'cad_confirmation'
    assert source.read_bytes() == b'original'
    before = list(native_calls)
    if scenario == 'cancel':
        result = cad.resume_cad(config, config.case_dir, action='cancel')
        assert result['workflow_status'] == 'cancelled'
        assert native_calls == before
        return
    if scenario == 'save_exit':
        result = cad.resume_cad(config, config.case_dir, action='save')
        assert result['workflow_status'] == 'paused'
        assert native_calls[-1] == 'save'
    if scenario == 'resume_model_override':
        monkeypatch.setenv('FOAMAGENT_VISION_MODEL', 'selected-vision-model')
        cad.resume_cad(config, config.case_dir, action='exit')
        assert config.vision_model == 'selected-vision-model'
    if scenario == 'reopen':
        result = cad.resume_cad(config, config.case_dir, action='reopen')
        assert result['workflow_status'] == 'paused'
        assert result['cad_state']['labeling']['process_id'] == 42
        assert native_calls[-1] == 'open_editor'
    if scenario == 'interrupted':
        saved = cad.load(config.case_dir)
        saved.update(status='running', step='validate_cad')
        cad.save(saved)
        result = cad.resume_cad(config, config.case_dir)
        assert result['cad_state']['pause_kind'] == 'recovery'
        assert native_calls == before
        result = cad.resume_cad(config, config.case_dir, action='retry')
        assert result['workflow_status'] == 'paused'
        assert native_calls.count('label_faces') == 1
    if scenario == 'manual_replan':
        saved = cad.load(config.case_dir)
        saved.update(status='failed', step='verify_selection', error='wrong object')
        cad.save(saved)
        result = cad.resume_cad(config, config.case_dir, action='clarify', supplement='use the inner wall')
        assert result['workflow_status'] == 'paused'
        assert native_calls.count('select') == 2
        assert result['cad_state']['clarifications'][-1] == 'use the inner wall'
    if scenario in {'pause_restart', 'save_exit'}:
        result = cad.resume_cad(Config(), config.case_dir)
        assert result['workflow_status'] == 'paused'
        assert native_calls.count('label_faces') == 1
    if scenario == 'empty_confirmation':
        from services.cad.cli import interactive
        answers = iter(['', 'exit'])
        monkeypatch.setattr('sys.stdin.isatty', lambda: True)
        monkeypatch.setattr('builtins.input', lambda *_: next(answers))
        assert interactive(result, config)['workflow_status'] == 'paused'
        assert native_calls == before
    if scenario == 'rename':
        controls['renamed'] = True
    if scenario == 'invalid_saved_geometry':
        controls['invalid'] = True
    if scenario == 'save_failure':
        controls['fail'] = 'save'
    result = cad.resume_cad(config, config.case_dir, action='approve')
    if scenario == 'rename':
        assert result['cad_state']['pause_kind'] == 'boundary_roles'
        result = cad.resume_cad(config, config.case_dir, action='approve', roles={'edited': 'wall'})
        assert result['cad_state']['result']['boundary_roles'] == {'edited': 'wall'}
    elif scenario in {'invalid_saved_geometry', 'save_failure'}:
        assert result['workflow_status'] == 'failed'
        if scenario == 'save_failure':
            assert native_calls[-1] == 'save'
        return
    assert result['workflow_status'] == 'cad_ready'
    assert Path(result['cad_state']['result']['confirmed_geometry']).is_file()
    assert (Path(config.case_dir) / 'cad-result.json').is_file()
    if scenario == 'reuse':
        assert 'extract_volume' not in native_calls
    if scenario == 'multi':
        assert len(result['cad_state']['result']['bodies']) == 2
        assert 'merge_bodies' not in native_calls
    if scenario == 'merge':
        assert native_calls.count('merge_bodies') == 1
        assert len(result['cad_state']['result']['bodies']) == 1
    if scenario == 'resume_terminal':
        terminal_calls = list(native_calls)
        assert cad.resume_cad(config, config.case_dir)['workflow_status'] == 'cad_ready'
        assert native_calls == terminal_calls
