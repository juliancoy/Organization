import io
import unittest
from scripts.wgs_reference_confidence import reference_quality,annotate
class ConfidenceTests(unittest.TestCase):
    def test_reference_confidence_uses_all_alternative_genotypes(self):
        self.assertEqual(reference_quality('0,60,255'),60)
        self.assertEqual(reference_quality('0,0,255'),3)
        self.assertEqual(reference_quality('60,0,255'),0)
        self.assertIsNone(reference_quality('0,.,255'))
        self.assertIsNone(reference_quality('0,nan,255'))
        self.assertEqual(reference_quality('0,20,20'),17)
        self.assertIsNone(reference_quality('0,60'))
        self.assertIsNone(reference_quality('0,10,40,60'))
    def test_tag_preserves_source_likelihoods(self):
        output=io.StringIO();annotate(io.StringIO('#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS\n1\t1\t.\tA\t<*>\t0\t.\t.\tPL:DP\t0,60,255:20\n'),output)
        self.assertIn('RCQ=60\tPL:DP\t0,60,255:20',output.getvalue())
