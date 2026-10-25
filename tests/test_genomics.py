import gzip
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('genomics',Path(__file__).resolve().parents[1]/'scripts/genomics.py')
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
HEADER='##fileformat=VCFv4.2\n##reference=GRCh38\n##reference=file:///private/reference.fa\n##contig=<ID=1>\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tprivate-person\n'

class GenomicsTests(unittest.TestCase):
    def test_inventory_omits_identifiers_genotypes_and_external_symlinks(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'test.vcf').write_text(HEADER+'1\t1\t.\tA\tC\t30\tPASS\t.\tGT\t0/1\n')
            (root/'outside.vcf').symlink_to('/etc/passwd')
            result=g.inventory(root);serialized=json.dumps(result)
            self.assertEqual(len(result['files']),1)
            self.assertEqual(result['files'][0]['header']['samples'],1)
            self.assertNotIn('private-person',serialized);self.assertNotIn('0/1',serialized)
            self.assertEqual(result['files'][0]['header']['references'],['GRCh38'])

    def test_compressed_header_and_invalid_input(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'test.vcf.gz'
            with gzip.open(p,'wt') as stream:stream.write(HEADER)
            self.assertEqual(g.vcf_header(p)['contigs'],1)
            (Path(d)/'bad.vcf').write_text('not a VCF\n')
            with self.assertRaises(ValueError):g.vcf_header(Path(d)/'bad.vcf')

    def test_reports_are_private_and_outside_git(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);folder=g.private_run(root/'reports')
            self.assertEqual(folder.stat().st_mode&0o777,0o700)
            (root/'.git').mkdir()
            with self.assertRaises(ValueError):g.private_run(root/'tracked')

    def test_gvcf_cannot_be_mistaken_for_variant_only_qc(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'blocks.vcf';p.write_text(HEADER.replace('##reference=GRCh38','##ALT=<ID=NON_REF,Description="reference block">'))
            with self.assertRaisesRegex(ValueError,'prepare'):g.quality_control(p,root)

    def test_qc_uses_argument_array_and_checks_tool_exit(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'sample;literal.vcf';p.write_text(HEADER)
            with patch.object(g.shutil,'which',return_value='/usr/bin/bcftools'),patch.object(g.subprocess,'check_output',return_value='bcftools test\n'),patch.object(g.subprocess,'run',return_value=SimpleNamespace(returncode=1)) as run:
                with self.assertRaises(RuntimeError):g.quality_control(p,root)
            self.assertEqual(run.call_args.args[0][-1],str(p));self.assertNotIn('shell',run.call_args.kwargs)
            self.assertEqual(json.loads((root/'provenance.json').read_text())['exit_code'],1)

    def test_annotation_counts_records_not_transcripts_and_makes_no_diagnosis(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'annotated.vcf'
            source.write_text(HEADER.replace('##reference=GRCh38','##INFO=<ID=CSQ,Number=.,Type=String,Description="Format: Allele|Consequence">')+'1\t1\t.\tA\tC\t30\tPASS\tCSQ=C|missense_variant,C|missense_variant&splice_region_variant\tGT\t0/1\n')
            g.annotation_summary(source,root)
            result=json.loads((root/'annotation-summary.json').read_text())
            self.assertEqual(result['consequence_record_counts'],{'missense_variant':1,'splice_region_variant':1})
            self.assertNotIn('private-person',json.dumps(result))

    @unittest.skipUnless(g.shutil.which('bcftools'),'bcftools integration requires local tool')
    def test_reference_blocks_removed_but_real_called_alternate_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'blocks.vcf'
            header='##fileformat=VCFv4.2\n##contig=<ID=1,length=100>\n##ALT=<ID=NON_REF,Description="Reference block">\n##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsynthetic\n'
            source.write_text(header+'1\t1\t.\tA\t<NON_REF>\t30\tPASS\t.\tGT\t0/0\n1\t2\t.\tA\tC,<NON_REF>\t30\tPASS\t.\tGT\t0/1\n')
            output=g.called_variants(source,root)
            with gzip.open(output,'rt') as stream:records=[line for line in stream if not line.startswith('#')]
            self.assertEqual(len(records),1)
            self.assertEqual(records[0].split('\t')[4],'C')
