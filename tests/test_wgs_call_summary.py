import io
import unittest
from scripts.wgs_call_summary import summarize

def row(pos,ref,alt,gt='0/0',dp=30,gq=30,rcq=30):return f'1\t{pos}\t.\t{ref}\t{alt}\t.\t.\tRCQ={rcq}\tGT:DP:GQ\t{gt}:{dp}:{gq}\n'
class SummaryTests(unittest.TestCase):
    def test_deletion_span_excludes_later_reference_and_unknown_stays_unknown(self):
        source=io.StringIO(row(1,'A','.')+row(2,'AAA','A','0/1')+row(3,'A','.')+row(4,'A','.')+row(5,'A','.'))
        bed=io.StringIO();vcf=io.StringIO();result=summarize(source,'1',10,bed,vcf)
        self.assertEqual(result['counts']['confirmed_same'],2)
        self.assertEqual(result['counts']['deletion_start'],1)
        self.assertEqual(result['counts']['unknown'],7)
        self.assertEqual(bed.getvalue(),'1\t0\t1\n1\t4\t5\n')
    def test_uncertain_alt_excludes_same_position_even_if_reference_arrives_first(self):
        result=summarize(io.StringIO(row(1,'A','.')+row(1,'AC','A','./.')), '1',3,io.StringIO(),io.StringIO())
        self.assertEqual(result['counts']['confirmed_same'],0)
    def test_N_and_missing_GQ_are_never_matches(self):
        result=summarize(io.StringIO(row(1,'N','.')+row(2,'A','.',gq='.',rcq='.')),'1',3,io.StringIO(),io.StringIO())
        self.assertEqual(result['counts']['unknown'],3)
    def test_whole_chromosome_mask(self):
        result=summarize(io.StringIO(row(1,'A','C','0/1')),'1',100,io.StringIO(),io.StringIO(),excluded=True)
        self.assertEqual(result['counts']['unknown'],100)

    def test_qual_and_variant_GQ_cannot_substitute_for_reference_RCQ(self):
        result=summarize(io.StringIO(row(1,'A','.',gq=99,rcq='.')+row(2,'A','.',rcq=17)), '1',2,io.StringIO(),io.StringIO())
        self.assertEqual(result['counts']['unknown'],2)
