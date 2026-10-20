"""Exercise run.py --login without exposing its one-use URLs in test logs."""
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    if ROOT != Path('/home/ubuntu/Organization') or not Path('/etc/organization-disposable-vm').is_file():
        raise RuntimeError('This check requires the disposable test VM.')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path='/usr/bin/google-chrome', args=['--no-sandbox'])
        try:
            for role in ['member', 'admin']:
                process = subprocess.Popen([sys.executable, '-u', 'run.py', '--login', role, '--no-open'], cwd=ROOT,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
                lines = queue.Queue()

                def read_output(stream, output):
                    for line in stream:
                        output.put(line.strip())
                    output.put(None)

                reader = threading.Thread(target=read_output, args=(process.stdout, lines), daemon=True)
                reader.start()
                context = browser.new_context(ignore_https_errors=True)
                context.route('**/*', lambda route: route.continue_() if urlsplit(route.request.url).hostname in {'localhost', '127.0.0.1'} else route.abort())
                try:
                    deadline = time.monotonic() + 650
                    while True:
                        try:
                            line = lines.get(timeout=max(.1, deadline - time.monotonic()))
                        except queue.Empty:
                            raise RuntimeError(f'{role}: launcher did not provide its browser bridge in time') from None
                        if line is None:
                            raise RuntimeError(f'{role}: launcher exited before providing its browser bridge')
                        if line.startswith('http://localhost:'):
                            page = context.new_page()
                            page.goto(line, wait_until='domcontentloaded')
                            assert page.url.startswith('https://localhost:8443/'), 'Bridge must return to the local gateway'
                            break
                    assert process.wait(timeout=60) == 0, f'{role}: launcher failed'
                    session = context.request.get('https://localhost:8443/pidp/auth/session-token')
                    assert session.status == 200, f'{role}: browser cookie was not accepted'
                    token = session.json()['access_token']
                    profile = context.request.get('https://localhost:8443/pidp/auth/me', headers={'Authorization': 'Bearer ' + token})
                    assert profile.status == 200, f'{role}: local identity was not accepted'
                    print(f'PASS: run.py --login {role} --no-open and one-use browser handoff', flush=True)
                finally:
                    context.close()
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
                    process.stdout.close()
        finally:
            browser.close()


if __name__ == '__main__':
    main()
