#!/usr/bin/env python3
"""Synchronized paired FASTQ shards with atomic receipts for streaming mappers.

This worker accepts complete gzip sources. Paired shards become available as
soon as each pair of shard files verifies; a source completion receipt remains
mandatory before final publication. No read names or sequences enter receipts.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path


def record(stream):
    header=stream.readline()
    if not header:return None
    seq=stream.readline();plus=stream.readline();quality=stream.readline()
    if not header.startswith(b'@') or not plus.startswith(b'+') or not seq or len(seq.rstrip())!=len(quality.rstrip()):raise ValueError('Malformed paired FASTQ')
    return header,seq,plus,quality


def pair_name(header):
    name=header.split()[0]
    return name[:-2] if name.endswith((b'/1',b'/2')) else name


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(8388608),b''):h.update(block)
    return h.hexdigest()


def split_opened(one,two,folder,records_per_shard=500000):
    if records_per_shard<1:raise ValueError('Invalid shard size')
    folder=Path(folder);folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    count=0;number=0;receipts=[]
    while True:
        existing=folder/f'{number:06d}.json'
        if existing.exists():
            prior=json.loads(existing.read_text())
            for f in prior['files']:
                if sha(Path(f['path']))!=f['sha256']:raise ValueError('Completed paired shard changed')
            for _ in range(prior['pairs']):
                a,b=record(one),record(two)
                if a is None or b is None or pair_name(a[0])!=pair_name(b[0]):raise ValueError('Resumed paired source differs')
            count+=prior['pairs'];number+=1
            continue
        paths=[folder/f'{number:06d}_{mate}.fastq.gz' for mate in (1,2)]
        partial=[p.with_suffix(p.suffix+'.partial') for p in paths]
        shard_count=0
        for stale in partial:
            if stale.exists():stale.unlink()
        with gzip.open(partial[0],'xb',compresslevel=1) as left,gzip.open(partial[1],'xb',compresslevel=1) as right:
            while shard_count<records_per_shard:
                a,b=record(one),record(two)
                if (a is None)!=(b is None):raise ValueError('Paired FASTQs have unequal record counts')
                if a is None:break
                if pair_name(a[0])!=pair_name(b[0]):raise ValueError('Paired FASTQ identifiers differ')
                left.write(b''.join(a));right.write(b''.join(b));shard_count+=1
        if not shard_count:
            for p in partial:p.unlink()
            break
        files=[]
        for tmp,path in zip(partial,paths):
            tmp.chmod(0o600);tmp.replace(path);files.append({'path':str(path),'bytes':path.stat().st_size,'sha256':sha(path)})
        receipt={'shard':number,'pairs':shard_count,'files':files,'phase':'verified_pair_shard','publication':False}
        temp=folder/f'{number:06d}.json.tmp';temp.write_text(json.dumps(receipt));temp.chmod(0o600);temp.replace(folder/f'{number:06d}.json')
        receipts.append(receipt);count+=shard_count;number+=1
        if shard_count<records_per_shard:break
    result={'pairs':count,'shards':number,'phase':'paired_sources_complete','publication':False}
    (folder/'complete.json').write_text(json.dumps(result));(folder/'complete.json').chmod(0o600)
    return result


def split(first,second,folder,records_per_shard=500000):
    with gzip.open(first,'rb') as one,gzip.open(second,'rb') as two:
        return split_opened(one,two,folder,records_per_shard)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--first',required=True);parser.add_argument('--second',required=True);parser.add_argument('--output',required=True);parser.add_argument('--pairs',type=int,default=500000);args=parser.parse_args();split(args.first,args.second,args.output,args.pairs)
