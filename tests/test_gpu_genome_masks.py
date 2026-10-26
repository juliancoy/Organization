from array import array
import random
import unittest
import json
import tempfile
from pathlib import Path
from scripts.gpu_genome_masks import oracle_words,oracle,tiles,decode,validate,word_counts

NAMES=['Julian','HG001','HG002','HG005','Watson','Venter']
def fixture(length=20,chrom='1'):
    return {'assembly':'GRCh38','chromosome':chrom,'start':0,'end':length,'chromosome_length':248956422 if chrom=='1' else 58617616,'reference_N':[],'source_scope':'synthetic_known_fixture','samples':[{'id':name} for name in NAMES]}
class MaskTests(unittest.TestCase):
    def test_all_unknown_does_not_create_pair_agreement(self):
        m=fixture();r=decode(m,oracle(m));self.assertTrue(all(p['unknown']==20 for p in r['samples']));self.assertTrue(all(p['jointly_known']==0 and p['agreement_fraction'] is None for p in r['pairs']))
    def test_full_deletion_span_excludes_reference_interior(self):
        m=fixture();m['samples'][0].update(confirmed_reference=[[0,20]],variant_ref_spans=[[4,10]],confident_variant_starts=[[4,5]])
        r=decode(m,oracle(m))['samples'][0];self.assertEqual(r['confirmed_reference'],14);self.assertEqual(r['unknown'],6);self.assertEqual(r['variant_span_covered'],6)
    def test_known_variant_status_is_not_allele_concordance(self):
        m=fixture(1);m['samples'][0].update(variant_ref_spans=[[0,1]],confident_variant_starts=[[0,1]]);m['samples'][1]['confirmed_reference']=[[0,1]]
        r=decode(m,oracle(m));self.assertEqual(r['pairs'][0]['jointly_known'],1);self.assertEqual(r['pairs'][0]['reference_status_differ'],1);self.assertEqual(r['samples'][0]['confident_variant_site_starts'],1)
    def test_N_uncertain_and_Watson_withheld_chromosome(self):
        m=fixture(10,'19');m['reference_N']=[[0,2]]
        for s in m['samples']:s['confirmed_reference']=[[0,10]]
        m['samples'][0]['unknown']=[[2,4]];r=decode(m,oracle(m));self.assertEqual(r['samples'][0]['confirmed_reference'],6);self.assertEqual(r['samples'][4]['unknown'],10)
    def test_overlapping_masks_and_tile_edges(self):
        m=fixture(1031);m['samples'][0]['confirmed_reference']=[[0,700],[500,1031]];m['samples'][0]['variant_ref_spans']=[[1023,1026]]
        packed=[w for tile in tiles(m,257) for w in tile];self.assertEqual(len(packed),1031);self.assertEqual(oracle_words(packed),oracle(m));self.assertEqual(decode(m,oracle(m))['samples'][0]['confirmed_reference'],1028)
    def test_randomized_masks_exact_and_partitioned(self):
        rng=random.Random(113)
        for _ in range(20):
            m=fixture(79)
            for sample in m['samples']:
                for key in ('confirmed_reference','variant_ref_spans','unknown','confident_variant_starts'):
                    sample[key]=[sorted(rng.sample(range(80),2)) for _ in range(4)]
            expected=oracle(m);self.assertEqual(expected,oracle_words(w for tile in tiles(m,13) for w in tile));decode(m,expected)
    def test_noncanonical_and_assembly_mix_rejected(self):
        m=fixture();m['chromosome']='GL000220.1'
        with self.assertRaises(ValueError):validate(m)
        m=fixture();m['assembly']='GRCh37'
        with self.assertRaises(ValueError):validate(m)

    def test_cpu_only_result_is_usable_without_gpu_and_partitions(self):
        from scripts.gpu_genome_masks import run_cpu
        m=fixture(13);m['samples'][0]['confirmed_reference']=[[0,5]]
        with tempfile.TemporaryDirectory() as folder:
            result=json.loads((run_cpu(m,Path(folder))/'result.json').read_text())
            self.assertEqual(result['backend'],'cpu_interval_oracle')
            self.assertEqual(result['samples'][0]['confirmed_reference'],5)
            self.assertEqual(result['samples'][0]['unknown'],8)
            self.assertFalse(result['publication'])
