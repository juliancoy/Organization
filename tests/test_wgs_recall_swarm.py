"""Compatibility entry points retain the genome application's execution policy."""
from unittest.mock import patch
import pytest
from scripts import genome_runtime,swarm_runtime,wgs_recall_swarm


def test_existing_genome_entry_point_uses_shared_execution(tmp_path):
    with patch.object(swarm_runtime,'run_job') as run:
        wgs_recall_swarm.leased_run({},'pinned','command',[],str(tmp_path),2,8192,tmp_path/'status.json',tmpfs={'/ram':2048})
    assert run.call_count==1
    assert run.call_args.kwargs['group']==genome_runtime.GROUP
    assert run.call_args.kwargs['labels']=={'org.lifetech.genomics':'true'}
    assert run.call_args.kwargs['container_prefix']=='genomics'
    assert run.call_args.kwargs['tmpfs']=={'/ram':2048}


def test_compatibility_imports_keep_one_transfer_and_cleanup_implementation():
    assert wgs_recall_swarm.copy_receipt is swarm_runtime.copy_verified_file
    assert wgs_recall_swarm.cleanup_container is swarm_runtime.cleanup_container


def test_old_running_controller_keeps_its_capacity_exception(tmp_path,monkeypatch):
    from scripts import genomics_distributed
    class PreviousCapacityWait(RuntimeError):pass
    monkeypatch.setattr(genomics_distributed,'CapacityWait',PreviousCapacityWait)
    with patch.object(swarm_runtime,'run_job',side_effect=swarm_runtime.CapacityWait('capacity wait')):
        with pytest.raises(PreviousCapacityWait,match='capacity wait'):
            genome_runtime.leased_run({},'pinned','command',[],str(tmp_path),2,8192,tmp_path/'status.json')
