"""Restore a verified checkpoint into an empty mounted volume with trading off."""
import argparse
import hashlib
import json
import os
import zipfile
from pathlib import Path
from storage import atomic_json


def restore(archive, environ=None):
    env = os.environ if environ is None else environ
    if env.get('TRADING_ENABLED', '').lower() != 'false':
        raise RuntimeError('Set TRADING_ENABLED=false before restoring state')
    root = Path(env.get('RAILWAY_VOLUME_MOUNT_PATH', '/data')).resolve()
    if not os.path.ismount(root):
        raise RuntimeError(f'Persistent volume is not mounted: {root}')
    if any((root/name).exists() for name in ('state.json','trades.csv')):
        raise RuntimeError('Existing ledger files found; refusing to overwrite')
    with zipfile.ZipFile(archive) as z:
        manifest = json.loads(z.read('manifest.json'))
        data = {name:z.read(name) for name in ('state.json','trades.csv')}
    if any(hashlib.sha256(b).hexdigest() != manifest['sha256'].get(n) for n,b in data.items()):
        raise RuntimeError('Recovery archive hash mismatch')
    state = json.loads(data['state.json'])
    if not isinstance(state,dict) or not isinstance(state.get('markets'),dict):
        raise RuntimeError('Invalid state structure')
    # The completed state file is the final restore marker. Refuse overwrites
    # above, including a prior partial restore; never silently reset history.
    with (root/'trades.csv').open('xb') as h:
        h.write(data['trades.csv']);h.flush();os.fsync(h.fileno())
    atomic_json(root/'state.json', state)
    return {'captured_utc':manifest['captured_utc'], 'restored_to':str(root)}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive')
    args=parser.parse_args()
    print(json.dumps(restore(args.archive)))
