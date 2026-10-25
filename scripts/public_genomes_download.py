"""Download a bounded, versioned public comparison set and retain source receipts."""
from concurrent.futures import ThreadPoolExecutor
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import urllib.request

MAX_FILE=512*1024*1024
CATALOG=[
 ('Watson','GRCh37','https://hgdownload.soe.ucsc.edu/goldenPath/hg19/database/pgWatson.txt.gz','https://www.ncbi.nlm.nih.gov/bioproject/28335'),
 ('Venter','GRCh37','https://hgdownload.soe.ucsc.edu/goldenPath/hg19/database/pgVenter.txt.gz','https://genome.ucsc.edu/cgi-bin/hgTrackUi?db=hg19&g=pgVenter')]
for sample,subdir in [('HG001','NA12878_HG001'),('HG002','AshkenazimTrio/HG002_NA24385_son'),('HG005','ChineseTrio/HG005_NA24631_son')]:
 CATALOG.append((sample,'GRCh38',f'https://ftp-trace.ncbi.nlm.nih.gov/ReferenceSamples/giab/release/{subdir}/NISTv4.2.1/GRCh38/{sample}_GRCh38_1_22_v4.2.1_benchmark.vcf.gz','https://github.com/genome-in-a-bottle/giab_latest_release'))
CATALOG.extend([
 ('GRCh37-GFF','GRCh37','https://ftp.ensembl.org/pub/grch37/release-115/gff3/homo_sapiens/Homo_sapiens.GRCh37.87.chr.gff3.gz','https://www.ensembl.org/info/genome/genebuild/genome_annotation.html'),
 ('GRCh38-GFF','GRCh38','https://ftp.ensembl.org/pub/release-115/gff3/homo_sapiens/Homo_sapiens.GRCh38.115.chr.gff3.gz','https://www.ensembl.org/info/genome/genebuild/genome_annotation.html')])


def download(root,item):
    sample,assembly,url,documentation=item
    folder=Path(root)/sample;folder.mkdir(exist_ok=True,mode=0o700);path=folder/url.rsplit('/',1)[1]
    if not path.exists():
        with urllib.request.urlopen(url,timeout=60)as response:
            expected=int(response.headers.get('Content-Length',0))
            if expected>MAX_FILE:raise ValueError('Unexpected public dataset size')
            part=path.with_suffix(path.suffix+'.part');size=0
            with part.open('wb')as stream:
                while chunk:=response.read(1024*1024):
                    size+=len(chunk)
                    if size>MAX_FILE:raise ValueError('Public dataset exceeds download limit')
                    stream.write(chunk)
            if expected and size!=expected:raise ValueError('Incomplete download')
            part.replace(path)
    digest=hashlib.sha256();size=0
    with path.open('rb')as stream:
        while chunk:=stream.read(1024*1024):size+=len(chunk);digest.update(chunk)
    prior=folder/'source.json'
    if prior.exists():
        previous=json.loads(prior.read_text())
        if previous['url']!=url or previous['sha256']!=digest.hexdigest():raise ValueError('Cached source differs from download receipt')
    value={'sample':sample,'assembly':assembly,'url':url,'documentation':documentation,'path':str(path),'bytes':size,'sha256':digest.hexdigest(),
           'downloaded_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
           'checksum_scope':'Local SHA-256 receipt plus HTTPS/length checks; not a publisher-signed checksum',
           'format':'UCSC pgSnp observed sites; genotype/depth/quality not reconstructed' if sample in ['Watson','Venter'] else 'Ensembl GFF3' if sample.endswith('GFF') else 'GIAB v4.2.1 autosomal high-confidence benchmark variants'}
    prior.write_text(json.dumps(value,indent=2));return value


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();os.umask(0o077)
    root=args.output.absolute()
    if any((p/'.git').exists()for p in [root,*root.parents]):raise ValueError('Download datasets outside Git')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with ThreadPoolExecutor(max_workers=3)as pool:results=list(pool.map(lambda item:download(root,item),CATALOG))
    (root/'catalog.json').write_text(json.dumps(results,indent=2));print('Public datasets ready: '+str(root))


if __name__=='__main__':main()
