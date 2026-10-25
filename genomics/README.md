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
The explicit private runner below uses enrolled identities, encrypted transport,
RAM-only worker inputs, resource limits, and private controller results. This
command is the authorization to send the specified inputs to enrolled computers.
GPU presence alone is not authorization.
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

## Explicit distributed private run

The standalone runner now supports private CPU jobs on the enrolled computers:

```bash
~/.local/share/swarm-control/venv/bin/python -m scripts.genomics_distributed \
  --qc "$GENOMICS_CALLED_VCF" \
  --annotations "$GENOMICS_ANNOTATED_VCF" \
  --output "$GENOMICS_REPORT_DIR"
```

It rechecks authenticated launcher identity and accepting/battery policy, pinned
SSH host keys, matching image contents, and Docker memory/swap enforcement.
Each machine has up to two one-CPU containers; new batches wait when RAM is low
or CPU utilization exceeds 85%. Another active test session does not by itself
exclude a machine with remaining capacity. Containers use an existing immutable
Python image, no network, no mounts, read-only filesystems, no swap, disabled
Docker logging and core dumps, and automatic removal. Genomic payloads travel
through SSH stdin and remain in container memory. No private inputs or results go
through the general test upload/artifact APIs. SSH host administrators remain
trusted; this does not defend against an already compromised worker OS.

Batches stop at chromosome boundaries or 50,000 records. Results include the
compressed-input checksum, input count, worker identity, and measured runtime.
The private state.json tracks assignment, capacity waits, completion, and failure.
Per-batch results and merged QC/annotation summaries remain on the controller's
data disk. Any missing or mismatched result fails the run. Failed runs retain completed private reports. Add `--resume /private/run-directory`
to reuse checksum-verified completed batches with matching source identity and
scientific worker code. Transient readiness/capacity changes put batches back in
the shared queue; execution failures retry on another worker, at most three
execution attempts per batch. Unknown CPU or RAM readings wait for a fresh sample.

The QC summary counts retained records, filter labels, variant/genotype classes,
allele-level transitions/transversions, and DP/GQ histograms. It does not apply
additional filtering or estimate genome-wide coverage. Annotation jobs summarize
existing VEP CSQ consequences; they do not execute VEP or transfer its cache.
This is a separate explicit private batch runner, not a general private-data
extension of the public test scheduler. Its native integration tests use invented
variants, and can run through the ordinary distributed test master.
