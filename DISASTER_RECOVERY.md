# Local disaster recovery and testing copies

The DR archive is a complete-data recovery source, separate from the running local
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
WebSocket connections only, which clients re-establish. In-flight push queues are
not copied: durable outboxes must be reconciled before replay. Other unhandled
bindings are explicitly reported as gaps. External calendars, Gmail/provider copies,
and third-party systems are outside these workspace backups. Published dependencies
are rebuilt from lockfiles; deployed static assets are rebuilt from source.

## Testing copy

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
expired testing-copy cleanup. It runs while the user service manager is active;
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
