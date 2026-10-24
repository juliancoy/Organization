"""Share complete Docker images through bounded SSH pipes, without archive files."""
from collections import deque
import hashlib
import json
import re
import shlex
import subprocess
import time
import threading

from distributed_worker import NODE, PYTHON, SUITES

TOOLCHAIN = 'orgportal-distributed-python-node:1'
TOOLCHAIN_DOCKERFILE = f'FROM {NODE} AS node\nFROM {PYTHON}\nCOPY --from=node /usr/local/bin/node /usr/local/bin/node\n'


def required_images(host):
    return list(dict.fromkeys(SUITES[suite]['image'] for suite in host['suites']))


class ImageDistributor:
    def __init__(self, hosts, ssh, cancelled, notify):
        self.hosts, self.ssh, self.cancelled, self.notify = hosts, ssh, cancelled, notify
        self.images = {}
        self.host_images = {}

    def command(self, host, command):
        return self.ssh(host) + [shlex.join(command)] if host else command

    def inspect(self, host, image):
        try:
            result = subprocess.run(self.command(host, ['docker', 'image', 'inspect', image, '--format', '{{json .Id}}']),
                                    capture_output=True, text=True, timeout=25)
            if result.returncode:
                return None
            image_id = json.loads(result.stdout)
            return image_id if isinstance(image_id, str) and re.fullmatch(r'sha256:[a-f0-9]{64}', image_id) else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            # A disconnected peer must not hide a complete copy on another machine.
            return None

    def fingerprint(self, host, image):
        result = subprocess.run(self.command(host, ['docker', 'image', 'inspect', image]),
                                capture_output=True, text=True, timeout=25)
        if result.returncode:
            return None
        metadata = json.loads(result.stdout)[0]
        # Classic and containerd stores expose different IDs for identical images.
        content = {key: metadata[key] for key in ('RootFS', 'Config', 'Architecture', 'Os', 'Created')}
        content['Config'] = {key: value for key, value in content['Config'].items() if value is not None}
        content['Config']['WorkingDir'] = content['Config'].get('WorkingDir') or ''
        return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def wait(self, processes, timeout):
        deadline = time.monotonic() + timeout
        output = {process: deque(maxlen=20) for process in processes}
        readers = []

        def drain(process, stream):
            for line in iter(stream.readline, b''):
                output[process].append(line.decode(errors='replace')[:2000])
            stream.close()

        for process in processes:
            for stream in (process.stdout, process.stderr):
                if stream:
                    reader = threading.Thread(target=drain, args=(process, stream), daemon=True)
                    reader.start()
                    readers.append(reader)
        try:
            while any(process.poll() is None for process in processes):
                if self.cancelled():
                    raise InterruptedError('Image preparation stopped')
                if time.monotonic() >= deadline:
                    raise TimeoutError('Image preparation timed out')
                time.sleep(0.2)
            for reader in readers:
                reader.join(timeout=5)
            for process in processes:
                if process.returncode:
                    raise RuntimeError(''.join(output[process])[-2000:] or 'Image command failed')
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                if process.poll() is None:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()

    def local_command(self, command, input_text=None):
        # Pull/build output is drained continuously; Docker progress cannot fill a pipe.
        process = subprocess.Popen(command, stdin=subprocess.PIPE if input_text else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if input_text:
            process.stdin.write(input_text.encode())
            process.stdin.close()
            process.stdin = None
        self.wait([process], 1800)

    def transfer(self, source, destination, image, expected):
        if self.cancelled():
            raise InterruptedError('Image distribution stopped')
        source_name = source['host'] if source else 'compai.local'
        target_name = destination['host'] if destination else 'compai.local'
        self.notify(image, 'distributing', source_name, target_name)
        # The tar stream travels through OS pipes only. No archive is written to disk.
        export = 'docker image save ' + shlex.quote(image) + ' | gzip -1'
        load = 'gzip -d | docker image load --quiet'
        producer = subprocess.Popen(self.command(source, ['bash', '-o', 'pipefail', '-c', export]),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        consumer = None
        try:
            consumer = subprocess.Popen(self.command(destination, ['bash', '-o', 'pipefail', '-c', load]),
                                        stdin=producer.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            producer.stdout.close()
            producer.stdout = None
            self.wait([producer, consumer], 1800)
            if self.inspect(destination, image) != expected and self.fingerprint(source, image) != self.fingerprint(destination, image):
                raise RuntimeError(f'Image ID verification failed on {target_name}: {image}')
        finally:
            for process in (producer, consumer):
                if process and process.poll() is None:
                    process.kill()
                    process.wait()

    def ensure(self, image, destinations):
        if self.cancelled():
            raise InterruptedError('Image preparation stopped')
        self.notify(image, 'locating', None, None)
        source, expected = None, self.inspect(None, image)
        if not expected:
            for host in self.hosts:
                expected = self.inspect(host, image)
                if expected:
                    source = host
                    break
        if not expected:
            if image == TOOLCHAIN:
                for base in (NODE, PYTHON):
                    self.ensure(base, [None])
                self.notify(image, 'building', 'compai.local', None)
                self.local_command(['docker', 'build', '--pull=false', '--network=none', '-t', image, '-'], TOOLCHAIN_DOCKERFILE)
            elif image == 'organization-launcher:webrtc-1':
                from pathlib import Path
                self.notify(image, 'building', 'compai.local', None)
                dockerfile = Path(__file__).resolve().parents[1] / 'Dockerfile.launcher'
                self.local_command(['docker', 'build', '-t', image, '-'], dockerfile.read_text())
            else:
                self.notify(image, 'pulling', 'compai.local', None)
                self.local_command(['docker', 'image', 'pull', image])
            expected = self.inspect(None, image)
            if not expected:
                raise RuntimeError('Image preparation did not produce a complete image: ' + image)
        self.notify(image, 'cached', source['host'] if source else 'compai.local', None)
        for host in destinations:
            actual = self.inspect(host, image)
            if actual != expected and (not actual or self.fingerprint(source, image) != self.fingerprint(host, image)):
                self.transfer(source, host, image, expected)
            self.host_images.setdefault(host['host'] if host else 'local', {})[image] = self.inspect(host, image)
        self.images[image] = expected
        self.notify(image, 'ready', source['host'] if source else 'compai.local', None)
        return expected

    def prepare(self):
        images = list(dict.fromkeys(image for host in self.hosts for image in required_images(host)))
        for image in images:
            self.ensure(image, [host for host in self.hosts if image in required_images(host)])
        return dict(self.images)
