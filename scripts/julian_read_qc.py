"""Private FASTQ QC using an existing validated physical-GPU socket when present."""
import argparse
import gzip
import json
import os
from pathlib import Path
import socket
import uuid
import time


def qc(paths, root):
    started=time.monotonic();request_seconds=0
    root = Path(root); sock = root / 'service.sock'
    if not sock.is_socket(): raise RuntimeError('Physical GPU QC service unavailable')
    folder = root / 'batches'; folder.mkdir(mode=0o700, exist_ok=True)
    hist = [0] * 94; bases = 0; devices = set(); chunks = 0
    sequence = bytearray(); quality = bytearray()
    def flush():
        nonlocal bases, chunks, request_seconds
        if not sequence: return
        ident = uuid.uuid4().hex
        seq = folder / (ident + '.seq'); qual = folder / (ident + '.qual')
        try:
            for p, data in ((seq, sequence), (qual, quality)):
                with p.open('xb') as f: f.write(data)
                p.chmod(0o600)
            request = {'version': 1, 'request_id': ident, 'sequence_path': str(seq), 'quality_path': str(qual), 'verify': chunks == 0}
            with socket.socket(socket.AF_UNIX) as s:
                request_started=time.monotonic()
                s.settimeout(120); s.connect(str(sock)); s.sendall(json.dumps(request).encode() + b'\n')
                with s.makefile('rb') as f: row = json.loads(f.readline(1024 * 1024))
                request_seconds+=time.monotonic()-request_started
            if row.get('request_id') != ident or not row.get('validated_backend') or row.get('software_fallback') or row.get('device_type') not in (1, 2):
                raise ValueError('GPU response not validated physical hardware: ' + str(row.get('error')))
            if chunks == 0 and row.get('exact_match') is not True: raise ValueError('GPU/CPU crosscheck failed')
            if row['bases'] != len(sequence) or sum(row['quality_histogram']) != len(sequence): raise ValueError('GPU length mismatch')
            for q, n in enumerate(row['quality_histogram']): hist[q] += n
            devices.add(row['device']); bases += len(sequence); chunks += 1
        finally:
            seq.unlink(missing_ok=True); qual.unlink(missing_ok=True)
        sequence.clear(); quality.clear()
    for path in paths:
        with gzip.open(path, 'rb') as f:
            while True:
                h = f.readline()
                if not h: break
                seq = f.readline().rstrip(b'\r\n'); plus = f.readline(); qual = f.readline().rstrip(b'\r\n')
                if not h.startswith(b'@') or not plus.startswith(b'+') or not seq or len(seq) != len(qual): raise ValueError('Invalid FASTQ')
                if len(sequence) + len(seq) > 4 * 1024**2: flush()
                sequence.extend(seq); quality.extend(qual)
    flush()
    elapsed=time.monotonic()-started
    return {'bases': bases, 'quality_histogram': hist, 'devices': sorted(devices), 'gpu_batches': chunks, 'backend': 'validated_physical_vulkan', 'first_batch_cpu_crosschecked': True,
            'phase_seconds':{'gpu_requests_including_queue':request_seconds,'read_parse_and_prepare':max(0,elapsed-request_seconds),'total':elapsed}}


if __name__ == '__main__':
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--gpu-root', required=True); p.add_argument('fastqs', nargs='+')
    a = p.parse_args(); print(json.dumps(qc(a.fastqs, a.gpu_root)))
