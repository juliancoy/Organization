"""Longest recorded work first; workers claim one application suite at a time."""
import json
import statistics
from pathlib import Path

SHARED=('identity-python','identity','visual','web','workspace','chat')
DEFAULTS={'identity-python':130,'identity':80,'visual':75,'web':65,'workspace':20,'chat':10}

def pending(folder):
    samples={suite:[] for suite in SHARED}
    for path in sorted(Path(folder).glob('*.json'),reverse=True)[:20]:
        try:run=json.loads(path.read_text())
        except (OSError,ValueError):continue
        for host in run.get('hosts',[]):
            for suite,job in host.get('jobs',{}).items():
                value=job.get('duration_seconds')
                if suite in samples and job.get('status')=='passed' and isinstance(value,(int,float)) and value>0:
                    samples[suite].append(value)
    return sorted(SHARED,key=lambda suite:statistics.median(samples[suite]) if samples[suite] else DEFAULTS[suite],reverse=True)
