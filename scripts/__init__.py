"""Organization worker and orchestration modules."""

# Adjacent owners remain importable in committed workspace snapshots, including
# new modules that do not yet have a compatibility symlink.
from pathlib import Path as _Path
_workspace = _Path(__file__).resolve().parents[1]
__path__.extend(str(_workspace / name / 'scripts') for name in ('JSwarm', 'HarborGenetics')
                if (_workspace / name / 'scripts').is_dir())
