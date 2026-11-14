# Harbor Genetics checkout migration — 2026-11-13

The canonical repository directory is `HarborGenetics/`, with origin
`https://github.com/juliancoy/HarborGenetics.git`. Workspace imports, relative
symlinks, scientific recipes, JSwarm source snapshots and test fixtures use this
name. The local website entry point is `scripts/harborgenetics_site.py` and its
user service is `harborgenetics-site.service`.

A temporary ignored `GenomeHarbor -> HarborGenetics` alias supports processes
started before the migration. It is not required by new committed snapshots.
JSwarm still validates and materializes historical manifests containing the old
repository key so existing publications remain available for rollback. New
publications use `HarborGenetics` and adjacent physical worktrees.

Validation uses host tests and local Docker. The release/source tests passed
16 cases initially; the new legacy rollback test exposed that its synthetic
Organization checkout needed to ignore the historical directory too. That
fixture was corrected and its focused test passed. Commands, dates, content and
environment fingerprints and results are in adjacent local test logs.
The exported Harbor Genetics browser check passed desktop/mobile navigation,
production controls and saved trajectory loading. Fleet publication runs its
normal frozen-source gate before rollout.
