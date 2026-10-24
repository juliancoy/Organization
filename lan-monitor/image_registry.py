"""Layer-aware image delivery over loopback-only, authenticated SSH tunnels."""
import hashlib
import json
import re
import shlex
import subprocess
import threading
import time
import urllib.request

IMAGE='registry:3'
NAME='organization-test-registry'
PORT=18791


class ImageRegistry:
    def __init__(self,service):
        self.service=service
        self.lock=threading.Lock()
        self.ready=False
        self.published={}

    def start(self):
        if self.ready:return
        self.service.notify(IMAGE,'pulling','compai.local',None)
        self.service.local_command(['docker','image','pull',IMAGE])
        image=self.service.inspect(None,IMAGE)
        result=subprocess.run(['docker','inspect',NAME],capture_output=True,text=True,timeout=25)
        if result.returncode==0:
            current=json.loads(result.stdout)[0]
            if (current['Config'].get('Labels') or {}).get('organization.role')!='test-registry':
                raise RuntimeError('Registry container name is already owned by another service')
            bindings=current['HostConfig'].get('PortBindings',{}).get('5000/tcp',[])
            if bindings!=[{'HostIp':'127.0.0.1','HostPort':str(PORT)}]:
                raise RuntimeError('Test registry must listen only on the expected loopback port')
            if current['Image']!=image:
                self.service.local_command(['docker','rm','-f',NAME])
                result=None
            elif not current['State']['Running']:
                self.service.local_command(['docker','start',NAME])
        if result is None or result.returncode!=0:
            self.service.local_command(['docker','run','--pull=never','-d','--name',NAME,
                '--restart','unless-stopped','--label','organization.role=test-registry',
                '--publish',f'127.0.0.1:{PORT}:5000','--memory','256m','--pids-limit','128',
                '--log-opt','max-size=2m','--log-opt','max-file=2',
                '--mount','type=volume,src=organization-test-registry,dst=/var/lib/registry',image])
        for _ in range(60):
            if self.service.cancelled():raise InterruptedError('Registry preparation stopped')
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/v2/',timeout=2) as response:
                    if response.status==200:self.ready=True;return
            except OSError:pass
            time.sleep(.5)
        raise RuntimeError('Loopback image registry did not become ready')

    def publish(self,image,expected):
        with self.lock:
            key=(image,expected)
            if key in self.published:return self.published[key]
            self.start()
            fingerprint=self.service.fingerprint(None,expected)
            if not fingerprint:raise RuntimeError('Image contents unavailable for registry publication')
            repository='tests/'+hashlib.sha256(image.encode()).hexdigest()[:24]
            tag=f'127.0.0.1:{PORT}/{repository}:{fingerprint}'
            self.service.notify(image,'publishing','compai.local',None)
            self.service.local_command(['docker','tag',expected,tag])
            self.service.local_command(['docker','push',tag])
            result=subprocess.run(['docker','image','inspect',tag,'--format','{{json .RepoDigests}}'],
                                  check=True,capture_output=True,text=True,timeout=25)
            digests=json.loads(result.stdout) or []
            digest=next((value.split('@',1)[1] for value in digests if value.startswith(f'127.0.0.1:{PORT}/{repository}@')),None)
            if not digest or not re.fullmatch(r'sha256:[a-f0-9]{64}',digest):
                raise RuntimeError('Registry did not return an immutable image digest')
            value=(repository,digest,fingerprint)
            self.published[key]=value
            return value

    def transfer(self,source,destination,image,expected):
        if source is not None:raise RuntimeError('Test registry requires coordinator-refreshed images')
        repository,digest,fingerprint=self.publish(image,expected)
        tunnel=None
        try:
            if destination is None or destination.get('local'):
                port=PORT
            else:
                # Reserve an available worker loopback port; ExitOnForwardFailure
                # rejects a collision rather than connecting to another service.
                code="import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1]);s.close()"
                result=subprocess.run(self.service.command(destination,['python3','-c',code]),
                                      check=True,capture_output=True,text=True,timeout=25)
                port=int(result.stdout.strip())
                args=self.service.ssh(destination)
                tunnel=subprocess.Popen(args[:-1]+['-N','-o','ExitOnForwardFailure=yes',
                    '-R',f'127.0.0.1:{port}:127.0.0.1:{PORT}',args[-1]],
                    stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
                # A remote HTTP request verifies the reverse tunnel before Docker pulls.
                check="import urllib.request;urllib.request.urlopen('http://127.0.0.1:"+str(port)+"/v2/',timeout=2).close()"
                for _ in range(20):
                    if self.service.cancelled():raise InterruptedError('Registry transfer stopped')
                    if tunnel.poll() is not None:raise RuntimeError('SSH registry forwarding failed')
                    result=subprocess.run(self.service.command(destination,['python3','-c',check]),capture_output=True,timeout=15)
                    if result.returncode==0:break
                    time.sleep(.25)
                else:raise RuntimeError('Worker cannot reach the registry through SSH')
            reference=f'127.0.0.1:{port}/{repository}@{digest}'
            self.service.notify(image,'distributing','compai.local',destination['host'] if destination else 'local')
            process=subprocess.Popen(self.service.command(destination,['docker','pull',reference]),
                                     stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            self.service.wait([process],1800)
            if self.service.fingerprint(destination,reference)!=fingerprint:
                raise RuntimeError('Registry image contents differ from the prepared source: '+image)
            self.service.local_command(self.service.command(destination,['docker','tag',reference,self.service.cache_tag(image)]))
        finally:
            if tunnel:
                tunnel.terminate()
                try:tunnel.wait(timeout=5)
                except subprocess.TimeoutExpired:tunnel.kill();tunnel.wait()
                if tunnel.stderr:tunnel.stderr.close()
