"""Publisher verification survives transient download-worker availability failures."""
import hashlib
import re


def verified_sources(catalog,downloads,qc,reconciled=None):
    """Return manifest-bound evidence only; partial reads and provisional QC never qualify."""
    evidence={}
    for item in catalog:
        ident=hashlib.sha256(item['url'].encode()).hexdigest()[:20]
        row=downloads.get(ident,{})
        receipt=row.get('receipt',{})
        if row.get('status')=='verified' and all(receipt.get(key)==item[key] for key in ('url','filename','bytes','md5')):
            evidence[ident]='publisher_receipt';continue
        checked=(reconciled or {}).get(ident,{})
        if (checked.get('verification')=='publisher_md5_rechecked'
            and all(checked.get(key)==item[key] for key in ('url','filename','bytes','md5'))
            and re.fullmatch('[0-9a-f]{64}',str(checked.get('sha256','')))
            and type(checked.get('verified_at')) in (int,float) and checked['verified_at']>0):
            evidence[ident]='publisher_md5_rechecked';continue
        record=qc.get(ident,{})
        if (record.get('phase')=='verified' and record.get('provisional') is False
            and all(record.get(key)==item[key] for key in ('sample','run','filename'))
            and record.get('input_md5')==item['md5']
            and type(item.get('read_count')) is int and item['read_count']>0
            and record.get('reads')==record.get('expected_reads')==item['read_count']
            and record.get('compressed_bytes_read')==record.get('compressed_bytes_total')==item['bytes']):
            # aggregate_qc checks the entire original MD5 and source receipt before
            # writing phase=verified. A later scheduler/readiness failure is not
            # evidence that this already-verified original input became corrupt.
            evidence[ident]='verified_full_source_qc'
    return evidence
