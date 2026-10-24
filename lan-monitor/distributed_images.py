"""Workspace suite definitions on top of the shared LAN-first Docker module."""
from pathlib import Path
import sys
import shlex
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from docker_utils.images import ImageResolver
from docker_utils.policy import prepare_build
from distributed_worker import NODE,PYTHON,SUITES

TOOLCHAIN='orgportal-distributed-python-node:1'


def required_images(host):return list(dict.fromkeys(SUITES[suite]['image'] for suite in host['suites']))


class ImageDistributor(ImageResolver):
    def __init__(self,hosts,ssh,cancelled,notify):
        self.refreshed=set()
        self.failures={}
        super().__init__(hosts,ssh,cancelled,notify,{
            TOOLCHAIN:self.toolchain,
            'organization-launcher:webrtc-1':self.launcher,
        })

    def ensure(self,image,destinations,*,isolate_hosts=False):
        # A run refreshes each required registry reference once, then shares its
        # verified contents over the LAN. Pull failures never silently use stale tags.
        if image not in self.refreshed:
            if self.cancelled():raise InterruptedError('Image preparation stopped')
            if image in self.builders:
                self.notify(image,'building','compai.local',None)
                self.builders[image](self)
            else:
                self.notify(image,'pulling','compai.local',None)
                self.local_command(['docker','image','pull',image])
            if not self.inspect(None,image) or not self.compatible(None,image):
                raise RuntimeError('Image refresh did not produce a native-platform image: '+image)
            self.refreshed.add(image)
        def distribute(host):
            try:return super(ImageDistributor,self).ensure(image,[host])
            except InterruptedError:raise
            except Exception as error:
                if not isolate_hosts or host is None:raise
                self.failures.setdefault(host['host'],{'stage':'image_preparation','reason':type(error).__name__+': '+str(error)[:600],'image':image})
                self.notify(image,'failed',None,host['host'])
                return None
        if len(destinations)<2:
            for host in destinations:distribute(host)
        else:
            with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(distribute,destinations))
        return self.images.get(image) or self.inspect(None,image)

    def inspect(self,host,reference):
        if host and host.get('agent_endpoint'):
            from agent_client import AgentClient
            value=AgentClient(host).call('image.inspect',{'image':reference})
            return value['id'] if value else None
        return super().inspect(host,reference)

    def compatible(self,host,reference):
        if host and host.get('agent_endpoint'):
            import json,subprocess
            from agent_client import AgentClient
            value=AgentClient(host).call('image.inspect',{'image':reference})
            if not value:return False
            local=json.loads(subprocess.check_output(['docker','image','inspect',reference],text=True,timeout=25))[0]
            return (local['Os'],local['Architecture'])==(value['os'],value['architecture'])
        return super().compatible(host,reference)

    def fingerprint(self,host,reference):
        if host and host.get('agent_endpoint'):
            from agent_client import AgentClient
            value=AgentClient(host).call('image.inspect',{'image':reference})
            return value['fingerprint'] if value else None
        return super().fingerprint(host,reference)

    def transfer(self,source,destination,image,expected):
        if destination and destination.get('agent_endpoint'):
            import subprocess,tempfile
            from agent_client import AgentClient
            if source is not None:raise RuntimeError('Agent image delivery requires a coordinator-prepared image')
            self.notify(image,'distributing','coordinator',destination['host'])
            with tempfile.TemporaryDirectory(prefix='launcher-image-') as folder:
                archive=Path(folder)/'image.tar'
                subprocess.run(['docker','image','save','-o',str(archive),image],check=True,capture_output=True,timeout=600)
                client=AgentClient(destination)
                upload=client.upload(archive,self.cancelled)
                result=client.call('image.import',{'upload':upload,'image':image,'fingerprint':self.fingerprint(None,image)},timeout=600)
                if not result.get('id'):raise RuntimeError('Agent did not report its verified local image ID')
            return
        if destination is not None:raise RuntimeError('Image destination has no authenticated agent endpoint')
        return super().transfer(source,destination,image,expected)

    def toolchain(self,service):
        for base in (NODE,PYTHON):self.ensure(base,[None])
        node=self.host_images['local'][NODE]
        python=self.host_images['local'][PYTHON]
        dockerfile=f'FROM {node} AS node\nFROM {python}\nCOPY --from=node /usr/local/bin/node /usr/local/bin/node\n'
        self.local_command(['docker','build','--pull=false','--network=none','-t',TOOLCHAIN,'-'],dockerfile)

    def launcher(self,service):
        dockerfile=Path(__file__).resolve().parents[1]/'Dockerfile.launcher'
        text=dockerfile.read_text()
        for line in text.splitlines():
            tokens=shlex.split(line,comments=True)
            if tokens and tokens[0].upper()=='FROM':self.ensure(tokens[1],[None])
        text=prepare_build(text)
        self.local_command(['docker','build','--pull=false','-t','organization-launcher:webrtc-1','-'],text)

    def prepare(self):
        images=list(dict.fromkeys(image for host in self.hosts for image in required_images(host)))
        for image in images:
            destinations=[host for host in self.hosts if host['host'] not in self.failures and image in required_images(host)]
            if not destinations:continue
            try:self.ensure(image,destinations,isolate_hosts=True)
            except InterruptedError:raise
            except Exception as error:
                for host in destinations:
                    self.failures.setdefault(host['host'],{'stage':'image_preparation','reason':type(error).__name__+': '+str(error)[:600],'image':image})
                    self.notify(image,'failed',None,host['host'])
        return dict(self.images)
