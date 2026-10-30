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
