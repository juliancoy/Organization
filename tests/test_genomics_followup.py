import tempfile
from pathlib import Path
import unittest

from scripts.genomics_followup import coverage_summary, gvcf_spans, variant_identity


class FollowupTests(unittest.TestCase):
    def test_annotation_identity_detects_changed_alleles_and_genotypes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'synthetic.vcf'
            row = '1\t1\t.\tA\tC\t30\tPASS\t.\tGT\t0/1\n'
            source.write_text(row)
            before = variant_identity(source)
            source.write_text(row.replace('\t.\tGT', '\tCSQ=C|missense_variant\tGT'))
            after = variant_identity(source)
            self.assertEqual(before['variant_and_genotype_sha256'], after['variant_and_genotype_sha256'])
            self.assertEqual(after['records_with_csq'], 1)
            source.write_text(row.replace('0/1', '1/1'))
            self.assertNotEqual(before['variant_and_genotype_sha256'], variant_identity(source)['variant_and_genotype_sha256'])

    def spans(self, rows):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'synthetic.vcf'
            source.write_text('##fileformat=VCFv4.2\n' + '\n'.join(rows) + '\n')
            return gvcf_spans(source)['counts']

    def test_reference_blocks_weight_bases_and_preserve_unknown_gq(self):
        result = self.spans(['1\t1\t.\tA\t<NON_REF>\t.\tPASS\tEND=100;MinDP=25\tGT:DP\t0/0:30'])
        self.assertEqual(result['represented_bases'], 100)
        self.assertEqual(result['called_min_depth_20_span'], 100)
        self.assertEqual(result['genotype_quality_unknown_span'], 100)
        self.assertNotIn('pass_depth_20_gq_20_span', result)

    def test_block_average_depth_is_not_assumed_to_be_minimum(self):
        result = self.spans(['1\t1\t.\tA\t<NON_REF>\t.\tPASS\tEND=100\tGT:DP:GQ\t0/0:30:50'])
        self.assertEqual(result['depth_confidence_unknown_span'], 100)
        self.assertNotIn('called_min_depth_20_span', result)

    def test_overlap_is_not_double_counted_and_failed_filters_stay_excluded(self):
        result = self.spans(['1\t1\t.\tA\t<NON_REF>\t.\tPASS\tEND=10;MinDP=25\tGT:GQ\t0/0:30',
                             '1\t10\t.\tA\tC\t.\tLowGQ\t.\tGT:DP:GQ\t0/1:25:30'])
        self.assertEqual(result['represented_bases'], 10)
        self.assertEqual(result['called_min_depth_20_span'], 10)
        self.assertEqual(result['pass_depth_20_gq_20_span'], 10)

    def test_missing_genotypes_and_unsorted_input(self):
        result = self.spans(['1\t1\t.\tA\tC\t.\tPASS\t.\tGT:DP:GQ\t./.:40:40'])
        self.assertNotIn('called_genotype_span', result)
        with self.assertRaisesRegex(ValueError, 'sorted'):
            self.spans(['1\t2\t.\tA\tC\t.\tPASS\t.\tGT\t0/1', '1\t1\t.\tA\tC\t.\tPASS\t.\tGT\t0/1'])

    def test_coverage_is_length_weighted_and_separates_autosomes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'coverage.tsv'
            path.write_text('#rname\tstartpos\tendpos\tnumreads\tcovbases\tcoverage\tmeandepth\tmeanbaseq\tmeanmapq\n1\t1\t100\t10\t90\t90\t20\t30\t40\nX\t1\t50\t5\t25\t50\t10\t30\t40\n')
            result = coverage_summary(path)['groups']
            self.assertAlmostEqual(result['all_bam_contigs']['mean_depth'], 2500 / 150)
            self.assertEqual(result['autosomes']['reference_bases'], 100)
            self.assertEqual(result['autosomes']['covered_bases'], 90)
