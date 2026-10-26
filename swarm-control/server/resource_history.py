"""Bounded in-memory resource samples; no credentials or process/file details."""
from collections import deque
from copy import deepcopy

WINDOW_SECONDS = 300
MAX_SAMPLES = 61
HOST_FIELDS = 'host observed_hostname addresses status sampled_at window_seconds percent cpus ram_percent ram_used_bytes ram_total_bytes disk_busy_percent disk_basis network_mbps network_tx_mbps network_rx_mbps cpu_iowait_percent cpu_steal_percent cpu_pressure_some_percent'
GPU_FIELDS = 'pci_id vendor name backend utilization_percent memory_total_mib memory_free_mib'


def project_host(row):
    host = {key: deepcopy(row[key]) for key in HOST_FIELDS.split() if key in row}
    hardware = row.get('hardware') or {}
    host['hardware'] = {key: hardware[key] for key in ('stale',) if key in hardware}
    host['hardware']['gpus'] = [{key: gpu[key] for key in GPU_FIELDS.split() if key in gpu}
                                for gpu in hardware.get('gpus', [])[:16]]
    return host


class ResourceHistory:
    def __init__(self):
        self.samples = deque(maxlen=MAX_SAMPLES)

    def append(self, hosts, now):
        self.samples.append({'sampled_at': now, 'hosts': [project_host(host) for host in hosts[:64]]})
        self.prune(now)

    def prune(self, now):
        while self.samples and self.samples[0]['sampled_at'] < now - WINDOW_SECONDS:
            self.samples.popleft()

    def view(self, now):
        self.prune(now)
        return deepcopy(list(self.samples))
