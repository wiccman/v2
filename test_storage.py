import json
import pytest
import storage


def test_state_and_log_require_actual_mount_and_existing_ledger(tmp_path,monkeypatch):
    state=tmp_path/'state.json'; log=tmp_path/'trades.csv'
    with pytest.raises(RuntimeError,match='persistent'):
        storage.require_mm_storage(state,log,{})
    with pytest.raises(RuntimeError,match='not mounted'):
        storage.require_mm_storage(state,log,{'RAILWAY_VOLUME_MOUNT_PATH':str(tmp_path)})
    monkeypatch.setattr(storage.os.path,'ismount',lambda p:True)
    with pytest.raises(RuntimeError,match='missing'):
        storage.require_mm_storage(state,log,{'RAILWAY_VOLUME_MOUNT_PATH':str(tmp_path)})
    state.write_text('{"markets":{},"mm":{"markets":{}}}')
    storage.require_mm_storage(state,log,{'RAILWAY_VOLUME_MOUNT_PATH':str(tmp_path)})
    with pytest.raises(RuntimeError,match='inside'):
        storage.require_mm_storage(state,tmp_path.parent/'escaped.csv',{'RAILWAY_VOLUME_MOUNT_PATH':str(tmp_path)})


def test_symlink_cannot_escape_volume(tmp_path,monkeypatch):
    volume=tmp_path/'volume';volume.mkdir();outside=tmp_path/'outside.json';outside.write_text('{"markets":{}}')
    (volume/'state.json').symlink_to(outside)
    monkeypatch.setattr(storage.os.path,'ismount',lambda p:True)
    with pytest.raises(RuntimeError,match='inside'):
        storage.require_mm_storage(volume/'state.json',volume/'trades.csv',{'RAILWAY_VOLUME_MOUNT_PATH':str(volume)})


def test_atomic_write_preserves_ledger_values_and_syncs_file_and_directory(tmp_path,monkeypatch):
    calls=[];real=storage.os.fsync
    monkeypatch.setattr(storage.os,'fsync',lambda fd:(calls.append(fd),real(fd)))
    p=tmp_path/'state.json';value={'markets':{},'mm':{'cash':'-2.6500'}}
    storage.atomic_json(p,value)
    assert json.loads(p.read_text())==value
    assert len(calls)==2 and not p.with_suffix('.tmp').exists()


def test_restore_requires_trading_off_verifies_hashes_and_refuses_overwrite(tmp_path,monkeypatch):
    import hashlib,zipfile
    import restore_mm_state as restore_module
    mount=tmp_path/'mount';mount.mkdir();archive=tmp_path/'recovery.zip'
    data={'state.json':b'{"markets":{},"mm":{"markets":{}}}','trades.csv':b'time_utc,event\n'}
    manifest={'captured_utc':'20260919T025002Z','sha256':{n:hashlib.sha256(b).hexdigest() for n,b in data.items()}}
    with zipfile.ZipFile(archive,'w') as z:
        for n,b in data.items():z.writestr(n,b)
        z.writestr('manifest.json',json.dumps(manifest))
    env={'RAILWAY_VOLUME_MOUNT_PATH':str(mount),'TRADING_ENABLED':'true'}
    monkeypatch.setattr(restore_module.os.path,'ismount',lambda p:True)
    with pytest.raises(RuntimeError,match='TRADING_ENABLED=false'):restore_module.restore(archive,env)
    env['TRADING_ENABLED']='false'
    restore_module.restore(archive,env)
    assert json.loads((mount/'state.json').read_text())==json.loads(data['state.json'])
    assert (mount/'trades.csv').read_bytes()==data['trades.csv']
    with pytest.raises(RuntimeError,match='overwrite'):restore_module.restore(archive,env)


def test_restore_rejects_corrupt_archive_before_writing(tmp_path,monkeypatch):
    import zipfile
    import restore_mm_state as restore_module
    root=tmp_path/'mount';root.mkdir();p=tmp_path/'bad.zip'
    with zipfile.ZipFile(p,'w') as z:
        z.writestr('state.json','{"markets":{}}');z.writestr('trades.csv','')
        z.writestr('manifest.json','{"sha256":{"state.json":"wrong","trades.csv":"wrong"}}')
    monkeypatch.setattr(restore_module.os.path,'ismount',lambda p:True)
    with pytest.raises(RuntimeError,match='hash mismatch'):
        restore_module.restore(p,{'RAILWAY_VOLUME_MOUNT_PATH':str(root),'TRADING_ENABLED':'false'})
    assert not list(root.iterdir())
