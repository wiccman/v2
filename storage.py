"""Durable state writes and the Railway volume preflight."""
import json
import os
from pathlib import Path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as handle:
        json.dump(value, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def require_mm_storage(state_path, log_path, environ=None):
    env = os.environ if environ is None else environ
    mount = env.get('RAILWAY_VOLUME_MOUNT_PATH')
    if not mount:
        raise RuntimeError('MM requires a persistent Railway volume; mount it at /data and restore state.json before starting')
    root = Path(mount).resolve()
    if not os.path.ismount(root):
        raise RuntimeError(f'MM storage path is not mounted: {root}')
    for name, path in [('state', state_path), ('log', log_path)]:
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(root) or resolved == root:
            raise RuntimeError(f'MM {name} must be inside the persistent volume')
    if not Path(state_path).is_file():
        raise RuntimeError('MM state.json is missing; restore the saved ledger rather than resetting the budget')
    state = json.loads(Path(state_path).read_text())
    if not isinstance(state, dict) or not isinstance(state.get('markets'), dict):
        raise RuntimeError('MM state file is invalid')
