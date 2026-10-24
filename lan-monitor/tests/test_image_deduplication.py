import concurrent.futures
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
from distributed_images import ImageDistributor


class ImageDeduplicationTests(unittest.TestCase):
    def test_concurrent_requests_refresh_an_image_only_once(self):
        service=ImageDistributor([],Mock(),lambda:False,Mock())
        with patch.object(service,'local_command') as pull,patch.object(service,'inspect',return_value='sha256:prepared'),patch.object(service,'compatible',return_value=True):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _:service.ensure('example/image:1',[]),range(8)))
        pull.assert_called_once_with(['docker','image','pull','example/image:1'])

    def test_registry_bridge_rejects_arbitrary_paths_and_negative_ranges(self):
        path=Path(__file__).resolve().parents[2]/'scripts/launcher_registry.py'
        spec=importlib.util.spec_from_file_location('registry_bridge',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch.object(module.urllib.request,'build_opener') as network:
            for path in ('/v2/_catalog','/etc/passwd','/v2/tests/'+('a'*24)+'/blobs/../../secrets'):
                with self.assertRaises(ValueError):module.read({'path':path})
            with self.assertRaises(ValueError):module.read({'path':'/v2/tests/'+('a'*24)+'/blobs/sha256:'+('b'*64),'offset':-1})
        network.assert_not_called()

    def test_registry_head_reads_no_layer_body(self):
        path=Path(__file__).resolve().parents[2]/'scripts/launcher_registry.py'
        spec=importlib.util.spec_from_file_location('registry_bridge',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        response=Mock(status=200,headers={'Content-Length':'123','Content-Type':'application/octet-stream'})
        with patch.object(module.urllib.request,'build_opener') as factory:
            factory.return_value.open.return_value.__enter__.return_value=response
            result=module.read({'path':'/v2/tests/'+('a'*24)+'/blobs/sha256:'+('b'*64),'head':True})
            request=factory.return_value.open.call_args.args[0]
        self.assertEqual(request.get_method(),'HEAD')
        self.assertEqual(result['total'],123)
        self.assertEqual(result['data'],'')
        response.read.assert_not_called()
