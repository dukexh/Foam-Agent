"""Structured native CAD failures; retained in durable workflow evidence."""

class PipelineError(RuntimeError):
    def __init__(self, code, message, *, stage='', substep='', objects=None,
                 suggested_action='', evidence=None):
        super().__init__(message)
        self.detail = dict(code=code, stage=stage, substep=substep,
                           objects=objects or [], suggested_action=suggested_action)
        self.evidence = evidence or {}
