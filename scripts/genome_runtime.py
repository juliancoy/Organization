"""Genome application adapter to the common swarm-node execution runtime.

Only application group and labels live here. Budgets, tool commands and evidence
remain with the individual genome workflows.
"""
from scripts import swarm_runtime
from scripts.swarm_runtime import AgentClient, cleanup_container, atomic_json, stamp
from scripts.swarm_runtime import copy_verified_file as copy_receipt

GROUP='genomics-wgs-recall'


def leased_run(host,image,command,mounts,work,cpus,memory_mb,report,wait_for_capacity=False,on_admitted=None,tmpfs=None):
    # Preserve existing callers and legacy agent names during an incremental
    # migration; resource accounting and container lifecycle are runtime-owned.
    try:
        return swarm_runtime.run_job(host,image,command,mounts,work,cpus,memory_mb,report,
            group=GROUP,labels={'org.lifetech.genomics':'true'},container_prefix='genomics',
            wait_for_capacity=wait_for_capacity,on_admitted=on_admitted,tmpfs=tmpfs)
    except swarm_runtime.CapacityWait as error:
        # A controller loaded before this extraction may still hold the old
        # exception class. Preserve its catch boundary until its next restart.
        from scripts.genomics_distributed import CapacityWait
        if CapacityWait is swarm_runtime.CapacityWait:raise
        raise CapacityWait(str(error)) from error
