"""Windows-only native execution. No model calls or workflow decisions."""
import contextlib
import json
from pathlib import Path
import sys
import traceback

from .settings import RuntimeConfig
from .native import SpaceClaimRunner
from .build import SpaceClaimBuildAdapter
from .models import GeometryCatalog


def execute(request):
    settings = RuntimeConfig(**request.get('settings', {}))
    args = request.get('args', {})
    operation = request['operation']
    root = Path(request['runtime'])
    root.mkdir(parents=True, exist_ok=True)
    if operation == 'save':
        return SpaceClaimBuildAdapter.save_current_document(
            args['labeling'], args['geometry'], timeout_s=settings.spaceclaim_timeout_s)
    if operation in {'catalog', 'select', 'details'}:
        reader = SpaceClaimRunner(output_dir=root / request['call_id'],
                                  ui_mode='hidden', config=settings)
        try:
            if operation == 'catalog':
                catalog, record = reader.catalog(Path(args['geometry']), render_candidates=False)
                return {'catalog': catalog.model_dump(mode='json'), 'record_path': str(record)}
            catalog = GeometryCatalog.model_validate(args['catalog'])
            if operation == 'details':
                return reader.render_candidate_details(Path(args['geometry']), catalog, args['requests'])
            return reader.select(Path(args['geometry']), catalog, args['ids'],
                                 views=args['views']).model_dump(mode='json')
        finally:
            reader.close()
    builder = SpaceClaimBuildAdapter(runtime_dir=root, ui_mode=request['ui_mode'], config=settings)
    if operation == 'open_editor':
        return builder._execute('open_document',
                                {'input': args['geometry'], 'output': args['geometry']}, keep_open=True)
    if operation not in {'extract_volume', 'merge_bodies', 'isolate_bodies', 'label_faces'}:
        raise ValueError('Unknown native CAD operation: ' + operation)
    return getattr(builder, operation)(**args)


if __name__ == '__main__':
    request_path = Path(sys.argv[1])
    request = json.loads(request_path.read_text(encoding='utf-8'))
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = execute(request)
        response = {'ok': True, 'result': result}
    except Exception as exc:
        response = {'ok': False, 'error': str(exc), 'type': type(exc).__name__,
                    'detail': getattr(exc, 'detail', {}),
                    'evidence': getattr(exc, 'evidence', {}), 'traceback': traceback.format_exc()}
    Path(request['response']).write_text(json.dumps(response, ensure_ascii=False), encoding='utf-8')
    sys.exit(0 if response['ok'] else 1)
