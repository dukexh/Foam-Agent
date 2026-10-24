"""Use Foam-Agent's model service for the migrated geometry planners."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import uuid
from pydantic import BaseModel


class VisionProbe(BaseModel):
    description: str


class GroundingLLMClient:
    def __init__(self, service, audit_dir):
        self.service = service
        self.audit_dir = Path(audit_dir)
        self._vision = None

    @classmethod
    def from_runtime_config(cls, config, audit_dir):
        from utils import LLMService
        service = getattr(config, '_service', None)
        if service is None:
            foam = copy.copy(config._foam_config)
            foam.model_provider = config.model_provider
            foam.model_version = config.model
            service = LLMService(foam)
            config._service = service
        client = cls(service, audit_dir)
        client._settings = config
        return client

    def probe_vision(self):
        cached = getattr(self._settings, '_vision_probe', None)
        if cached is not None:
            return cached
        # A real image request checks this provider/model's image transport, not its name.
        pixel = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAGUlEQVR4nGP8//8/AymAiSTVoxpGNQwpDQBVbQMdPVIhQwAAAABJRU5ErkJggg=='
        try:
            self.service.invoke('Briefly describe the supplied image.',
                                pydantic_obj=VisionProbe, images=[pixel])
            answer = SimpleNamespace(status=SimpleNamespace(value='verified'), reason='Image request succeeded')
        except Exception as exc:
            answer = SimpleNamespace(status=SimpleNamespace(value='unavailable'), reason=str(exc))
        self._settings._vision_probe = answer
        return answer

    def invoke(self, *, system_prompt, user_prompt, response_model, images=None):
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        record = self.audit_dir / (uuid.uuid4().hex + '.json')
        audit = dict(system_prompt=system_prompt, user_prompt=user_prompt,
                     images=[str(path) for path in images or []], schema=response_model.__name__)
        try:
            answer = self.service.invoke(user_prompt, system_prompt, response_model, images=images)
            audit['response'] = answer.model_dump(mode='json')
            return answer
        except Exception as exc:
            audit['error'] = str(exc)
            raise
        finally:
            record.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
