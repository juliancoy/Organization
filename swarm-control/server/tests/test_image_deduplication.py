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
        path=Path(__file__).resolve().parents[3]/'scripts/launcher_registry.py'
        spec=importlib.util.spec_from_file_location('registry_bridge',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch.object(module.urllib.request,'build_opener') as network:
            for path in ('/v2/_catalog','/etc/passwd','/v2/tests/'+('a'*24)+'/blobs/../../secrets'):
                with self.assertRaises(ValueError):module.read({'path':path})
            with self.assertRaises(ValueError):module.read({'path':'/v2/tests/'+('a'*24)+'/blobs/sha256:'+('b'*64),'offset':-1})
        network.assert_not_called()

    def test_registry_head_reads_no_layer_body(self):
        path=Path(__file__).resolve().parents[3]/'scripts/launcher_registry.py'
        spec=importlib.util.spec_from_file_location('registry_bridge',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        response=Mock(status=200,headers={'Content-Length':'123','Content-Type':'application/octet-stream'})
        with patch.object(module.urllib.request,'build_opener') as factory:
            factory.return_value.open.return_value=response
            response.__enter__=Mock(return_value=response)
            response.__exit__=Mock(return_value=False)
            result=module.read({'path':'/v2/tests/'+('a'*24)+'/blobs/sha256:'+('b'*64),'head':True})
            request=factory.return_value.open.call_args.args[0]
        self.assertEqual(request.get_method(),'HEAD')
        self.assertEqual(result['total'],123)
        self.assertEqual(result['data'],'')
        response.read.assert_not_called()


class PeerLayerCacheTests(unittest.TestCase):
    def module(self):
        spec=importlib.util.spec_from_file_location('layer_registry',Path(__file__).resolve().parents[3]/'scripts/launcher_registry.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

    def test_only_complete_digest_verified_content_is_shared_and_ranges_work(self):
        import tempfile,hashlib,base64
        module=self.module();data=b'peer layer bytes'*100;digest=hashlib.sha256(data).hexdigest()
        path='/v2/tests/'+('a'*24)+'/blobs/sha256:'+digest
        with tempfile.TemporaryDirectory() as folder:
            with tempfile.NamedTemporaryFile(mode='w+b',dir=folder,delete=False) as stream:
                stream.write(data)
                self.assertIsNone(module.cache_read(folder,path,0,False))
                self.assertTrue(module.cache_publish(folder,stream,path,'application/octet-stream',len(data)))
            with patch.object(module.urllib.request,'build_opener') as network:
                result=module.read({'path':path,'offset':8},folder)
            self.assertEqual(base64.b64decode(result['data']),data[8:]);network.assert_not_called()
            self.assertEqual(module.read({'path':path,'head':True},folder)['data'],'')

    def test_wrong_digest_and_incomplete_objects_cannot_be_seeded(self):
        import tempfile
        module=self.module();path='/v2/tests/'+('a'*24)+'/blobs/sha256:'+('b'*64)
        with tempfile.TemporaryDirectory() as folder:
            with tempfile.NamedTemporaryFile(mode='w+b',dir=folder) as stream:
                stream.write(b'wrong')
                self.assertFalse(module.cache_publish(folder,stream,path,'application/octet-stream',6))
                with self.assertRaises(ValueError):module.cache_publish(folder,stream,path,'application/octet-stream',5)
            self.assertIsNone(module.cache_read(folder,path,0,False))

    def test_mptcp_socket_falls_back_only_for_unsupported_protocol(self):
        import socket,errno
        module=self.module()
        with patch.object(module.socket,'socket',side_effect=[OSError(errno.EPROTONOSUPPORT,'unsupported'),Mock()]) as factory:
            module.transport_socket((socket.AF_INET,socket.SOCK_STREAM,0))
            self.assertEqual(factory.call_args_list[0].args[2],262)
            self.assertEqual(factory.call_args_list[1].args[2],0)
        with patch.object(module.socket,'socket',side_effect=OSError(errno.EMFILE,'socket limit')):
            with self.assertRaises(OSError):module.transport_socket((socket.AF_INET,socket.SOCK_STREAM,0))
