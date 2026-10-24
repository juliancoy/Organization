"""Bounded, read-only projection of the local coordinator for paired owners."""
import json
from urllib.request import Request, urlopen


def snapshot():
    def read(path):
        # Fixed loopback source; callers cannot supply destinations or credentials.
        with urlopen(Request('http://127.0.0.1:8089' + path), timeout=4) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError('Coordinator snapshot exceeded the size limit')
        return json.loads(raw)
    network, tests = read('/api/devices'), read('/api/distributed-tests')
    if not isinstance(network, dict) or not isinstance(tests, dict):
        raise ValueError('Invalid coordinator snapshot')
    run = tests.get('run') or {}
    interfaces = []
    for name, traffic in list(network.get('interface_traffic', {}).items())[:16]:
        link = network.get('links', {}).get(name, {})
        interfaces.append({'name': name, 'rx_mbps': traffic.get('rx_mbps'),
                           'tx_mbps': traffic.get('tx_mbps'), 'updated': traffic.get('updated'),
                           'capacity_mbps': link.get('capacity_mbps')})
    return {
        'network': {'interface': network.get('interface'), 'subnet': network.get('subnet'),
                    'gateway': network.get('gateway'), 'interfaces': interfaces},
        'updated': network.get('updated'), 'scanning': bool(network.get('scanning')),
        'devices': [{'hostname': device.get('hostname') or device.get('ip'),
                     'address': device.get('ip'), 'reachable': device.get('ping') is True,
                     'coordinator': bool(device.get('self'))}
                    for device in network.get('devices', [])[:256]],
        'run': {'id': run.get('id'), 'status': run.get('status', 'No run recorded'),
                'started': run.get('started'), 'finished': run.get('finished'),
                'hosts': [{'host': host.get('host'), 'status': host.get('status'),
                           'jobs': [{'name': job.get('name') or key, 'status': job.get('status')}
                                    for key, job in list(host.get('jobs', {}).items())[:100]]}
                          for host in run.get('hosts', [])[:64]]}}
