"""DNS-SD discovery on the enrolled LAN; advertisements never grant membership."""
import socket
import threading
from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, Zeroconf

SERVICE = '_lifetech._tcp.local.'


class Discovery:
    def __init__(self, name, peers, port):
        self.name, self.peers = name, dict(peers)
        self.address = peers[name]
        self.lock = threading.Lock()
        self.services = {}
        self.zc = Zeroconf(interfaces=[self.address], ip_version=IPVersion.V4Only)
        self.info = ServiceInfo(SERVICE, name + '.' + SERVICE,
            addresses=[socket.inet_aton(self.address)], port=port,
            properties={'node': name, 'protocol': 'lifetech-launcher.v1', 'agent':'lifetech-agent.v1'},
            server=name.rstrip('.') + '.',
            host_ttl=45, other_ttl=45)
        self.browser = None

    def start(self):
        self.zc.register_service(self.info)
        self.browser = ServiceBrowser(self.zc, SERVICE, listener=self)

    def add_service(self, zc, kind, name):
        self.update_service(zc, kind, name)

    def update_service(self, zc, kind, name):
        with self.lock:
            self.services.pop(name, None)
        info = zc.get_service_info(kind, name, timeout=2000)
        if not info:
            return
        node = info.properties.get(b'node', b'').decode('utf-8', errors='replace')
        if (node == self.name or node not in self.peers or
                info.properties.get(b'protocol') != b'lifetech-launcher.v1' or info.port != 8790):
            return
        # Enrollment constrains the advertised address; DNS-SD alone is untrusted.
        addresses = info.parsed_addresses(IPVersion.V4Only)
        address = next((value for value in addresses if value == self.peers[node]), None)
        if address:
            with self.lock:
                self.services[name] = (node, address, info.port)

    def remove_service(self, zc, kind, name):
        with self.lock:
            self.services.pop(name, None)

    def endpoint(self, node):
        with self.lock:
            return next(((address, port) for peer, address, port in self.services.values()
                         if peer == node), None)

    def close(self):
        if self.browser:
            self.browser.cancel()
        self.zc.unregister_service(self.info)
        self.zc.close()
