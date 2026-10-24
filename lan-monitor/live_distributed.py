"""Load mounted coordinator code for each new run without restarting the API."""
import importlib
import sys
import threading


class LiveDistributedTests:
    def __init__(self):
        self.lock = threading.RLock()
        self.runner = self.load()

    @staticmethod
    def load():
        importlib.invalidate_caches()
        for name in ('distributed','run_activity','distributed_worker','suite_cache','github_sources','distributed_images','image_registry','agent_client','p2p_network','docker_utils','docker_utils.images','docker_utils.policy','docker_utils.sdk'):
            sys.modules.pop(name,None)
        return importlib.import_module('distributed').DistributedTests()

    def view(self, ident=None):
        with self.lock:
            return self.runner.view(ident)

    def start(self):
        with self.lock:
            if self.runner.running:
                return False
            self.runner = self.load()
            return self.runner.start()

    def stop(self):
        with self.lock:
            return self.runner.stop()
