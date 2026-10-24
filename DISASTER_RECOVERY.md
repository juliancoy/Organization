# Local disaster recovery and testing copies

The DR archive captures the full configured D1/R2 data inventory, separate from the running local
test deployment. `local-sync` makes a scoped testing copy from an archive. Neither
command writes to production. The public organization replica continues separately.

## Disaster recovery

Install the workspace Python requirements and OrgPortal's existing Node dependencies.
Use the host's Cloudflare operator login for D1 exports and Worker configuration;
PIdP/OrgPortal website grants cannot export an entire database. R2 uses
`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, and `R2_SECRET_ACCESS_KEY` from the host environment
or the existing `PIdP/.env`. Credentials are never mounted in testing containers.

```sh
./disaster-recovery inventory
./disaster-recovery backup
./disaster-recovery status
./disaster-recovery verify
./disaster-recovery restore --target /mnt/PILEDRIVE/recovery-drill
```

The default archive location is `/mnt/PILEDRIVE/organization-dr`, configurable with
`ORGANIZATION_DR_DIR` or the global `--store` option. Directories are private (0700),
files are private (0600). Archives include private account data and local secret
configuration; filesystem permissions do not encrypt the drive. Keep access limited
to the operator and use volume encryption when physical-loss protection is required.

Each completed snapshot includes:

- Full production OrgPortal, PIdP, chat, and independent journal D1 exports, including
  private data. SQL exports use lossless gzip compression. Each export is rebuilt
  into an isolated SQLite database and checked for integrity, foreign-key violations,
  and per-table counts. The rebuilt database is temporary; testing copies and restore
  drills reconstruct it when needed instead of retaining a second full representation.
- All configured production R2 bucket objects, original keys, metadata, SHA-256,
  sizes, and provider versions. Objects use content-addressed storage across snapshots.
  Interrupted downloads resume using a private catalog; changed objects are fetched
  again. Conditional reads and a second listing detect concurrent bucket changes.
- Git history, the current working tree including uncommitted source changes,
  available local environment configuration (including `.env.pidp` and
  `.pidp.env` when present), and deployed Worker source/settings.
- A consistent online copy of the local OrgPortal SQLite stores, preserving locally
  authored newsletter history, plus the local PIdP PostgreSQL dump when those default
  containers are running. Verification actually restores PostgreSQL into a disposable
  container with no network or published ports, then removes that container and volume.

A manifest records coverage and gaps. A snapshot becomes `latest.json` only after
the restore and hash checks pass. To resume an interrupted backup:

```sh
./disaster-recovery backup --resume .pending-YYYYMMDDTHHMMSSZ
```

An export is reusable only after its successful completion hash was saved. Partial
snapshots never replace the last verified snapshot. Database snapshots have separate
capture times; there is no transaction spanning all services and object buckets.

Object bytes are deduplicated by their SHA-256 across buckets and generations.
Already compressed media and jobs archives are kept byte-for-byte; recompressing
them provides little benefit. Identical snapshot files share storage using hard links
after matching and verifying their hashes. Deterministic gzip headers let unchanged
SQL and source archives match across runs. Treat completed archives as immutable:
editing a hard-linked file would affect every generation sharing that file. Deleting
a generation does not remove the file bytes needed by another generation.

`restore` writes to a new, empty directory. It reconstructs databases, object files
with an original-key index, local stores, source, and configuration. It neither
uploads restored data nor starts services. Test in isolation, reapply verified
deletions and expiry, reconcile outboxes, reconnect external providers, and validate
permissions before a separately reviewed production cutover. Local PIdP PostgreSQL
and deployed PIdP D1 have different schemas: restore each into its own native runtime.
Do not replace a running PostgreSQL database with the D1 export.

## Recovery limits and secret escrow

### Recovering and maintaining the active secret escrow

`python3 scripts/recover_production_secrets.py --if-needed` maintains
`~/.config/organization-recovery/production.json` and its version-linked
`proof.json`, with owner-only permissions. It recovers the existing values; it
does not rotate encryption or integration keys. The scheduled backup attempts
this refresh before capture. If a deployed version changes and cannot be verified,
the backup retains its data and reports a secret-recovery gap.

For the organization and identity Workers, an authenticated, ten-minute recovery
handler is uploaded as a zero-traffic version. It encrypts the values using
AES-GCM and a host RSA-OAEP public key. Only the host private key can decrypt the
response. Version and one-time challenge checks bind the response to the recovery
request. Original source is restored as the latest uploaded version and the
recovery version is deleted. Production traffic remains on its original version.
The peer Worker has a Durable Object and cannot expose version URLs; its existing
local signing-key candidates are instead verified using a read-only signed-ticket
check, with an invalid-signature control. No registration or notification is sent.

Encrypted envelopes and the private decryption key are also in that private host
directory. Keep any independent copy of `private.pem` protected separately from
the encrypted envelopes. The ordinary private DR archive includes the verified
plaintext escrow for service restoration; dev copies never include that escrow,
operator configuration, or the host recovery key. Routine backups compare secret
fingerprints and active version IDs before marking production validation complete.

Cloudflare does not return deployed secret values. A local `.env` with the same
variable name is not proof that its value matches production. Secret names and
missing/unverified local copies are reported as gaps; preserve known production
values in an independently protected operator escrow. Account access/2FA recovery,
provider credentials, encryption keys, and signing keys must be recoverable before
claiming complete service recovery. Never commit the escrow or private archives.

To include an existing operator escrow in the archive, use
`./disaster-recovery backup --secrets-file /private/path/production-secrets.json`.
Its format is a JSON object mapping Worker names to objects of secret names and
string values. `ORGANIZATION_DR_SECRETS_FILE` supplies the same option for automated
runs when configured in the service environment. Values stay in the private archive;
name coverage is recorded without logging values. Coverage is not proof that the
provided values match production; confirm them during an authentication restore drill.

Chat messages and memberships are in D1; the current chat Durable Object holds live
WebSocket connections only, which clients re-establish. Launcher Durable Object
registrations are exported separately through a short-lived, object-scoped backup
ticket. This read-only export retains credential hashes, revocation state and
the original expiry; it never returns device credentials. Original room mappings
are retained for new/renewed registrations. Historical registrations without a
room mapping are flagged for namespace preservation or operator re-enrollment.
Registration files are hash-verified, included in local restores and available
in the private dev baseline. Restore review must reapply revocations and expiry.
Socket connections and expiring ICE credentials are re-established. Other unhandled
bindings are explicitly reported as gaps. External calendars, Gmail/provider copies,
and third-party systems are outside these workspace backups. Published dependencies
are rebuilt from lockfiles; deployed static assets are rebuilt from source.

## Testing copy

### Full development data

`./dev-data refresh` creates a full, verified database and object baseline from
the latest DR snapshot in `/mnt/PILEDRIVE/organization-dev`. It includes every
table in the four captured production databases and every captured R2 object,
including private records. Database table counts, integrity and object hashes
are checked. Media has independent file inodes; editing a dev file cannot alter
the DR archive. Database baselines and shared dev media are read-only.

```
./dev-data refresh
./dev-data checkout
./dev-data status
./dev-data shell org
./dev-data shell pidp
./dev-data shell chat
./dev-data shell org-journal
```

`checkout` creates separate writable SQLite databases, preserving every existing
workspace. The SQL shell runs in a container with no network or published ports,
no production environment or operator credentials, writable dev databases, and
read-only media and original-key bucket indexes. Identity/session rows in these
full copies remain private production data: use this offline shell rather than
connecting the databases to production services. This is a complete captured-data
workspace, not an automatically configured replacement application deployment.

Successful daily DR capture now refreshes the full dev baseline automatically.
Existing writable checkouts never change during a refresh; explicitly create a
new checkout when ready to adopt the newer baseline. Dev copies expire after
seven days and the daily job purges expired copies and unreferenced dev media.
DR archives remain separate and durable. Authenticated working copies retain
their reviewed API-based production writes; dev databases have no writeback path.

The backup also discovers application-key candidates in local `.env.cloudflare`,
`.env`, `.pidp.env` and `.dev.vars` files. Candidate values are archived privately
under `secrets/local-candidates.json`; conflicting values and their source paths
are recorded by name. Candidates are never marked production-validated merely
because a local name matches. `.env.cloudflare` operator keys can export the
databases and Worker settings, but cannot read deployed application-secret values.
Email binding configuration is included in Worker settings; email provider state
is external. New push jobs persist in the private `push_outbox` D1 table before
enqueue, with stable recipient/event IDs, leased consumers and bounded cron retries.
Delivery or suppression immediately clears private content; pending content expires
after 23 hours and minimal deduplication hashes expire after seven days. The backup
marks recoverable queued work only after confirming the deployed health capability,
its exact Worker version and the captured outbox table. Raw transport queue contents
are not exported, and historical transport-only jobs cannot be reconstructed.
Restores must review expiry, deletions, consent and delivery receipts before replay;
they remain network-isolated by default. See OrgPortal's `docs/privacy/PUSH_OUTBOX.md`.

For missing keys, obtain the original values from the deployment operator or secret
manager and supply the existing `--secrets-file` escrow option. Validate those
values in an isolated authentication recovery drill. If originals are irretrievable,
plan coordinated rotation across both ends of each integration; preserve encryption
keys needed for existing encrypted records before rotating anything. Future secret
changes should update the protected escrow as part of the same release.

The default target is `local-community-api`, the workspace's read replica.
Use `--container NAME` only for another local replica; writable fixture stacks
are rejected. The local storage backup command uses the same default target,
and scheduled DR capture includes this replica's local SQLite stores when it
is running.

Keep writable fixture stacks on their own Docker database volumes. Existing
fixture volumes are not reset or certified synthetic by `local-sync`; create
a fresh disposable fixture stack when a clean synthetic dataset is required.

```sh
./local-sync --org lifetech --include-private --dry-run
./local-sync --org lifetech --include-private
./local-sync --org lifetech --include-private --from 20261023T000000Z
./local-sync --purge-expired
```

The command accepts only this workspace's host-local Docker replica. It backs up the
local stores, applies existing local migrations, and imports one organization's
configuration, published contact profiles, memberships, personnel configuration,
invitations, conduct records, hosted events, and registrations. It deliberately
excludes authentication secrets, grants, provider credentials, email delivery jobs,
financial ledgers, private contact profiles, and account-private newsletter archives.
Use the existing account-authorized newsletter sync for newsletter testing.

Original identities and roles are retained; a separate synthetic local administrator
receives local access. The replica configuration disables production background jobs
and rejects normal application mutations. It is appropriate for viewing realistic
data. For write tests, use a separate disposable fixture deployment with synthetic
identities and delivery adapters; do not turn a private replica into a production
writer or reconnect it to production services.

Imports run in one SQLite transaction. Refreshes remove only previously tracked
snapshot rows and preserve unrelated organizations/local newsletter stores. Changed
local rows or independent local references stop cleanup instead of being silently
overwritten. Imported private rows have a seven-day testing-copy clock. Cleanup
removes expired tracked private records; shared/public organization configuration is
retained, and shared public profiles are retained while another membership uses them.
The snapshot tracker is local-only metadata and is never sent upstream. Private
temporary import files are removed after use. Source timestamps and relationship
expiry are preserved; a testing import never grants production permissions.
Import and expiry operations suppress the affected local journal triggers inside
their transaction, then restore them. Temporary snapshot data does not enter new
immutable business history; existing backups/history still require deletion review.

## Daily operation, retention, and deletion

```sh
python3 scripts/setup_recovery_timer.py
systemctl --user status organization-dr.timer organization-dr.service
journalctl --user -u organization-dr.service
```

The user timer runs at 03:15 America/New_York, with up to five minutes of jitter and
missed-run catch-up. It runs the full backup and restore verification, then attempts
expired testing-copy cleanup and refreshes the complete dev-data baseline. The
installer enables user lingering so the schedule also runs after logout;
offline containers or cleanup conflicts require operator attention. It does not
extend authentication grants or silently open a browser when credentials expire.
Check `latest.json`, manifest gaps, and service failures; a verified data copy with
gaps is not a completed production-recovery drill.

DR copies are intentionally durable and are not removed by testing-copy cleanup.
The existing policy targets a 30-day backup window; this tool does not yet delete
old generations automatically. Operators must review older generations and remove
them under that policy. Object blobs can be shared by generations: do not delete a
blob still referenced by any retained manifest or an in-progress backup. Verified
erasure requests must cover the local test database, every archive generation and
source/journal copies; backups must have deletions reapplied before any restoration
serves traffic. If the deletion ledger is unavailable, keep a restored service isolated.

## Copies on the distributed test devices

The test master's authenticated `POST /api/v1/master/backups` replicates the latest
complete DR generation to every enrolled device, keeping current public dataset versions. Historical public dataset versions remain in the full PILEDRIVE snapshot; private records, media, credentials and source code are retained in every encrypted replica. See `lan-monitor/MASTER_API.md`
for status, retries and restoration. These are encrypted recovery replicas, not
working copies or test fixtures. Keep the recovery RSA private key independently
protected offline; it is deliberately excluded from the remote copies.
