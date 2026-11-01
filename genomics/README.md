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

`python3 -m scripts.genomics_followup --help` describes the private follow-up
pipeline. It runs quality-filtered BAM coverage, a reference-block-aware gVCF
span audit, and fresh offline VEP 115.2 annotation concurrently. Pin the local
VEP image by SHA-256 and pass the local launcher's `.resources` directory so
these jobs share CPU/RAM admission with ordinary tests. BAM and cache remain
local; after annotation, bounded encrypted streams use eligible enrolled workers
for QC and consequence summaries. Run under a user service for long analyses.
Private state and reports live in a new output run directory. Annotation identity
checks require unchanged ordered alleles and genotypes. Coverage includes
reference gaps; missing block GQ remains unknown. Neither report is a validated
clinical callable-region mask or a pathogenicity classification.

`python3 -m scripts.genomics_depth --help` adds a full-reference depth distribution
to an existing private follow-up run, using a pinned local mosdepth binary. Its
100 kb windows include every BAM reference interval, including zero-depth contigs.
The summary checks interval identity and threshold monotonicity before reporting
breadth at 1x, 10x, 20x, 30x and 50x, separately for all contigs and autosomes.
It uses MQ >=20, duplicate/supplementary exclusion and mate-overlap correction;
mosdepth has no base-quality filter. Its output therefore has a different scope
from the BQ >=20 samtools coverage scan. Binary version, checksum, exact command
and BAM identity are recorded alongside the private reports.

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

## Public comparison datasets

A bounded initial public collection contains the UCSC hg19 `pgWatson` and
`pgVenter` archives, plus GIAB HG001, HG002 and HG005 v4.2.1 GRCh38 autosomal
benchmark calls. These are published variant datasets, not raw whole-genome
sequencing reads and not a representative population cohort. Source URLs,
assembly, local SHA-256 receipts, sizes and preparation logs stay with each file.
The five datasets plus two Ensembl GFF resources total about 560 MiB to download.

```bash
python3 -m scripts.public_genomes_download --output "$PUBLIC_GENOMES_DIR"
python3 -m scripts.public_genomes prepare --root "$PUBLIC_GENOMES_DIR" \
  --reference37 "$GRCH37_FASTA" --reference38 "$GRCH38_FASTA"
~/.local/share/swarm-control/venv/bin/python -m scripts.public_genomes analyse \
  --root "$PUBLIC_GENOMES_DIR" --output "$GENOMICS_REPORT_DIR"
```

The GRCh37 reference helper aliases the existing primary CM000663.1--CM000686.1
FASTA index to chromosomes 1--22, X and Y without modifying or copying sequence.
Legacy pgSnp records use 0-based intervals and observed allele strings. The importer
fetches reference bases, anchors supported insertions/deletions, and retains unknown
GT/DP/GQ values as missing. It does not infer diploid genotypes or read depth from
allele counts/frequencies. Ambiguous, unavailable-contig, >10kb and unanchorable
records are explicitly counted as exclusions. Watson's published masking is not
filled in or imputed. Retained records are not a complete inventory of his genome.

Modern benchmark variants retain available genotype metadata and are checked and
left-normalized against GRCh38. Their benchmark scope is high-confidence autosomal
regions; absent variants do not establish a reference genotype or genome coverage.

Fresh annotation uses offline `bcftools csq --local-csq --samples -` with matching
Ensembl GRCh37.87 or GRCh38.115 gene models. This produces BCSQ coding consequences,
not VEP CSQ results; both annotation tags are understood by the summary worker.
These predictions are site-local and independent of sample genotype/phase. They
are not comprehensive VEP noncoding/regulatory annotation, and missing coding
consequences are not evidence that a variant is benign. Transcript/CDS warnings
from the supplied gene models remain in each processing.log. No GRCh37/GRCh38
coordinate mixing or direct allele-burden ranking is performed.

All five people share one bounded distributed work queue, with dataset labels in
job IDs and results to keep each summary separate. Each dataset gets QC and coding
consequence summaries; native bcftools statistics provide independent record-count
checks. The sources, discarded records, annotation engine and tool/reference
versions must accompany any later scientific comparison.

## Demo-day Genome Explorer

`/genome-explorer.html` is a dependency-free visual tour, linked from Research.
It intentionally serves aggregate summaries for Julian and five public datasets;
no individual loci, alleles, reads, local paths, or clinical predictions are exported.
The static aggregate snapshot is visible to anyone who can access the page.

Rebuild after a completed analysis:

