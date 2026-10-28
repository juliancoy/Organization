from scripts.wgs_finalize import merge_resources
from scripts.wgs_recall import merge_commands


def test_merge_queues_a_supported_minimum_and_uses_available_resources_when_free():
    assert merge_resources({'available_mb':0,'available_cpus':0},8)==(2,1024)
    assert merge_resources({'available_mb':16000,'available_cpus':6},8)==(5,8000)
    cpus,memory=merge_resources({'available_mb':0,'available_cpus':0},8)
    command=merge_commands([], '/work/result.bam',cpus,memory,list_file='/work/bams.list')
    assert '-m 256M' in command and 'samtools markdup' in command


def test_finalization_uses_verified_gathered_copy_when_original_host_is_gone():
    import pytest
    from scripts.wgs_finalize import bam_location
    original={'receipt':{'host':'offline','bam':'/original.bam'}}
    collected={'run-1':{'host':'ssd','path':'/copied.bam','run':'run-1','sha256':'a'*64}}
    assert bam_location('run-1',original,collected)['host']=='ssd'
    assert bam_location('run-1',original,{})['host']=='offline'
    with pytest.raises(ValueError):bam_location('run-1',original,{'run-1':{'path':'unverified'}})
