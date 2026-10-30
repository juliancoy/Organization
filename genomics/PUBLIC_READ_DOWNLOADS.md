# Single-copy public read downloads

For a public archive file whose chunk-plus-assembly peak would not fit a peer,
use `scripts.wgs_single_copy_download` with an exact publisher manifest and one
explicitly selected enrolled peer. It reuses authenticated discovery, pinned SSH
identity, renewable CPU/RAM leases, Docker limits, cancellation cleanup and the
existing verified-receipt contract. It does not change other download jobs or
network-control files.

```
python -m scripts.wgs_single_copy_download \
  --manifest /private/manifest.json --hosts /private/hosts.json \
  --host julian-inspiron-3793.local --output /private/download-state
```

The controller applies the archive HTTPS allowlist, exact file length and MD5
validation before execution. The worker requests up to eight exact 128-MiB
HTTP ranges, writing them into one sparse `.part` file. Each completed range is
fsynced and recorded with SHA-256 in `ranges.json`; saved ranges are rechecked
before resuming. Whole ranges from a previous sequential prefix can be imported.
An unfinished range may be downloaded again. No second assembly copy is created.
The existing sequential transfer function remains available for regression
coverage and prefix migration.

`progress.json` distinguishes completed-range bytes from in-flight received
bytes. The partial file's logical length is reserved up front and must not be
used as a completion measurement. Only a full publisher-MD5 match produces the
final filename and verified receipt, which also records SHA-256. Failed
checksums retain the partial file and produce no verified receipt. Permanent
failures use exit 78 so an operator service can prevent automatic retry loops.
All manifests, peer details, progress, receipts and downloaded reads stay outside
Git. Active Swarm Control services need no restart.

Transport timeouts, lost renewable leases and temporary capacity shortages use
exit 75 and may restart with bounded service backoff. Identity, permissions and
integrity errors retain exit 78. Services should retry temporary failures without
a finite start-rate lockout; completed ranges are verified again on resume.
Lease renewal reconnects its authenticated client before giving up, and server
expiry still enforces resource ownership if remote cleanup is unreachable.

For paired Venter processing, stage both full publisher-verified sources over
SHA-checked pinned SSH before parsing. A complete set of old chunk numbers is
not evidence that a mirror matches a repaired source. Retire provisional mapping
generations when stale mirrors are found; preserve their evidence. Finalization
uses generation-specific scratch, reports and summaries, and cannot reuse a
previous source generation. Completed chromosome summaries survive transport
cleanup failures and are recovered before scheduling new extraction work.

Verified archive staging resumes an interrupted `.partial` LAN transfer from
its saved byte offset. It hashes that prefix plus all newly transferred bytes
and publishes only after the complete expected SHA-256 matches; a corrupt
prefix is quarantined. Existing source reads and retired generations remain
intact. Rechecking saved download ranges emits a progress heartbeat so slow disk
validation is distinguishable from a stopped worker.