```bash
curl --fail --location https://hgdownload.soe.ucsc.edu/goldenPath/hg38/database/rmsk.txt.gz -o /mnt/PILEDRIVE/genomics-workbench/demo/rmsk.txt.gz
python3 scripts/genome_demo.py --output swarm-control/server/view/genome-data.js
```

The builder checks chromosome totals against the completed QC summaries. Charts
include variant types, genotype categories, called-site depth, ten-megabase windows,
and functional consequences. RepeatMasker `repClass=Satellite` intervals are merged
before intersecting variant reference spans (0-based, half-open). Only GRCh38
samples receive satellite overlap counts. The reference satellite landscape is
not a personal repeat-length measurement; expansions require separate analysis.
The generated module contains only reviewed aggregate fields and records the
repeat-source SHA-256. These are dated snapshots, not live analysis progress.


## Streaming public whole-genome analysis

The public Watson original LS454 runs and Venter SRR7097858 paired reads are
catalogued by `scripts/wgs_catalog.py`. Publisher file sizes and MD5 values,
source identities, and the private storage plan live beneath
`/mnt/PILEDRIVE/genomics-workbench/wgs/`; this directory is outside Git.
Venter's two large mates are placed on the Inspiron HDD and Latitude SSD.
Inspiron is suitable for sequential input storage; final gathering, duplicate
removal, and heavy sort scratch are restricted to Latitude's SSD. Derived
BAMs remain distributed until a host passes a fresh scratch-space check.

`wgs_swarm_download.py` assigns downloads to six enrolled machines using signed,
renewable resource leases. A shared adaptive WAN budget backs off on archive
errors. Committed chunks have SHA-256 receipts; incomplete `.part` files are not
consumed. `wgs_stream_qc.py` processes committed chunks immediately and requires
whole-file publisher MD5, gzip integrity, and exact read counts before final QC.
A malformed input is quarantined rather than skipped.

`wgs_recall_swarm.py` maps verified Watson runs as they arrive, retaining both
short and long reads. `wgs_venter_stream.py` synchronizes mates and creates
500,000-pair shards while the larger files are still arriving. Its outputs stay
provisional until both complete sources pass checksums. A changed or invalid
source starts a new generation; old shard receipts cannot satisfy the new run.
Reference and tool images are verified before remote execution. Mapper CPU and
RAM allocations derive from available signed capacity; whole-human BWA mapping
requires at least 7,000 MiB. Smaller machines still perform downloads, QC, and
chromosome calling.

The persistent user services are `lifetech-wgs-download`, `lifetech-wgs-stream-qc`,
`lifetech-wgs-index`, `lifetech-wgs-pilot`, `lifetech-wgs-recall`,
`lifetech-wgs-finalize`, `lifetech-wgs-venter-stream`, and
`lifetech-wgs-venter-finalize`. Inspect them with `systemctl --user status` and
private progress JSON under `wgs/recall`, `wgs/downloads`, and `wgs/qc`. The
authenticated Task Manager `/task-manager` streams an operational projection
every five seconds through `/api/v1/swarm/wgs`; it exports counts and storage
allocation, without raw reads, genotypes, source URLs, or filesystem paths.

Finalization waits for all required sources, QC, and alignments, then removes
duplicates across runs and calls canonical chromosomes. Confirmed reference
positions require depth >=20 and explicit reference confidence RCQ >=20,
computed from genotype likelihoods with equal genotype priors. Variant calls
require caller GQ >=20. Missing confidence, ambiguous reference bases, and
uncertain calls remain unknown. All recorded alternate REF spans are excluded
from confirmed matches. Watson chromosome 19 is excluded from inference to
respect the original withheld APOE region; it remains in the genome denominator
as unknown.

Completed WGS results are private research aggregates pending review. These
services do not automatically replace the public Genome Explorer snapshot.


`lifetech-wgs-archive.service` maintains a complete source-read copy on Inspiron's
HDD at the private `wgs-archive` directory beside its worker cache. It copies
publisher-verified Watson/Venter files sequentially, resumes interrupted transfers,
and verifies both MD5 and SHA-256 before atomic installation. A 10 GiB disk reserve
is required. Local archive progress is `wgs/archive/state.json`.

An explicitly authorized private Julian supplement includes original and
normalized calls, structural/mitochondrial calls, annotations, analysis provenance,
reference-match summaries, the reference FASTA and index, and the original indexed
WGS BAM. A private manifest is built by `lifetech-julian-archive-prepare.service`;
`wgs_archive.py --private-manifest` incorporates it. Originals are retained. These
files and their detailed archive receipts are owner-only and never included in
the public operational projection or Genome Explorer.


