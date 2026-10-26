"""Validate a verified FASTQ and export a bounded sequence-only benchmark batch."""
import gzip, hashlib, json, pathlib, sys
source=pathlib.Path(sys.argv[1]);expected=sys.argv[2];limit=int(sys.argv[3]);h=hashlib.sha256()
with source.open('rb') as stream:
    for block in iter(lambda:stream.read(1048576),b''):h.update(block)
if h.hexdigest()!=expected:raise ValueError('Verified source SHA256 changed')
records=0;size=0;target=pathlib.Path('bases.bin')
with gzip.open(source,'rb') as stream,target.open('wb') as output:
    while size<limit:
        lines=[stream.readline() for _ in range(4)]
        if not lines[0]:break
        sequence=lines[1].rstrip(b'\r\n');quality=lines[3].rstrip(b'\r\n')
        if not lines[0].startswith(b'@') or not lines[2].startswith(b'+') or len(sequence)!=len(quality):raise ValueError('Malformed FASTQ')
        if size+len(sequence)>limit:break
        output.write(sequence);size+=len(sequence);records+=1
if not records:raise ValueError('No complete reads fit in batch')
target.chmod(0o600)
pathlib.Path('batch.json').write_text(json.dumps({'source_sha256':h.hexdigest(),'records':records,'bases':size,'prefix_batch':True}))
