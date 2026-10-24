"""Run the launcher and its Python, Worker, frontend and local browser checks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
RESULTS = ROOT / '.vm-results'


def main():
    if ROOT != Path('/home/ubuntu/Organization') or not Path('/etc/organization-disposable-vm').is_file():
        raise RuntimeError('Run guest tests through run.py --vm inside its disposable guest.')
    RESULTS.mkdir(exist_ok=True, mode=0o700)
    env = dict(os.environ, PATH=f'{ROOT}/.venv/bin:' + os.environ['PATH'],
               ORGPORTAL_LOCAL_BIND_ADDRESS='127.0.0.1',
               ORGPORTAL_LOCAL_PUBLIC_BASE_URL='https://localhost:8443',
               ORGPORTAL_ORGANIZATION_REPLICA_SOURCE='',
               ORGPORTAL_JOURNAL_AUTO_SYNC='false',
               ORGPORTAL_LOCAL_EMAIL_SENDING_ENABLED='false',
               ORGPORTAL_SESSION_TEST_PREFIX='bmoremedtech-',
               ORGPORTAL_SESSION_TEST_NETWORK='bmoremedtech',
               PIDP_OAUTH_TEST_DATABASE_URL='postgresql+asyncpg://test:vm-test@127.0.0.1:5432/oauth_test',
               BROWSER_BINARY='/usr/bin/google-chrome', CHROME_BIN='/usr/bin/google-chrome')
    checks = []

    def run(label, command, directory='.', timeout=900):
        print(f'Checking {label}…', flush=True)
        start = time.monotonic()
        with (RESULTS / f'{label}.log').open('w') as log:
            try:
                result = subprocess.run(command, cwd=ROOT / directory, env=env, stdout=log,
                                        stderr=subprocess.STDOUT, timeout=timeout)
                code = result.returncode
            except (OSError, subprocess.TimeoutExpired) as error:
                log.write(f'\n{error}\n')
                code = 1
        checks.append({'check': label, 'exitCode': code, 'seconds': round(time.monotonic() - start, 1)})
        (RESULTS / 'summary.json').write_text(json.dumps(checks, indent=2) + '\n')
        print(f'{label}: {"PASS" if code == 0 else "FAIL"} (see {label}.log)', flush=True)
        if code and label.startswith(('stack-', 'launcher-')):
            # Capture runtime failures before another startup recreates containers.
            for service in ['identity-api', 'community-api', 'chat-api', 'community-web', 'nginx-gateway']:
                with (RESULTS / f'{label}-{service}.log').open('w') as log:
                    subprocess.run(['docker', 'logs', '--tail', '200', f'lifetech-{service}'],
                                   stdout=log, stderr=subprocess.STDOUT, timeout=30)
        return code == 0

    run('workspace-unit', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'])
    run('portal-launcher-unit', [sys.executable, '-m', 'unittest', 'discover', '-s', 'scripts', '-p', 'test_local_*.py'], 'OrgPortal')
    run('service-names-unit', [sys.executable, '-m', 'unittest', 'discover', '-s', 'nginx/service-directory', '-p', 'test_names.py'], 'OrgPortal')
    from docker_utils import ensure_image
    ensure_image('postgres:15-alpine')
    run('identity-postgres-start', ['docker', 'run', '--pull=never', '--detach', '--name', 'organization-oauth-test',
        '--publish', '127.0.0.1:5432:5432', '--env', 'POSTGRES_DB=oauth_test',
        '--env', 'POSTGRES_USER=test', '--env', 'POSTGRES_PASSWORD=vm-test', 'postgres:15-alpine'])
    run('identity-postgres-ready', ['bash', '-c',
        'for attempt in {1..60}; do docker exec organization-oauth-test pg_isready -U test -d oauth_test && exit 0; sleep 1; done; exit 1'])
    run('identity-python', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'], 'PIdP')
    for package in ['OrgPortal/web', 'OrgPortal/org-worker', 'OrgPortal/chat-worker', 'PIdP/serverless']:
        label = package.replace('/', '-')
        run(label + '-tests', ['npm', 'test'], package)
        if package.endswith('/web'):
            run(label + '-build', ['npm', 'run', 'build'], package)
        else:
            run(label + '-types', ['npm', 'run', 'typecheck'], package)
    if run('stack-start', [sys.executable, 'run.py'], timeout=1500):
        # Exercise repeat startup against the same fresh database and volumes.
        run('stack-repeat-start', [sys.executable, 'run.py'], timeout=900)
        run('launcher-login', [sys.executable, 'vm/launcher_login.py'], timeout=1500)
        run('local-sso', ['node', 'web/scripts/test-local-sso-registration.mjs'], 'OrgPortal')
        run('brand-themes', ['node', 'web/scripts/check-brand-themes.mjs'], 'OrgPortal')
        run('poster-editor', ['node', '--import', '../org-worker/node_modules/tsx/dist/loader.mjs', 'scripts/check-poster-editor.mjs'], 'OrgPortal/web')
        run('session-persistence', [sys.executable, 'web/scripts/test-local-session-persistence.py'], 'OrgPortal')
    else:
        checks.append({'check': 'local-browser-checks', 'exitCode': 1, 'reason': 'Stack startup failed'})
    run('container-status', ['docker', 'ps', '-a'])
    (RESULTS / 'summary.json').write_text(json.dumps(checks, indent=2) + '\n')
    return int(any(item['exitCode'] != 0 for item in checks))


if __name__ == '__main__':
    raise SystemExit(main())