## GPU compute and telemetry

CompAI has a 12 GiB RTX 3060; Inspiron also has a 2 GiB MX230. Both passed the
controller-signed CUDA dispatch/readback check. The mini-PC has an AMD Renoir
integrated GPU with 8 GiB reserved UMA memory. Its login user cannot open the
render node directly; GPU jobs must pass the exact render device and supplemental
render GID to a non-root container. A restricted, leased Vulkan dispatch/readback
probe verified hardware execution without changing host permissions. GPU memory
reservation is independent of active utilization.

Task Manager samples NVIDIA counters and integrated GPU activity. AMD reports its
driver busy counter and reserved GPU-memory usage. Intel percentages reflect the
busiest engine across visible DRM clients; other users' clients may be excluded.
Missing counters remain unmeasured, rather than being reported as zero. Software
renderers such as llvmpipe never count as hardware GPU execution.

The production BWA/samtools/bcftools commands remain CPU programs. Device exposure
does not accelerate them. Read QC now uses bounded Vulkan batches on the mini-PC through
`lifetech-gpu-qc.service`, with one persistent hardware context and automatic CPU
fallback per transaction. It computes base composition and all 94 Phred+33
quality bins. `lifetech-gpu-histogram-backfill.service` enriches previously verified
mini-PC sources without replacing their original QC provenance. The first two
files were independently CPU-checked for every bin in all 14 batches; later files
retain publisher checksums, exact read counts, and prior aggregate comparisons.
Task Manager reports backfill separately so repeat analysis does not inflate
primary read totals.

Tiled confidence-mask comparisons share the serialized worker. These operate on
explicit confidence evidence, preserve unknown positions and variant REF-span
exclusions, and compare reference status rather than alleles. The mask tools
produce private analysis results; they do not publish new chart values.
A new kernel needs exact CPU agreement and a benchmark including transfers and
setup before it is described as an acceleration.
Keep a persistent GPU context if it materially improves throughput, respect live
resource leases, and preserve other applications' GPU-memory headroom.

## Venter collection download service

The local huref2-download.service now reads a persistent collection.json under
~/Downloads/HuRef2.0. It keeps the three existing SUP BAMs at their original
paths, then queues four published assemblies, the Zenodo phased-variant/haplotype
support files, and the ENA HAC FASTQ. Old Illumina files stay in their existing
collection; they are not downloaded twice. Raw POD5/FAST5 is explicitly unavailable
until an actual public source is identified. The current collection is 15 files,
about 607.22 GB in total.

The running user installation uses copies of scripts/huref_download_service.py
and scripts/huref_download_status.py under ~/.local/opt/huref-download.
Its aria2 runtime negotiates MPTCP where supported, falls back to TCP, retains
segmented resume controls, and verifies publisher MD5s. Verified receipts are tied
to expected checksum, size, inode/device and modification metadata. Changed or
unverified files are rechecked on restart. Failed transfers are requeued after a
delay without restarting healthy transfers. Storage admission was checked for the
current collection, and the supervisor pauses active transfers below a 30 GB
free-space reserve.

Commands: huref2-status --watch, huref2-status --datasets, and
huref2-status --all. The existing systemctl --user controls still apply.

Use j/k or n/p to page through the watch view. Closing it leaves the service
running. Per-file ETA is shown for active transfers; queued files have no invented
completion estimate. Overall ETA excludes final integrity verification. Durable
state and receipts also allow the display to show saved progress while offline.
The collection-md5checksums.txt manifest can be used for an independent later
checksum audit from the download root.

Focused verification:
python3 -m pytest -q scripts/tests/test_huref_download_service.py


## Signed Venter peer QC

`scripts/venter_peer_deploy.py` enrolls a fixed set of authenticated machines in a
signed run executed by `scripts/venter_peer_qc.py`. The controller retains its
signing key; each worker generates its own identity key. Once provisioned, peers
schedule work and exchange verified inputs, signed receipts, and result files
directly over mutual TLS without a running controller scheduler. Placement and
fallback decisions are recorded in `events.jsonl`; partitions can cause duplicate
execution, and differing output hashes are flagged rather than silently replaced.

Each node keeps its run under `~/.local/state/venter-peer-qc/<run-id>/`. The signed
manifest records recipes, input identities, image IDs, resource limits, and node
identities. Original BAM QC runs on the machine holding the BAMs. Other inputs
use resumable transfers with fixed 1 MiB buffers and MD5 verification against the
publisher inventory. Downloads and cached inputs are retained. These are ordinary
TLS transfers; no RDMA transport is implemented.

