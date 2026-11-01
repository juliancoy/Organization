# Run-manager streaming inputs

The run manager supports `auto`, `stream`, and `cache` input modes. BAM read-QC
is a sequential recipe: it can parse BGZF/gzip members and BAM records from
stdin while receiving the file. It does not require a complete BAM cache, BAM
index, or seeking. Recipes requiring random access retain their seekable input
cache; an explicit incompatible streaming request fails before dispatch.

`auto` selects streaming for supported sequential recipes. Concurrent inputs
are bounded by the requested parallelism and available shared CPU/RAM admission.
Each QC stream reserves two CPU cores and 2 GiB of RAM; transfer buffers are one
MiB each, and the BAM parser bounds individual records at 64 MiB. A full BAM is
never accumulated in RAM or written to a receiver-side temporary input file.

```
python -m scripts.run_manager plan --root /private/existing-authorized-run
python -m scripts.run_manager start --root /private/existing-authorized-run \
  --mode auto --parallelism 2
```

The existing public-QC deployer invokes this planner automatically for BAMs;
`--input-mode cache` preserves the previous cache execution path. Signed
manifests authorize the source, receiver, exact input identities, code versions,
concurrency and endpoint. The controller signing key never leaves its host.
Workers continue using their existing pinned mTLS identities and local shared
resource pool. Generated source, executor and placement units are scoped per run.

Input serving runs separately from source-side compute scheduling. An active
source scan finishes normally. Once its compute executor becomes idle, the
manager reconciles placement so it cannot also execute delegated jobs. The
input server continues serving the streams during that transition.

Delegated streaming jobs appear inside the original run, with receiver, input
bytes and individual states. Streaming progress is not a verified result:
completion requires the entire compressed input's expected byte count and
publisher MD5, successful gzip CRC and BAM parsing, and a signed result receipt.
Saved receipts bind run, parent, input, node, authorized code and result digest.
Source files remain unchanged; existing completed results remain reusable.

Transient network failures restart parsing from the beginning, because this
version does not checkpoint BAM parser/hash state. Durable attempts use the
existing SQLite retry ledger and stop after three execution attempts; capacity
waits do not consume attempts. Invalid data or authorization is never published
as a completed result. A streaming worker restart does not restart source-side
scans, the coordinator, or Swarm Control conversations.

Transfers use normal NIC DMA through encrypted TCP/TLS. The current Ethernet
adapters expose no RDMA interface; this is not a direct application-memory
RDMA or zero-copy implementation. Simultaneous streams share the link bandwidth.

New streaming delegations enable a private, per-host shared block cache. Each
4 MiB block is keyed by publisher MD5, object length and byte offset, with a
SHA-256 integrity check on the stored block. Fixed lock stripes coalesce
concurrent fetches across run-manager processes. The OS page cache is the RAM
tier; no separate Python whole-input RAM copy is retained. The disk tier uses
LRU eviction, a 2 GiB budget and 1 GiB free-space reserve. Inputs larger than the
budget stream through the rolling cache. A cache miss fetches an exact range
from the signed mTLS source; a disk-space shortage bypasses persistence.
Each consumer still checks the full publisher MD5 and BAM CRC before publishing.

Cache identity and reuse span runs on the same receiving host. This is not
shared physical RAM across hosts, nor a new arbitrary peer-discovery service.
Active delegations retain their signed policy
and are not restarted to enable caching. Cached private blocks persist until
LRU eviction; the cache directory is owner-only.

Signed delegations now advertise cache endpoints for enrolled receiving peers.
Workers try local blocks, then up to three authorized cache peers ordered by
observed response time, then the original source. Misses and failures put a peer
on a 60-second cooldown. Cache services only return already-resident blocks and
never forward misses, avoiding loops. Exact ranges, pinned mutual TLS identities,
run expiry and authorized object lists constrain serving. Module hashes authorize
the cache and peer-selection implementation. Each consumer still validates the
full input before publishing a result. There is no hardware shared-address-space
or RDMA requirement, and no promise of a single physical copy across machines.
Versioned service runtimes preserve the code used by older active delegations.

