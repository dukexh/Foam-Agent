"""WSL to Windows native SpaceClaim calls, using task-local scripts and receipts."""
from dataclasses import asdict
import json
from pathlib import Path
import re
import shutil
import subprocess
import uuid

from .errors import PipelineError
from .settings import RuntimeConfig


def linux_path(value):
    if isinstance(value, str) and re.match(r'^[A-Za-z]:[\\/]', value):
        return '/mnt/' + value[0].lower() + '/' + value[3:].replace('\\', '/')
    return value


def windows_path(value):
    if not isinstance(value, str):
        return value
    match = re.match(r'^/mnt/([a-z])/(.*)$', value)
    if match:
        return match[1].upper() + ':\\' + match[2].replace('/', '\\')
    return value


def map_paths(value, convert):
    if isinstance(value, dict):
        return {key: map_paths(item, convert) for key, item in value.items()}
    if isinstance(value, list):
        return [map_paths(item, convert) for item in value]
    return convert(value)


class WindowsBridge:
    def __init__(self, config, state):
        self.config = config
        self.state = state
        self.root = Path(linux_path(state['windows_runtime']))
        self.root.mkdir(parents=True, exist_ok=True)
        self.package = self.root / 'package'
        self._stage_code()

    def _stage_code(self):
        target = self.package / 'services' / 'cad'
        target.mkdir(parents=True, exist_ok=True)
        (target.parent / '__init__.py').touch()
        (target / '__init__.py').touch()
        source = Path(__file__).parent
        for name in ('worker.py', 'settings.py', 'models.py', 'errors.py', 'native.py',
                     'build.py', 'openings.py', 'windows_process.py'):
            shutil.copy2(source / name, target / name)
        shutil.copytree(source / 'workers', target / 'workers', dirs_exist_ok=True)

    def call(self, operation, **args):
        call_id = operation + '-' + uuid.uuid4().hex[:10]
        transport = self.root / 'transport'
        transport.mkdir(exist_ok=True)
        response_path = transport / (call_id + '-response.json')
        request_path = transport / (call_id + '-request.json')
        settings = RuntimeConfig(ansys_root=self.config.ansys_root,
                                 runtime_root=str(windows_path(str(self.root))),
                                 spaceclaim_timeout_s=self.config.spaceclaim_timeout_s)
        request = dict(operation=operation, args=map_paths(args, windows_path), call_id=call_id,
                       runtime=windows_path(str(self.root / 'native')),
                       ui_mode=self.config.cad_ui_mode, settings=asdict(settings),
                       response=windows_path(str(response_path)))
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
        python = self.config.windows_python
        if not python:
            raise ValueError('Set FOAMAGENT_WINDOWS_PYTHON to a Windows Python with pydantic>=2 and Pillow')
        # The package is the working directory. Windows Python never imports the original CFD checkout.
        command = [linux_path(python), '-m', 'services.cad.worker', windows_path(str(request_path))]
        log_dir = Path(self.state['run_dir']) / 'logs'
        log_dir.mkdir(exist_ok=True)
        with (log_dir / (call_id + '.log')).open('wb') as log:
            try:
                completed = subprocess.run(command, cwd=self.package, stdout=log, stderr=log,
                                           timeout=self.config.spaceclaim_timeout_s + 60)
            except subprocess.TimeoutExpired as error:
                raise PipelineError('CAD_WORKER_TIMEOUT', str(error), evidence={'request': str(request_path)}) from error
        if not response_path.is_file():
            raise PipelineError('CAD_WORKER_NO_RESPONSE', f'Windows worker exited {completed.returncode}; see {log_dir / (call_id + ".log")}',
                                evidence={'request': str(request_path)})
        response = map_paths(json.loads(response_path.read_text(encoding='utf-8-sig')), linux_path)
        def archive_images(value):
            if isinstance(value, dict):
                for key, item in list(value.items()):
                    if key == 'path' and isinstance(item, str) and Path(item).suffix.lower() in {'.png', '.jpg', '.jpeg'}:
                        source = Path(item)
                        if source.is_file():
                            folder = Path(self.state['run_dir']) / 'artifacts' / 'evidence' / call_id
                            folder.mkdir(parents=True, exist_ok=True)
                            destination = folder / source.name
                            shutil.copy2(source, destination)
                            value[key] = str(destination)
                    else:
                        archive_images(item)
            elif isinstance(value, list):
                for item in value:
                    archive_images(item)
        archive_images(response)
        receipt = Path(self.state['run_dir']) / 'operations'
        receipt.mkdir(exist_ok=True)
        (receipt / (call_id + '.json')).write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding='utf-8')
        if not response.get('ok'):
            raise PipelineError('CAD_NATIVE_ERROR', response.get('error', 'SpaceClaim operation failed'),
                                evidence=response)
        return response['result']
