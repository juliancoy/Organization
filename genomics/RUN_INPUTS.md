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