Machine updates use `scripts/deploy_swarm_inputs.py`: it reuses the standard
`LauncherUpdates` pinned SSH/agent identity checks and idle-only launcher
activation. Companion input modules install atomically under the enrolled
configuration's `input-releases/<digest>` with `current` and `previous` pointers.
Every module is checked before activation; existing jobs retain immutable code.
Run input services consume those standard releases instead of copying arbitrary
source into workers. `--watch` retries busy/offline machines and saves a per-node
receipt to `~/.local/state/swarm-control/input-deployment.json`.

The Julian/HuRef2 comparison uses chromosome-sized seekable bundles rather than
whole BAMs. Public assembly/reference bundles use the shared range cache; private
Julian chromosome shards stream over pinned encrypted SSH into no-swap container
tmpfs and bypass the worker disk cache. Container leases reserve CPU and data-sized RAM estimates, renew while running,
and are released only after container cleanup. The comparison has no hard Docker
RAM ceiling; its reservation is soft. Tmpfs size follows actual host capacity,
and the compressed private input pages are pinned resident.
Each output is bound to public/private input hashes, worker identity, immutable
image ID and scientific worker hash in an owner-signed receipt.

The current comparison phase is explicitly limited to intrachromosomal primary
MAPQ60 assembly mappings and reported diploid SNP calls with supported Julian
PASS DP/GQ20 evidence. Missing calls are not treated as reference agreement.
Genome-wide mapping uniqueness, indel/haplotype equivalence, complete shared
callability, phase, SV and CNV comparison remain separate validation gates.

Comparison work items now partition query chromosomes into disjoint ~40 Mi-base
regions, using ~10 Mi-base alignment tiles within each region. The same public
bundle identity is reused across regions, allowing shared block-cache hits.
Private chromosome inputs bypass that disk cache. Region coordinates are bound
in the signed result receipt; overlapping target mappings across regions are
excluded during aggregation using owner-only hashed site classifications.
Worker concurrency follows available cores and shared resource admission, and
there is no fixed per-job runtime timeout. Reservations are calculated from the
reference/query lengths, compressed inputs and the selected query span instead
of being clamped to 4 GiB. Reference indices are still chromosome-scoped.

### One launcher and Docker-backed workers

Each enrolled machine runs one launcher supervisor and one agent child. Shared
file locks in the enrolled configuration directory reject duplicate native or
container launches. The files remain in place; the operating system releases
the locks when the processes exit.

Worker containers use `genomics-<job ID>` names and the labels
`org.lifetech.worker=true`, `org.lifetech.job`, and `org.lifetech.group`.
Docker is authoritative for running workers. Capacity comes from their configured
CPU, memory, and PID limits. The shared admission lock and short pending-start
reservations prevent simultaneous requests from promising the same capacity.
Reservations are replaced by Docker accounting when the containers start;
there is no running-worker lease journal, expiry timer, or lease service.
Native jobs continue using the shared resource pool and are not counted twice.

A launcher restart leaves these Docker workers running. The replacement discovers
them by label and inspects their limits. Updated controllers skip renewal threads;
legacy `resource.renew` calls remain compatible as worker-status checks. Workers
end when their task completes or the owner explicitly stops them. Agent updates
still defer active test-coordination sessions, browser sessions, uploads, and
pending starts, whose orchestration remains attached to the agent process.

Use `python -m scripts.worker_maintenance status` to inspect the fleet. `drain`
closes admission while current workers finish. After verifying the selected
machines have the expected revision, `resume` restores the saved worker policies.
The standard SSH deployer installs supervisor changes for native and container
launches. Worker isolation stays in Docker; no additional network or scheduler
service is needed for this lifecycle.

### HuRef comparison follow-up queue

`python -m scripts.huref_followup_swarm --original <completed-comparison> --folder <follow-up-folder>`
persists its job specifications and dependency edges in `prepared/ready.json`,
operational state in `state.json`, and signed receipts beside owner-only results.
It retains the original mapping evidence and exposes operational progress through
the existing comparison task registry. Representation checks depend on the
matching native-coordinate normalization shard. Completed work is reused only
after validating its signature, output digest, input identities and recipe digest.

Native-coordinate work uses 40 Mi-base windows and shared public chromosome
objects. CPU/RAM admission still controls concurrency; RAM reservations are soft.
Rootless Docker workers receive a finite memory-lock setting supported by their
host. Jobs whose private inputs cannot fit that locked-memory allowance run on
other machines, rather than allowing private pages to become swappable.

