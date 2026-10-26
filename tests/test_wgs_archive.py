import hashlib
import json
import subprocess
import sys
from scripts.wgs_archive import WRITE


def test_resume_archive_validates_both_checksums(tmp_path):
    payload=b'public reads'*1000
    path=tmp_path/'reads.fastq.gz';partial=tmp_path/'reads.fastq.gz.partial'
    partial.write_bytes(payload[:123])
    item={'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest(),'md5':hashlib.md5(payload).hexdigest()}
    result=subprocess.run([sys.executable,'-c',WRITE,str(path),json.dumps(item),'123'],input=payload[123:],capture_output=True)
    assert result.returncode==0,result.stderr
    assert path.read_bytes()==payload
    assert json.loads(path.with_name(path.name+'.receipt.json').read_text())['sha256']==item['sha256']


def test_bad_resumed_prefix_is_quarantined(tmp_path):
    payload=b'public reads'*1000
    path=tmp_path/'reads.fastq.gz';partial=tmp_path/'reads.fastq.gz.partial'
    partial.write_bytes(b'X'*123)
    item={'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest(),'md5':hashlib.md5(payload).hexdigest()}
    result=subprocess.run([sys.executable,'-c',WRITE,str(path),json.dumps(item),'123'],input=payload[123:],capture_output=True)
    assert result.returncode!=0
    assert not path.exists() and not partial.exists()
    assert len(list(tmp_path.glob('*.checksum-failed-*')))==1


def test_archive_proxy_keeps_ssh_host_verification_and_original_host():
    from scripts.wgs_archive import remote
    host={'local':False,'ssh':['ssh','-o','StrictHostKeyChecking=yes','-o','HostKeyAlias=inspiron.local','owner@inspiron.local'],'archive_mptcp':{'address':'10.42.0.142','port':8792}}
    original=list(host['ssh']);command=remote(host,'print(1)')
    assert 'StrictHostKeyChecking=yes' in command
    assert 'HostKeyAlias=inspiron.local' in command
    assert any(value.startswith('ProxyCommand=') and 'ssh_multipath.py' in value for value in command)
    assert host['ssh']==original