The native launcher at http://127.0.0.1:8791/launcher exposes **Join swarm**,
**Pause new jobs**, and **Worker mode**. Automatic mode accepts work on AC power;
Always also accepts work on battery. The Jobs section shows the signed peer QC
service and provides an explicit stop action. Pausing admission lets accepted jobs
finish. Joining starts an existing valid signed peer run if it was stopped; it
does not create a new authorization or enroll an unknown machine automatically.
Both native launcher and peer QC services are enabled as user systemd services.

## HuRef2 DAT1/SLC6A3 VNTR check

The priority task `huref2-dat1-vntr` uses public HuRef2 inputs. Its primary assembly
(GCA_059792245.1, CM186513.1) has a DAT1 3′-UTR repeat at zero-based coordinates
1,404,324–1,404,728 that exactly matches the GRCh38 repeat annotated as 10 copies
by UCSC simpleRepeat. The 404-bp annotated interval includes repeat imperfections;
it should not be naively divided by 40 to assign a fractional biological allele.

CodeCollective checked unique flanking sequences and exact repeat identity; CompAI
independently checked a published primer pair in silico. CodeCollective also
verified the publisher MD5 and scanned all 2,941,282 records of the phased VCF,
finding no overlapping locus call. The supported result is **10R in the assembled
sequence**. The second allele and a diploid 10R/10R genotype remain unresolved:
absence from a heterozygous-only VCF does not establish reference homozygosity.
No ADHD diagnosis or phenotype is inferred.

The workers used existing signed resource leases, pinned local images, one CPU,
256 MiB, and containers without network access. Only a small public locus payload
was streamed for cross-checking; the full VCF audit used the worker's verified
cache. Task transitions, the timed-out worker attempt, successful retries, and
results are retained in the transactional compute journal. Recipes are in
`scripts/dat1_vntr_worker.py`, `scripts/dat1_vntr_swarm.py`, and
`scripts/dat1_vntr_vcf_audit.py`. CompAI keeps evidence under
`~/.local/state/swarm-control/analyses/huref2-dat1-vntr/`; this laptop keeps the
report and extraction artifacts under `~/Downloads/HuRef2.0/analysis/dat1-vntr/`.

## Transfer and memory hierarchy measurements

Use `scripts/swarm_transfer_audit.py` for read-only NIC DMA masks, checksum and
segmentation offloads, RDMA devices, NUMA topology, page-cache headroom, storage
read-ahead and Linux pressure-stall metrics. A DMA mask is a capability report,
not a DMA traffic counter. All five enrolled workers exposed a single NUMA node
and no RDMA device in the 2026-10-31 audit. Ordinary NIC drivers manage DMA;
Python TLS still performs CPU encryption and copies. No zero-copy or RDMA claim
is made. CPU affinity and huge pages are not justified by these measurements.

`scripts/swarm_transfer_benchmark.py` reads at most 64 MiB of an allowed public
object through the existing pinned mTLS peer connection and compares the prefix
SHA256 with a source-derived expected digest. It uses one reusable buffer and
compares 64 KiB, 256 KiB and 1 MiB reads. This is an end-to-end warm-cache sample,
not a cold-disk or maximum-link benchmark. On 2026-10-31, the Latitude → CompAI
Ethernet path achieved 908–911 Mbps, with no meaningful buffer-size advantage.
CodeCollective → Latitude achieved 21–27 Mbps with CodeCollective Ethernet down.
Larger buffers or RDMA emulation would not fix that wireless bottleneck.

Keep the existing 1 MiB transfer buffers, sequential buffered reads and Linux
page cache. Do not flush cache, lock entire genomes into RAM, or raise global
read-ahead speculatively. BAM processing stays on its source NVMe machine and
uses record-bounded streaming, avoiding whole-file RAM residency and large
inter-node copies. The measured BAM container used ~26 MiB, with zero current
memory PSI and no swap-in/out during a short vmstat sample. Swap occupancy alone
is not evidence of current paging. Inspiron has a rotational root disk, high I/O
PSI and a 100 Mbps Ethernet link; that is a separate placement/storage constraint.
Raw measurements and SHA256-verified transfer checks are retained under CompAI's
owner-only `~/.local/state/swarm-control/analyses/swarm-memory-transfer/` and in the
transactional compute event journal. Running jobs are not restarted for auditing.
