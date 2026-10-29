"""Recruit public HuRef DAT1 candidate reads directly from a downloaded FASTQ.

Sequence matches recruit candidates, not genotypes. Preserve source identity,
record structure, read qualities and a reusable compressed-stream index.
"""
import json
import os
from pathlib import Path
import time
import concurrent.futures


def extract_candidate(data,hit_end,run):
    """A motif may occur in a quality line; accept only a complete sequence row."""
    begin=data.rfind(b'\n',0,hit_end+1)+1
    if begin==0:return None
    header_begin=data.rfind(b'\n',0,begin-1)+1
    if not data[header_begin:begin].startswith(('@'+run+'.').encode()):return None
    end=data.find(b'\n',begin)
    plus_end=data.find(b'\n',end+1) if end>=0 else -1
    quality_end=data.find(b'\n',plus_end+1) if plus_end>=0 else -1
    if min(end,plus_end,quality_end)<0:return None
    header=data[header_begin:begin-1].rstrip(b'\r')
    sequence=data[begin:end].rstrip(b'\r')
    plus=data[end+1:plus_end].rstrip(b'\r')
    quality=data[plus_end+1:quality_end].rstrip(b'\r')
    if not plus.startswith(b'+') or len(sequence)!=len(quality):
        raise ValueError('A recruited source record has invalid FASTQ structure')
    if not sequence or any(c not in b'ACGTN' for c in sequence):raise ValueError('Invalid recruited DNA alphabet')
    if any(c<33 or c>126 for c in quality):raise ValueError('Invalid recruited Phred+33 quality')
    return header.split()[0],b'\n'.join([header,sequence,plus,quality])+b'\n'


_MATCHER=None
_RUN=None

def search_block(data):
    found={}
    for hit,_ in _MATCHER.iter(data.decode('latin1')):
        value=extract_candidate(data,hit,_RUN)
        if value is not None:found[value[0]]=value[1]
    return list(found.items())


def main():
    import ahocorasick
    import rapidgzip
    os.umask(0o077)
    folder=Path('/work');job=json.loads((folder/'job.json').read_text())
    automaton=ahocorasick.Automaton()
    sequence=job['reference_context'];complement=str.maketrans('ACGT','TGCA')
    seeds=set()
    for i in range(0,len(sequence)-30,15):
        seed=sequence[i:i+31]
        if 'N' not in seed:seeds.update([seed,seed.translate(complement)[::-1]])
    for seed in seeds:automaton.add_word(seed,True)
    automaton.make_automaton()
    global _MATCHER,_RUN
    _MATCHER=automaton;_RUN=job['run']
    state={'status':'scanning','run':job['run'],'source_receipt_sha256':job['source_sha256'],'source_bytes':job['source_bytes'],
           'uncompressed_bytes_scanned':0,'candidate_reads':0,'source_fully_scanned':False,'genotype':'not_assessed','absence_confirmed':False}
    def save():
        state['updated']=time.time();p=folder/'scan.next.json';p.write_text(json.dumps(state,indent=2)+'\n');p.replace(folder/'scan.json')
    save();seen=set();carry=b'';last_save=0
    try:
        with rapidgzip.open('/input/reads.fastq.gz',parallelization=3) as stream,(folder/'candidates.fastq').open('wb') as output,concurrent.futures.ProcessPoolExecutor(max_workers=4) as pool:
            # A previous scan of this exact source may have completed a no-hit
            # prefix. Re-decode that prefix, then resume matching with overlap.
            skip=max(0,int(job.get('verified_no_hit_prefix_bytes',0))-2048)
            remaining=skip
            while remaining:
                block=stream.read(min(16*1024*1024,remaining))
                if not block:raise ValueError('Resume prefix exceeds source')
                remaining-=len(block)
            state['uncompressed_bytes_scanned']=skip
            pending=[]
            def consume(future,size):
                nonlocal last_save
                for name,record in future.result():
                    if name in seen:continue
                    if len(seen)>=100000:raise ValueError('Recruitment exceeded candidate bound')
                    seen.add(name);output.write(record)
                state['uncompressed_bytes_scanned']+=size;state['candidate_reads']=len(seen)
                if time.monotonic()-last_save>10:
                    output.flush();save();last_save=time.monotonic()
            while True:
                block=stream.read(16*1024*1024)
                if not block:break
                data=carry+block
                pending.append((pool.submit(search_block,data),len(block)))
                carry=data[-1024:]
                if len(pending)>=8:consume(*pending.pop(0))
            for item in pending:consume(*item)
            stream.export_index(str(folder/'source.gzindex'))
        state.update(status='source_scan_completed',source_fully_scanned=True,candidate_reads=len(seen))
        save()
    except BaseException as error:
        state.update(status='failed',error=type(error).__name__+': '+str(error));save();raise


if __name__=='__main__':main()
