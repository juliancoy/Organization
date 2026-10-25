import gzip
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('genome_demo',Path(__file__).resolve().parents[1]/'scripts/genome_demo.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class GenomeDemoTests(unittest.TestCase):
    def test_overlap_uses_half_open_reference_span_and_counts_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'input.vcf.gz'
            with gzip.open(path,'wt') as out:
                out.write('#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n')
                for pos,ref in [(10,'A'),(11,'A'),(20,'A'),(21,'A'),(9,'AAA')]:
                    out.write(f'1\t{pos}\t.\t{ref}\tT\t.\tPASS\t.\n')
            counts,bins,overlap=module.distribution(path,{'1':[(10,20)]})
            self.assertEqual(counts,{'1':5});self.assertEqual(overlap,{'1':3})
            self.assertEqual(bins,{'1':{0:5}})

    def test_satellite_class_only_and_interval_union(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'rmsk.gz'
            with gzip.open(path,'wt') as out:
                for start,end,kind in [(10,20,'Satellite'),(15,25,'Satellite'),(0,100,'LINE')]:
                    fields=['0']*17;fields[5]='chr1';fields[6]=str(start);fields[7]=str(end);fields[11]=kind;fields[12]='family'
                    out.write('\t'.join(fields)+'\n')
            regions,families=module.satellite_intervals(path)
            self.assertEqual(regions,{'1':[(10,25)]})
            self.assertEqual(families,{'family':20})
