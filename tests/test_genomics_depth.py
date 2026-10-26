import gzip
from pathlib import Path
import tempfile
import unittest

from scripts.genomics_depth import summarize, windows


class DepthTests(unittest.TestCase):
    def summarize(self, rows, contigs):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'depth.gz'
            with gzip.open(path, 'wt') as stream:
                stream.write('#chrom\tstart\tend\tregion\t1X\t10X\t20X\t30X\t50X\n' + '\n'.join(rows) + '\n')
            return summarize(path, contigs)

    def test_windows_include_zero_coverage_and_partial_terminal_interval(self):
        self.assertEqual(list(windows([('1', 100001), ('X', 2)])), [('1', 0, 100000), ('1', 100000, 100001), ('X', 0, 2)])

    def test_counts_use_all_reference_bases_and_keep_autosomes_separate(self):
        result = self.summarize(['1\t0\t100\ta\t90\t80\t70\t60\t0', 'X\t0\t50\tb\t0\t0\t0\t0\t0'], [('1', 100), ('X', 50)])['groups']
        self.assertEqual(result['all_bam_contigs']['reference_bases'], 150)
        self.assertEqual(result['all_bam_contigs']['percent_at_least']['1'], 60)
        self.assertEqual(result['autosomes']['percent_at_least']['20'], 70)

    def test_missing_or_duplicate_windows_cannot_inflate_coverage(self):
        row = '1\t0\t100\ta\t90\t80\t70\t60\t0'
        with self.assertRaisesRegex(ValueError, 'missing'):
            self.summarize([row], [('1', 100), ('X', 50)])
        with self.assertRaisesRegex(ValueError, 'differ'):
            self.summarize([row, row], [('1', 100)])

    def test_nonmonotonic_thresholds_and_counts_above_length_fail(self):
        for row in ['1\t0\t100\ta\t80\t90\t70\t60\t0', '1\t0\t100\ta\t101\t80\t70\t60\t0']:
            with self.assertRaisesRegex(ValueError, 'Invalid'):
                self.summarize([row], [('1', 100)])
