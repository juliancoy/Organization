# Private genomics workbench

Initial scope: single-genome quality control and an audit of existing VEP annotations.
This is descriptive research analysis, not a clinical interpretation or a GWAS.

Keep all input files, derived VCFs, reports, and individual-level results outside
Git and outside Swarm Control's artifact directories. The test suite uses invented
variants only. Tests can run across the swarm; private genomes are not staged to
workers, phones, cloud APIs, or public dashboards.

Run from the Organization checkout, with an output directory on your data disk:

```bash
python3 -m scripts.genomics inventory "$GENOMICS_DATA_DIR" --output "$GENOMICS_REPORT_DIR"
python3 -m scripts.genomics prepare "$GENOMICS_VCF" --output "$GENOMICS_REPORT_DIR"
python3 -m scripts.genomics annotation-summary "$GENOMICS_ANNOTATED_VCF" --output "$GENOMICS_REPORT_DIR"
```

Each command creates a new private run directory. Existing files are read only.
`prepare` needs bcftools and produces an indexed called-variant VCF and bcftools
statistics. It keeps alternate genotypes, trims unused alleles, and excludes gVCF
reference symbols. It deliberately excludes hom-reference and uncalled sites:
its missingness and depth summaries describe retained variants, not whole-genome
callability or sequencing coverage. No PASS, depth, or quality cutoff is silently
applied. Keep the original gVCF/BAM for subsequent coverage and callability work.

`annotation-summary` reads existing VEP CSQ annotations and counts each consequence
once per VCF record. Consequence categories overlap. This audits an existing file;
it does not establish that file's completeness or equivalence to another VCF and
does not classify variants as pathogenic.

## Tool and data placement

- bcftools and samtools: CPU tools for input validation, variant QC, BAM coverage.
- Ensembl VEP: offline functional annotation; match tool and cache release as well
  as the reference assembly. The existing dataset has a release-115 GRCh38 cache
  and a VEP 115.2 annotated file. Reuse these for a reproducible baseline before
  separately validating an upgrade. Never send private variants to the VEP web API.
- PLINK 2: later cohort QC and population comparisons after assembly, strand,
  sample metadata, and allele harmonization are established.
- R/Bioconductor: later statistical summaries and reproducible notebooks.

Start local to the data on PILEDRIVE. Raw reads and BAMs are much larger than
called variants; do not duplicate them across the cluster. The current general
swarm execution and artifact paths are not an approved private-genome scheduler.
Before real chromosome jobs are sent to a worker, add explicit enrollment for
private data, encrypted transport/storage, access controls, bounded retention,
and a separate private artifact path. GPU presence alone is not authorization.
CPU chromosome parallelism is the first useful scaling step; phones remain useful
for synthetic browser/visualization tests rather than handling private genomes.

For population work, choose a documented reference cohort (for example the 1000
Genomes Project) rather than treating several famous individual genomes as a
statistically representative sample. Preserve reference-data versions and terms.

## Verification

```bash
python3 -m unittest discover -s tests -p test_genomics.py -v
```

The synthetic integration test checks that a reference block is removed while a
called real allele adjacent to NON_REF is preserved. It runs when bcftools is
installed and explicitly skips otherwise. The remaining tests have no external
dependencies. Request `tests/test_genomics.py` through the distributed master's
focused-test API to exercise those checks on workers without private inputs.
