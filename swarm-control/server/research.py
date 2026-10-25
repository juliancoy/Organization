"""Allowlisted operational progress only; never serve genomic reports or records."""
import json
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path('/mnt/PILEDRIVE/genomics-workbench')


def snapshot(root=ROOT):
    result = {'observed_at': datetime.now(timezone.utc).isoformat(), 'run': None,
              'public_downloads': []}
    runs = sorted((root / 'distributed').glob('run-*/state.json'), key=lambda p: p.stat().st_mtime, reverse=True)
    if runs:
        try:
            path = runs[0]
            state = json.loads(path.read_text())
            jobs = list(state.get('jobs', {}).values())
            verification = path.with_name('verification.json')
            verified = json.loads(verification.read_text()).get('passed') is True if verification.exists() else False
            result['run'] = {'status': state.get('status') if state.get('status') in
                             ('passed', 'running', 'failed', 'stopped', 'preparing') else 'unknown',
                             'updated_at': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                             'total': len(jobs), 'completed': sum(j.get('status') == 'passed' for j in jobs),
                             'failed': sum(j.get('status') == 'failed' for j in jobs),
                             'workers': len({j.get('host') for j in jobs if j.get('status') == 'passed' and j.get('host')}),
                             'verified': verified and state.get('status') == 'passed'}
        except (OSError, ValueError, TypeError, AttributeError):
            result['run'] = None
    for name in ('Watson', 'Venter', 'HG001', 'HG002', 'HG005'):
        folder = root / 'public' / name
        # Download presence is not scientific validation or successful analysis.
        partial = any(folder.glob('*.part'))
        present = any(p.is_file() and p.suffix == '.gz' for p in folder.glob('*'))
        result['public_downloads'].append({'name': name, 'status':
            'download present; validation unconfirmed' if present else
            'partial download; completion unconfirmed' if partial else 'planned; no download observed'})
    return result
