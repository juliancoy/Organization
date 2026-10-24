import io
import json
import unittest
from unittest.mock import Mock,patch
from distributed_images import ImageDistributor


class ImageStreamingTests(unittest.TestCase):
    def service(self):
        hosts=[{'host':'a.local'},{'host':'b.local'}]
        return ImageDistributor(hosts,lambda host:['ssh',host['host']],lambda:False,Mock()),hosts

    def test_matching_cached_images_never_copy_pull_or_build(self):
        service,hosts=self.service()
        with patch.object(service,'inspect',return_value='sha256:same'),patch.object(service,'transfer') as transfer,patch.object(service,'local_command') as download:
            self.assertEqual(service.ensure('browser',hosts),'sha256:same')
        transfer.assert_not_called();download.assert_not_called()

    def test_identical_content_across_storage_backends_reuses_local_ids(self):
        service,hosts=self.service()
        def inspect(host,image):return 'sha256:source' if host is None else 'sha256:'+host['host']
        with patch.object(service,'inspect',side_effect=inspect),patch.object(service,'fingerprint',return_value='same-content'),patch.object(service,'transfer') as transfer:
            service.ensure('browser',hosts)
        transfer.assert_not_called()
        self.assertEqual(service.host_images['b.local']['browser'],'sha256:b.local')

    def test_fingerprint_normalizes_optional_docker_metadata(self):
        service,hosts=self.service()
        metadata={'RootFS':{'Layers':['sha256:layer']},'Config':{'Cmd':['run']},'Architecture':'amd64','Os':'linux','Created':'2026-01-01'}
        other={**metadata,'Config':{'Cmd':['run'],'WorkingDir':'','OnBuild':None,'Volumes':None,'Entrypoint':None}}
        results=[Mock(returncode=0,stdout=json.dumps([value])) for value in (metadata,other)]
        with patch('distributed_images.subprocess.run',side_effect=results):
            self.assertEqual(service.fingerprint(None,'browser'),service.fingerprint(hosts[0],'browser'))

    def test_cached_peer_is_used_before_downloading(self):
        service,hosts=self.service()
        def inspect(host,image):return 'sha256:peer' if host==hosts[0] else None
        with patch.object(service,'inspect',side_effect=inspect),patch.object(service,'transfer') as transfer,patch.object(service,'local_command') as download:
            service.ensure('browser',hosts)
        transfer.assert_called_once_with(hosts[0],hosts[1],'browser','sha256:peer');download.assert_not_called()

    def test_image_transfer_is_pipe_to_pipe_with_no_disk_archive(self):
        service,hosts=self.service()
        producer=Mock(stdout=io.BytesIO(b'image stream'),stderr=io.BytesIO());producer.poll.return_value=0
        stream=producer.stdout
        consumer=Mock(stdout=io.BytesIO(),stderr=io.BytesIO());consumer.poll.return_value=0
        with patch('distributed_images.subprocess.Popen',side_effect=[producer,consumer]) as processes,patch.object(service,'wait'),patch.object(service,'inspect',return_value='sha256:expected'),patch('builtins.open') as opened:
            service.transfer(hosts[0],hosts[1],'browser','sha256:expected')
        first,second=processes.call_args_list
        self.assertIn('docker image save browser | gzip -1',first.args[0][-1])
        self.assertIn('gzip -d | docker image load --quiet',second.args[0][-1])
        self.assertIs(second.kwargs['stdin'],stream)
        opened.assert_not_called()


if __name__=='__main__':unittest.main()
