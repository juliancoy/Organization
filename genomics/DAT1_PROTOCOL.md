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

## Experimental caller validation (public reference only)

Build the isolated Linux amd64 image from the pinned upstream image and
hash-checked dependencies:

```
docker build --iidfile /tmp/dat1-image-id genomics/dat1-image
python -m scripts.genomics_dat1_assay --image "$(cat /tmp/dat1-image-id)"
```

The upstream adVNTR 1.5.0 image combines pomegranate 0.6.1 with incompatible
NetworkX 3.5 and MUSCLE 5. This image pins NetworkX 1.11, decorator 4.4.2 and
MUSCLE 3.8.1551. A startup compatibility file restores Python's removed
`fractions.gcd` alias to `math.gcd`; it does not change HMM mathematics.
The upstream activation entry point resets PATH, so the derived image executes
commands directly with explicit binary/library paths.

The installed 1.5.0 CLI does **not** expose the older documented
`--accuracy_filter` option. The experimental wrapper uses the installed author's
HMM recruitment, repeat-count and genotype-likelihood APIs. It accepts only
reads matching both flanks, deduplicates read identifiers per repeat count,
and requires three supporting read identifiers per count. Partial-read repeat
counts are lower bounds and cannot become a genotype. This is a custom research
wrapper, not a clinically validated DAT1 assay; the caller's internal model
probability is not a calibrated clinical confidence score.

Reference segmentation is restricted to the published 404-bp interval. The HMM
assigns its terminal base to the right flank, giving a 403-bp, ten-unit model.
The wrapper permits at most one terminal base on either edge to be assigned to
a flank, and requires a unique contiguous reconstruction of the full published
reference interval. Both coordinate conventions are recorded. No sample data
or desired genotype is used to choose the model boundaries.

The synthetic panel contains 9R/9R, 9R/10R and 10R/10R. Each is tested with
spanning reads and with 150-bp windows, using deletion-aware alignments.
Spanning controls must return their known counts. Non-spanning controls must
return an explicit no-call. Missing controls, failed processes or inconsistent
reports fail validation. These idealized reference-derived controls assess
software operation and safe handling of insufficient evidence; they do not
validate real-world allele dropout, sequencing errors, rare alleles or all
possible repeat-aware approaches to short-read data.

A `software_controls_passed_short_reads_unresolved` result means the software
passed these controls **and this method cannot resolve the matched 150-bp
controls**. It authorizes no private genotype or absence claim. This entry point
does not load Julian's reads. The original short-read audit and any subsequent
private analysis remain separate.

Execution uses one CPU and 2 GiB under renewable authenticated resource leases,
with network disabled, read-only root, no container logging, swap or core dumps,
and RAM-only temporary work. Cancellation and failure remove the container;
leases are released only after cleanup is confirmed. Container exit/OOM state,
worker/image identities and reference/code hashes are retained in private
reports outside Git. This work requires no Swarm Control restart.

## Read-level check of the downloaded public HuRef source

`genomics/dat1-readscan-image` pins rapidgzip and pyahocorasick wheels by hash.
Build that image, obtain its immutable local Docker ID, then run:

```
python -m scripts.genomics_venter_dat1 --image sha256:YOUR_LOCAL_IMAGE_ID
```

The source is the verified downloaded `SRR7097858_1.fastq.gz` receipt, not a
literature genotype or the incomplete existing alignment shards. This is a
later 100-bp Illumina HuRef run, distinct from the original Sanger project.
Reference 31-mer seeds from the DAT1 region and both orientations recruit
candidates directly from FASTQ. The extractor checks complete record structure,
DNA alphabet and Phred+33 quality characters, excludes hits occurring only in
quality lines, and deduplicates read identifiers across buffered boundaries.
Four matching processes and three decompression workers fit an eight-CPU,
4-GiB renewable resource lease. Original source data is mounted read-only.

A recorded no-hit prefix may be reused only with matching source receipt and
reference context; it is re-decoded before matching resumes with overlap.
Full scans export a reusable compressed-stream index. Partial scans must remain
explicitly partial: they do not establish that all alleles or both FASTQ mates
were searched. Reference-seeded recruitment can miss divergent alleles and is
not itself repeat genotyping. Reports, recruited reads, alignments and any
experimental caller output stay in the private work directory outside Git.

End-to-end synthetic extractor controls check both strands, quality-line-only
false matches, complete scanning and index export. Each fixture has its own
source hash and is labeled synthetic. In a read-level follow-up, candidate
alignments to the full genome establish locus identity; coverage and a
reference-like assembly path must still not be promoted to a repeat genotype.
In particular, an assembly algorithm must reject false 10R paths in 11R and
9R/11R controls before being used as evidence of a 10R allele.
