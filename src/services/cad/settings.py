"""Small serializable CAD settings, shared with the Windows execution worker."""
from dataclasses import dataclass
from pathlib import Path
import os
import tempfile


@dataclass
class RuntimeConfig:
    model: str = ''
    model_provider: str = ''
    ansys_root: str = ''
    runtime_root: str = ''
    spaceclaim_timeout_s: float = 900
    selection_max_candidates_per_round: int = 12
    selection_max_detail_rounds: int = 3

    def spaceclaim_executable(self):
        root = self.ansys_root or os.environ.get('AWP_ROOT241', '')
        if not root:
            raise ValueError('Set FOAMAGENT_ANSYS_ROOT or Windows AWP_ROOT241')
        path = Path(root) / 'scdm' / 'SpaceClaim.exe'
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def staging_root(self):
        return Path(self.runtime_root or tempfile.gettempdir()) / 'foamagent-cad'
