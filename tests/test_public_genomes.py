import gzip
import json
from pathlib import Path
import tempfile
import unittest
from scripts.public_genomes import Reference,convert_pgsnp
from scripts.genomics_compute import compute
from scripts.genomics_distributed import merge

class PublicGenomeTests(unittest.TestCase):
    def test_import_preserves_unknown_genotype_and_reports_exclusions(self):
        with tempfile.TemporaryDirectory()as d:
            root=Path(d);fasta=root/'ref.fa';fasta.write_text('>1\nACGT\n');Path(str(fasta)+'.fai').write_text('1\t4\t3\t4\t5\n')
            source=root/'source.gz'
            with gzip.open(source,'wt')as stream:stream.write('1\tchr1\t0\t1\tA/G\t2\t10,5\t0,0\n1\tchr1\t1\t2\tC\t1\t4\t0\n1\tchrM\t0\t1\tA\t1\t4\t0\n1\tchr1\t2\t4\tNT/-\t2\t4,3\t0,0\n')
            out=root/'out.gz';counts=convert_pgsnp(source,fasta,out,'PublicExample')
            self.assertEqual(counts['retained_observed_variant_records'],1)
            result=compute(out.read_bytes(),'qc')
            self.assertEqual(result['genotypes'],{'missing_or_partial':1});self.assertEqual(result['depth_bins'],{'missing':1})
            with gzip.open(out,'rt')as stream:line=next(line for line in stream if not line.startswith('#'))
            self.assertIn('\tA\tG\t',line);self.assertTrue(line.endswith('\t./.\n'))
            self.assertFalse(json.loads(Path(str(out)+'.import.json').read_text())['genotype_inferred'])

    def test_bcsq_annotations_are_understood_without_pretending_they_are_vep(self):
        source='##fileformat=VCFv4.2\n##INFO=<ID=BCSQ,Number=.,Type=String,Description="Format: Consequence|gene|transcript">\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSynthetic\n1\t1\t.\tA\tC\t.\t.\tBCSQ=missense|GENE|TX,missense|GENE|OTHER\tGT\t./.\n'
        result=compute(gzip.compress(source.encode()),'annotation')
        self.assertEqual(result['records_with_csq'],1);self.assertEqual(result['consequences'],{'missense':1})

    def test_dataset_summaries_do_not_mix_people(self):
        results=[{'kind':'qc','dataset':'Watson','records':3},{'kind':'qc','dataset':'HG001','records':7}]
        self.assertEqual(merge(results,'qc','Watson')['records'],3)
        self.assertEqual(merge(results,'qc','HG001')['records'],7)
        self.assertEqual(merge(results,'qc')['records'],0)

    def test_legacy_deletions_and_insertions_are_anchored_to_reference(self):
        with tempfile.TemporaryDirectory()as d:
            root=Path(d);fasta=root/'ref.fa';fasta.write_text('>1\nACGT\n');Path(str(fasta)+'.fai').write_text('1\t4\t3\t4\t5\n')
            source=root/'source.gz'
            with gzip.open(source,'wt')as stream:stream.write('1\tchr1\t1\t3\tCG/-\t2\t0,0\t0,0\n1\tchr1\t3\t3\t-/AA\t2\t0,0\t0,0\n')
            out=root/'out.gz';convert_pgsnp(source,fasta,out,'PublicExample')
            with gzip.open(out,'rt')as stream:records=[line.split('\t')for line in stream if not line.startswith('#')]
            self.assertEqual(records[0][1:5],['1','.','ACG','A'])
            self.assertEqual(records[1][1:5],['3','.','G','GAA'])
