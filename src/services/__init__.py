"""Service namespace. CAD inspection and recovery do not require a live model."""


class _LazyDefaultLLM:
    def __init__(self):
        self._service = None

    def __getattr__(self, name):
        if self._service is None:
            from utils import LLMService
            from config import Config
            self._service = LLMService(Config())
        return getattr(self._service, name)


global_llm_service = _LazyDefaultLLM()
