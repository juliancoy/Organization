"""Recent host socket ownership and bounded file/database resource context."""
import ipaddress
from pathlib import Path
import re
import subprocess
import threading
import time
from process_resources import resource_context


def endpoint(value):
    address, port = value.rsplit(':', 1)
    ip = ipaddress.ip_address(address.strip('[]').split('%')[0])
    return str(getattr(ip, 'ipv4_mapped', None) or ip), int(port)


def parse_owners(output):
    rows = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 7 or parts[0] not in ('tcp', 'udp'):
            continue
        try:
            local, lp = endpoint(parts[4]); remote, rp = endpoint(parts[5])
        except ValueError:
            continue
        owners = [{'name': name, 'pid': int(pid)} for name, pid in re.findall(r'\("([^"\n]+)",pid=(\d+)', line)]
        if owners:
            rows[(6 if parts[0] == 'tcp' else 17, local, lp, remote, rp)] = owners[:4]
    return rows


class SocketOwners:
    def __init__(self):
        self.lock = threading.Lock()
        self.rows = {}
        self.sampled_at = 0
        self.resources = {"processes":{}}

    def annotate(self, flows, host_mac):
        with self.lock:
            rows = dict(self.rows); sampled = self.sampled_at
        if time.time()-sampled > 10:
            return
        for flow in flows:
            if flow.get('device_mac') != host_mac:
                continue
            key = tuple(flow.get(k) for k in ('protocol','local_ip','local_port','remote_ip','remote_port'))
            if key in rows:
                flow['recent_processes'] = rows[key]
                flow['process_sampled_at'] = sampled

    def view_context(self):
        with self.lock:
            if time.time()-self.sampled_at>10:
                return {"status":"stale","processes":{}}
            return {**self.resources,"sampled_at":self.sampled_at,"status":self.resources.get("status","live")}

    def worker(self):
        while True:
            try:
                result = subprocess.run(['ss','-Hntup'],capture_output=True,text=True,timeout=3,check=True)
                rows = parse_owners(result.stdout)
            except (OSError, subprocess.SubprocessError):
                rows = {}
            try:
                resources = resource_context(rows)
            except (OSError,ValueError):
                resources = {'processes':{},'status':'unavailable'}
            with self.lock:
                self.rows = rows; self.resources = resources; self.sampled_at = time.time()
            time.sleep(3)
