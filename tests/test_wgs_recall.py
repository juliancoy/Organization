import unittest
import tempfile
import gzip
from pathlib import Path
from scripts.wgs_recall import alignment, allowed_intervals, call_commands, classify, fastq_audit, split_pilot_fastq

class RecallTests(unittest.TestCase):
    def test_overlapping_masks_and_whole_chromosome(self):
        self.assertEqual(allowed_intervals({'19':100,'1':20},[('19',10,40),('19',30,80),('1',0,20)]),[('19',0,10),('19',80,100)])
    def test_evidence_required(self):
        self.assertEqual(classify('A',[],(0,0),25,None),'unknown')
        self.assertEqual(classify('A',[],(0,0),19,99),'unknown')
        self.assertEqual(classify('A',[],(0,0),20,20),'confirmed_same')
        self.assertEqual(classify('A',['C'],(0,1),20,20),'snv')
        self.assertEqual(classify('A',['AC'],(0,1),20,20),'insertion_anchor')
        self.assertEqual(classify('AC',['A'],(0,1),20,20),'deletion_start')
        self.assertEqual(classify('AC',['GT'],(0,1),20,20),'other')
    def test_all_sites_masked_caller(self):
        command=call_commands('/ref','/bam','19','/allowed.bed','/out')
        self.assertIn('-T /allowed.bed',command);self.assertIn('-f GQ',command)
        self.assertNotIn(' -v',command)
    def test_resource_headroom(self):
        with self.assertRaises(ValueError):alignment('r',['f'],'o','s','r',8,4000)
        self.assertIn('-t 7',alignment('r',['f'],'o','s','r',8,12000))

    def test_fastq_pilot_reports_lengths_not_sequences(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'reads.fastq';path.write_text('@private-id\nACGT\n+\nIIII\n')
            report=fastq_audit(path)
            self.assertEqual(report['reads_below_70bp'],1)
            self.assertNotIn('private-id',str(report))

    def test_short_reads_are_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'reads.fastq'
            path.write_text('@a\nACGT\n+\nIIII\n@b\n'+'A'*70+'\n+\n'+'I'*70+'\n')
            counts=split_pilot_fastq(path,Path(folder)/'out')
            self.assertEqual(counts,{'short':1,'long':1})
            self.assertIn('@a', (Path(folder)/'out/short.fastq').read_text())

    def test_gzip_input_stays_compressed_and_preserves_both_read_classes(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'reads.fq.gz'
            short='@short\nACGT\n+\nIIII\n'
            long='@long\n'+'A'*70+'\n+\n'+'I'*70+'\n'
            # Concatenated gzip members also occur in streamed archive inputs.
            path.write_bytes(gzip.compress(short.encode())+gzip.compress(long.encode()))
            output=Path(folder)/'out'
            self.assertEqual(split_pilot_fastq(path,output,compressed=True),{'short':1,'long':1})
            self.assertEqual(sorted(p.name for p in output.iterdir()),['long.fastq.gz','short.fastq.gz'])
            for name,expected in [('short',short),('long',long)]:
                with gzip.open(output/(name+'.fastq.gz'),'rt') as stream:
                    self.assertEqual(stream.read(),expected)

    def test_corrupt_gzip_cannot_complete_split(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'reads.fq.gz'
            payload=bytearray(gzip.compress(b'@a\nACGT\n+\nIIII\n'))
            payload[-8]^=1  # CRC corruption must be detected at EOF.
            path.write_bytes(payload)
            with self.assertRaises(gzip.BadGzipFile):
                split_pilot_fastq(path,Path(folder)/'out',compressed=True)

    def test_incomplete_fastq_cannot_complete_compressed_split(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'reads.fq.gz';path.write_bytes(gzip.compress(b'@a\nACGT\n+\n'))
            with self.assertRaisesRegex(ValueError,'Malformed FASTQ'):
                split_pilot_fastq(path,Path(folder)/'out',compressed=True)