Published HuRef heterozygous variants do not supply genome-wide reference
confidence. Native normalization, source-confidence audits, structural-call
inventories and signed-result audits can run immediately. Full comparable-region,
reference-agreement and downstream reporting stages remain explicitly waiting
for the missing validated evidence; integrity audits do not establish biological
reproduction or complete genotype agreement.

### HuRef Ethernet and RAM path

`Comparison.serving()` stages checksum-pinned **public** chromosome bundles on
Latitude's NVMe and makes Latitude the signed mTLS origin. Consumers use enrolled
LAN addresses and reuse authenticated HTTP connections for bounded range reads.
The comparison's shared input cache lives in owner-only `/dev/shm/swarm-inputs-<uid>`
with a maximum 512 MiB budget per node and a free-memory allowance. It never falls
back to a disk cache. Private chromosome shards retain their existing locked
container-RAM path; outputs and verification receipts remain durable.

Single-threaded normalization and evidence recipes reserve one CPU per job;
minimap2 alignment retains its requested thread count. The shared CPU/RAM pool
limits how many independent shards run simultaneously. Native Vulkan QC sends
bounded batches directly over its owner-only Unix socket (`qc-inline`), without
batch files, and overlaps parsing with GPU requests. GPU acceleration is for
validated QC kernels; this does not claim GPU alignment or genotype comparison.

Physical connectivity still matters: a node with no Ethernet carrier remains
eligible over Wi-Fi, and a 100 Mb/s adapter cannot supply gigabit throughput.
Inspect the actual route and link speed before interpreting a throughput result.

FASTQ workers consume `.fq.gz` and `.fastq.gz` directly. Sequential QC and
paired-read sharding decompress bounded buffers in memory; paired shards remain
gzip files. Venter mapping passes gzip mates directly to BWA. Watson mapping
retains both short and long reads in gzip intermediates and passes those to
BWA-MEM and BWA-backtrack, avoiding full plaintext FASTQ copies. The splitter
must finish successfully before alignment starts; malformed FASTQ or a failed
gzip CRC aborts that stage. General ZIP archives are not FASTQ gzip inputs.
Random-access reference and alignment stages still require their indexed,
seekable formats; gzip support does not remove those requirements.


HuRef reported sequence-allele comparison (2026-11-01):
`scripts.huref_projected_swarm` extends the completed SNP subset with 86 bounded
common-reference SNP/MNV/indel comparison shards. `huref-projected.service` runs
and resumes the owner queue under `huref-comparison-20261031/projected-20261101`.
HuRef inputs stream from Latitude through the signed mTLS RAM-cache path; Julian
chromosome gVCFs originate on CompAI and stay on the authenticated worker path.
Projection requires primary MAPQ60 contiguous mappings and matching GRCh38 REF;
both samples are left normalized without splitting their diploid genotypes.
Julian calls require PASS and DP/GQ at least 20. Unreported HuRef calls remain
unknown. REF mismatches, ambiguous mappings, symbolic alleles, alleles over 10kb,
and incompatible representations are excluded. Cross-shard target collisions
are excluded during report reconciliation. Whole-genome diploid callability,
phase agreement and complex SV equivalence remain separate evidence limitations.
Scoped owner reports, public operational summaries and signed provenance archives
are generated after the runnable shards finish; these reports do not require
unsupported whole-genome conclusions. Sequence allele comparison uses CPU tools.

HuRef combined-evidence acceptance tests:
`tests/test_huref_evidence.py` is a synthetic, swarm-safe workspace test file.
It needs Python/pytest only and never accesses owner data, Docker sockets or the
network. The normal published-file workspace catalog discovers it; focused runs
select that path. It guards useful assembly comparisons without VCF records,
local diploid haplotype evidence without chromosome-wide phase extrapolation,
uncertainty attributed separately to HuRef/Julian/mapping, strand and tile
coordinates, and ambiguity/gap exclusion. The projected worker now also compares
HuRef assembled bases at explicit confident Julian SNV sites independently of
HuRef VCF presence. Its output distinguishes assembly allele membership from
reported diploid genotype agreement. Report reconciliation excludes cross-shard
collisions for each evidence class separately. Docker integration tests in
`tests/test_huref_projected.py` verify the actual combined worker with an empty
HuRef VCF; those tests run locally in the genome-tools image. Existing running
comparison jobs retain their signed worker recipes; source changes apply to
subsequent recipe versions rather than silently mixing results.
