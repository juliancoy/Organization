# DAT1 / SLC6A3 10-repeat assessment

The target is the 40-bp 3′-UTR VNTR commonly called rs28363170, not gene
presence, a neighboring SNP or the unrelated DRD4 repeat. Published GRCh38 TR30
coordinates are chr5:1393582–1393985 (1-based inclusive); the audit stores these
as 1393581–1393985 (0-based half-open). Source:
https://doi.org/10.26508/lsa.202201677.

## Interpretation gate

The deployed audit is source-callability QC, **not a genotype caller**. It never
reports a 10-repeat allele absent. Missing VCF entries, ordinary reference blocks,
coverage alone and two workers reproducing the same metrics cannot establish a
repeat genotype. Neither allele count nor an ADHD diagnosis is inferred here.

Before an absence claim, require a locus-specific validated assay that resolves
both alleles, detects a 10-repeat allele in positive controls, detects it in a
heterozygous mixture, and distinguishes the relevant neighboring repeat lengths.
Check allele dropout, sufficient usable coverage, reference/coordinate identity,
repeat boundary/model suitability and confidence intervals or equivalent assay
uncertainty. No-call, failed controls, unresolved allele or conflicting evidence
remain inconclusive. Computational agreement is reproducibility, not an
independent biological confirmation. Use DAT1-specific PCR/fragment sizing or
suitable long-read data if the available sequencing cannot resolve both alleles.

## Deployed audit

`python -m scripts.genomics_dat1 --bam ORIGINAL.bam --output PRIVATE_REPORT_DIR`

This uses the established private runner's authenticated enrolled identities,
pinned SSH host keys and immutable approved Python image. Per-worker CPU/RAM
leases bound execution to one CPU and 256 MiB. Docker has no network, mounts,
persistent genomic input, logs, swap or core dumps. Target alignments travel in
encrypted SSH stdin and remain in RAM on workers; reports stay under a mode-0700
controller directory outside Git. Source BAMs and indexes remain unchanged.
Cancellation cleans up audit containers through the private runner.

Two workers run synthetic evidence-gating controls and the same target QC:
mapQ >=30, baseQ >=20, secondary/supplementary/duplicate/QC-failed/unmapped
exclusion, flank/repeat depth and directly spanning template counts. Controls
check the QC logic, **not sensitivity of a VNTR genotype caller**. Source geometry,
worker code and input hashes are recorded, and disagreements block completion.

adVNTR 1.5.0 was inspected in the public Biocontainers image
`quay.io/biocontainers/advntr@sha256:1d9c04c007c2d986ea988ed3eac6c61719bc489144db4fbc8105246d581fc35c`.
The author-provided recommended GRCh38 model database has no model for this
locus. A custom model must undergo locus-specific validation before being used
for absence claims; adVNTR was not run on the private sample by this audit.
Official model archive:
https://cseweb.ucsd.edu/~mbakhtia/adVNTR/vntr_data_recommended_loci_hg38.zip
(SHA-256 `00ae45822e9f8574e3f6c86651b2c1f4d8b972eea3f4ac4ea00d71a0691287cf`).

No personal read sequences, individual repeat genotypes or clinical predictions
belong in public task artifacts, Genome Explorer or Git. The existing private
report lifecycle and owner deletion policy apply; no new public data store is
introduced.
