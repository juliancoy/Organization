import pytest
from scripts.wgs_catalog import build,files_for,selected_watson,https_file


def watson():
    return {'run_accession':'SRR000390','study_accession':'PRJNA28335','sample_accession':'SAMN00001698','library_strategy':'WGS','instrument_platform':'LS454','instrument_model':'454 GS FLX','library_layout':'SINGLE','fastq_ftp':'ftp.sra.ebi.ac.uk/vol1/fastq/SRR000/SRR000390/SRR000390.fastq.gz','fastq_md5':'a'*32,'fastq_bytes':'700000000','read_count':'20','base_count':'5000'}


def venter():
    return {'run_accession':'SRR7097858','study_accession':'PRJNA448959','sample_accession':'SAMN03491120','library_strategy':'WGS','instrument_platform':'ILLUMINA','instrument_model':'Illumina HiSeq 2000','library_layout':'PAIRED','fastq_ftp':'ftp.sra.ebi.ac.uk/vol1/fastq/SRR709/008/SRR7097858/SRR7097858_1.fastq.gz;ftp.sra.ebi.ac.uk/vol1/fastq/SRR709/008/SRR7097858/SRR7097858_2.fastq.gz','fastq_md5':'b'*32+';'+'c'*32,'fastq_bytes':'46534297261;50990987871','read_count':'100','base_count':'20000'}


def test_umbrella_project_does_not_include_later_wga_runs():
    unrelated={**watson(),'sample_accession':'SAMN12110737','library_strategy':'WGA','instrument_platform':'CAPILLARY'}
    catalog=build([watson(),unrelated],[venter()])
    assert catalog['totals']['files']==3
    assert catalog['totals']['by_sample']['Watson']['runs']==1
    assert catalog['totals']['bytes']==98225285132
    assert not selected_watson(unrelated)
    assert all(row['url'].startswith('https://ftp.sra.ebi.ac.uk/') for row in catalog['files'])
    assert 'APOE' in catalog['watson_release_note']


def test_large_files_keep_publisher_identity_without_arbitrary_cap():
    files=files_for('Venter',venter())
    assert files[0]['bytes']==46534297261
    assert files[0]['md5']=='b'*32
    assert files[1]['filename']=='SRR7097858_2.fastq.gz'


def test_mismatched_checksums_and_locations_are_rejected():
    with pytest.raises(ValueError,match='Incomplete'):
        files_for('Venter',{**venter(),'fastq_md5':'b'*32})
    with pytest.raises(ValueError,match='location'):
        https_file('malicious.example/private.fastq.gz')
    with pytest.raises(ValueError,match='Unexpected'):
        files_for('Watson',{**watson(),'fastq_ftp':'ftp.sra.ebi.ac.uk/vol1/fastq/other.fastq.gz'})


def test_gold_cache_preserves_receipt_and_detects_changes(tmp_path,monkeypatch):
    import io
    import scripts.wgs_catalog as catalog
    class Response(io.BytesIO):
        headers={'Content-Length':'4'}
    monkeypatch.setattr(catalog,'urlopen',lambda *args,**kwargs:Response(b'data'))
    first=catalog.download_gold_file(tmp_path,'SNP.vcf.gz')
    assert first['bytes']==4 and not first['publisher_md5_verified']
    assert (tmp_path/'SNP.vcf.gz').read_bytes()==b'data'
    (tmp_path/'SNP.vcf.gz').write_bytes(b'edit')
    with pytest.raises(ValueError,match='prior receipt'):
        catalog.download_gold_file(tmp_path,'SNP.vcf.gz')


def test_gold_incomplete_download_is_not_promoted(tmp_path,monkeypatch):
    import io
    import scripts.wgs_catalog as catalog
    class Response(io.BytesIO):
        headers={'Content-Length':'5'}
    monkeypatch.setattr(catalog,'urlopen',lambda *args,**kwargs:Response(b'data'))
    with pytest.raises(ValueError,match='Incomplete'):
        catalog.download_gold_file(tmp_path,'SNP.vcf.gz')
    assert not (tmp_path/'SNP.vcf.gz').exists()


def test_gold_audit_unions_reference_regions_and_reports_cross_file_positions(tmp_path):
    import gzip,json
    from scripts.wgs_catalog import audit_gold
    header='##fileformat=VCFv4.2\n##reference=file:///private/human_g1k_v37_decoy.fasta\n##contig=<ID=1,length=100>\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\til_fb\til_hc\til_st\tsg_fb\tsg_hc\tsg_st\n'
    def row(pos,ref,alt):return f'1\t{pos}\t.\t{ref}\t{alt}\t.\tPASS\t.\tGT\t./.\t./.\t./.\t./.\t./.\t./.\n'
    for name,rows in [('SNP.vcf.gz',row(10,'A','C')+row(20,'AC','GT')),('INDEL.vcf.gz',row(10,'A','AT')+row(30,'AT','A'))]:
        with gzip.open(tmp_path/name,'wt') as stream:stream.write(header+rows)
    with gzip.open(tmp_path/'hcref.filtered.bed.gz','wt') as stream:stream.write('1\t0\t40\n1\t30\t50\n')
    fai=tmp_path/'ref.fai';fai.write_text('1\t100\t0\t100\t101\n')
    result=audit_gold(tmp_path,fai)
    assert result['assembly']=='GRCh37'
    assert result['snp_indel_start_position_overlap']==1
    assert result['combined_unique_primary_start_positions']==3
    assert result['high_confidence_reference_bed']['primary_positions']==50
    assert result['sources']['SNP.vcf.gz']['variant_types_by_record']=={'single':1,'multi':1}
    assert len(result['sources']['SNP.vcf.gz']['caller_columns'])==6
    assert 'private' not in json.dumps(result)


def test_gold_audit_reports_source_mitochondria_missing_from_local_reference(tmp_path):
    import gzip
    from scripts.wgs_catalog import audit_gold_vcf
    path=tmp_path/'variants.vcf.gz'
    with gzip.open(path,'wt') as stream:
        stream.write('##contig=<ID=1,length=100>\n##contig=<ID=MT,length=16569>\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\nMT\t1\t.\tA\tC\t.\tPASS\t.\n1\t10\t.\tA\tC\t.\tPASS\t.\n')
    summary,positions=audit_gold_vcf(path,{'1':100})
    assert summary['unavailable_primary_records']==1
    assert summary['source_primary_contigs_not_in_local_reference']==['MT']
    assert summary['unique_primary_start_positions']==1
