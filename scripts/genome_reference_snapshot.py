"""Apply verified swarm aggregates to the public genome snapshot; never export sites."""
import argparse
from copy import deepcopy
from datetime import datetime,timezone
import json
from pathlib import Path


def apply_reference_matches(snapshot,summaries):
    result=deepcopy(snapshot)
    for sample in result['samples']:
        name=sample['name'];c=sample['genome_composition'];summary=summaries.get(name)
        if summary is None:
            continue
        if name=='Julian':
            same=sum(summary['counts'].get('confirmed_reference',{}).values())
            unscored=sum(summary['counts'].get('reference_quality_unknown',{}).values())
            description=(f'{same:,} positions meet PASS, fully reference genotype, GQ ≥20 and minimum depth ≥20. '
                         f'{unscored:,} additional reference-called positions lack sufficient quality metadata and remain unverified. '
                         'Reference assembly and primary chromosome lengths match GRCh38; no match is inferred from coverage alone.')
            source_url=None
        else:
            if summary.get('prepared_records',summary['records'])!=sample['records']:
                raise ValueError('Benchmark variant count differs from chart snapshot')
            same=summary['same'];unscored=None
            description=(f'{same:,} high-confidence reference-matching positions inside official GIAB v4.2.1 benchmark regions, '
                         'after excluding the union of official and normalized chart variant REF spans, including deletion spans and insertion anchors. '
                         'Outside those benchmark regions remains unverified. This benchmark covers small variants, not all structural variation.')
            source_url=summary['benchmark_source']['url']
        if type(same) is not int or same<0:
            raise ValueError('Invalid reference-match count')
        other=sum(value for key,value in c['counts'].items() if key not in ('same','unknown'))
        unknown=c['reference_positions']-other-same
        if unknown<0:
            raise ValueError('Reference matches overlap variant partition or exceed denominator')
        c['counts']['same']=same;c['counts']['unknown']=unknown;c['same']=same
        c['scope']='Primary reference positions: variant starts, separately verified reference-match positions, and an unverified remainder. Insertions use reference anchors; full variant REF spans are excluded from matches.'
        c['reference_evidence']={'summary':description,'source_url':source_url,
                                 'unscored_reference_positions':unscored,
                                 'worker_count':len(summary['workers']),'batches':summary['batches']}
        assert sum(c['counts'].values())==c['reference_positions']
    result['reference_analysis_at']=datetime.now(timezone.utc).isoformat()
    return result


def load_summaries(run):
    names=('Julian','HG001','HG002','HG005')
    return {name:json.loads((run/(name+'-reference-summary.json')).read_text()) for name in names}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--snapshot',type=Path,required=True)
    args=parser.parse_args()
    summaries=load_summaries(args.run)
    data=json.loads(args.snapshot.read_text().removeprefix('export default ').strip().removesuffix(';'))
    data=apply_reference_matches(data,summaries)
    args.snapshot.write_text('export default '+json.dumps(data,separators=(',',':'))+';\n')
    for s in data['samples']:
        print(s['name'],s['genome_composition'].get('same'))


if __name__=='__main__':main()
