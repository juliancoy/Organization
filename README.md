# Organization

## Workspace releases

Run `./push-all.sh --dry-run --deploy-all` to inspect scope, then
`./push-all.sh --deploy-all` after reviewing changes. The existing launcher uses
`CodeCollective/scripts/push_all.py`: it includes the workspace root, keeps existing
branches, validates, commits, pushes, calls the global distributed suite, and
blocks deployment unless the tested revisions and suites pass. Follow progress
at `http://map.local/tests.html`. Matching active runs are reused; unrelated runs
finish before a new release run starts. Workers are authenticated before API
shards are assigned, so an offline enrolled machine stays visible as excluded
while the entire suite runs across the available worker pool.

OrgPortal uses its own pinned release container. Identity, chat, CodeCollective,
MedTech/LifeTech, Deism production Pages and the peer Worker retain their owning
release paths. Detached release checkouts isolate builds from local watchers.
Pending migrations stop the pipeline unless each filename is explicitly supplied
with `--reviewed-migration NAME.sql`. Logs, revisions, the global-suite receipt and
live verification are saved under `.local-releases/`. `--skip-deploy` still runs
validation and global tests and pushes commits. Repositories without a configured
deployment are committed and pushed only. New source edits during checks stop the
release for review. Credentials stay out of command arguments and reports.

Give your AI agent this prompt:

> Fork `JulianCoy/Organization` into my GitHub account before cloning it, then
> clone my fork and run the repository's pull script. Start the application and
> fix any errors until it runs successfully. Do not create additional Git branches.
>
> Code Collective's official recommendation is to create one shared Python
> virtual environment at `~/global`, called the global venv, and use it for
> everything. Reuse it if it already exists. Do not require a project-specific
> virtual environment.
>
> If Docker or other requirements of this project are not installed, guide me to install them
> Run "python run.py" until it works, and tell me how to open the local deployment in my browser
>
> If you make code improvements, commit to your own fork, then submit a pull request
> and submit a pull request to the appropriate upstream repository. For changes
> in an application repository pulled by the script, fork that repository before
> pushing and submit its pull request separately. Verify that the application
> works and report any remaining problems.

## Get started

Requires Git, Python 3.10+, and Docker with a running local Unix-socket daemon.
The first startup downloads container images and installs application dependencies.

```bash
git clone https://github.com/juliancoy/Organization.git
cd Organization
./pull-all
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
./run.py --login admin
```

Open https://localhost:8443 and accept the locally generated development
certificate. The launcher starts OrgPortal and its identity, chat, organization
replica, and gateway services. Other ecosystem repositories are checked out for
their own workflows; the launcher does not start every repository.

For subsequent updates, run `./pull-all` (or `./pull-all.sh`). It clones missing
repositories and pulls existing ones with `--ff-only`, reports each failure, and
continues through the list. It never stashes or resets local work. Resolve any
reported checkout conflicts in that application's repository and retry.
An optional directory argument chooses another existing checkout parent.

## Local access

```bash
./run.py                       # start the named local launcher container
./local-login                  # sign in as a local member
./local-login --admin          # sign in as a synthetic local administrator
./local-login-guest            # local administrator in Chrome Guest mode
./run.py --port 9443 --login admin
./run.py --login admin --no-open  # print a one-use browser link
```

The launcher also builds and starts both public site previews from the shared
`BmoreMedTech` checkout:

- MedTech: https://localhost:8769 (`dist`, tenant `medtech.social`)
- LifeTech: https://localhost:8770 (`dist-lifetech`, tenant `lifetech.fyi`)

Both use the existing local OrgPortal, PIdP and chat services. Preview startup
builds the sites without running tests or deploying. After changing launcher
code, use `./run.py --recreate-launcher` to activate it.

Local sign-in requires the running loopback deployment and its local Docker
containers. Administrator provisioning is explicit and local; it grants no
production access. The launcher preserves the historical `bmoremedtech-` Docker
volume namespace by default. Use `./run.py --help` for configuration.

`run.py` runs inside `lifetech-launcher` (or `<prefix>-launcher` for another
namespace). It stays visible after startup; use `docker logs -f lifetech-launcher`
for its console and `docker stop lifetech-launcher` to stop the launcher.
The machine inspector marks launcher containers in biotech gold (`#D4AF37`).
The image packages the installed Python 3.12 launcher dependencies without package
downloads. The launcher uses the host Docker CLI and socket, with the workspace mounted at
its original path so the application containers receive the correct bind mounts.

Application changes belong in their respective repositories. Local credentials,
certificates, databases, dependency directories, and application checkouts are
excluded from this launcher repository. `push-all.sh` delegates to the existing
CodeCollective helper; review its behavior there before using it.

## Reverse engineering tools

To optionally clone the official [REA](https://github.com/morluto/rea)
source and prepare the CLI, run:

```bash
./setup-rea
./rea-tool --help
./rea-tool inspect-artifact /absolute/path/to/package.zip --json
```

Setup requires Git and npm. It caches REA 6.5.0 and its supported Node 24.18.0
runtime through npm, without changing the system Node installation. The
executable stays pinned until
the versions in `rea-tool` are deliberately updated. Native decompilation needs
a separately configured provider such as Ghidra. CLI use needs no agent restart.
To configure the Codex integration, first review
`./rea-tool setup --client codex --dry-run --json`, then apply the reviewed scope
with `./rea-tool setup --client codex --yes` and reconnect the agent.

## Verify the workspace scripts

```bash
python3 -m unittest discover -s tests
```

## Disposable VM test run

Install [Multipass](https://canonical.com/multipass/install), then run:

```bash
python3 run.py --vm
# Optional resource limits (default: 4 CPUs, 8 GB RAM, 40 GB disk, 90 minutes):
python3 run.py --vm --vm-cpus 6 --vm-memory 12G --vm-disk 60G --vm-timeout 7200
# Reuse downloaded public runtime images instead of pulling them again:
docker save -o /path/to/runtimes.tar postgres:15-alpine node:22-bookworm-slim node:24-alpine python:3.11-slim nginx:alpine busybox:latest curlimages/curl:8.4.0
python3 run.py --vm --vm-image-archive /path/to/runtimes.tar
```

This creates a uniquely named Ubuntu 24.04 VM with Docker, Python, Node 24,
and headless Chromium. It copies the current source from this checkout and its
`OrgPortal/` and `PIdP/` siblings, including uncommitted, nonignored source files.
Host directories are never mounted into the VM. Git metadata, ignored files,
dependency caches, local state, `.env`/`.dev.vars` credentials, `pidp_editme.py`,
and symlinks are excluded. The guest generates fresh local configuration and
databases; changes inside the VM do not change host application data.

The guest runs workspace and launcher unit tests, PIdP Python tests (including
the real PostgreSQL OAuth checks in a separate disposable test database), OrgPortal
frontend tests/build, both Worker test suites/type checks, and PIdP serverless
tests/type checks. It runs the full launcher twice, exercises member and admin
`--login --no-open` browser handoffs, then checks local browser-bound
SSO, brand themes, poster editing, and session persistence across browser and
container restarts. Synthetic fixture tests run with remote organization
replication, journal sync, and email delivery disabled. The VM uses localhost
port 8443 and its own Docker namespace; omit host `--login`, port, and namespace
overrides. Internet access is needed for Ubuntu, packages, and container images.

Test logs and `summary.json` are copied to `.local-reports/organization-test-*/`.
The command returns nonzero on a failed check, timeout, or cleanup failure.
It deletes and purges **only its own VM** after success, failure, Ctrl-C, or
SIGTERM, using [Multipass's per-instance deletion](https://canonical.com/multipass/docs/latest/reference/command-line-interface/delete/).
An uncatchable kill or host shutdown can prevent cleanup; the printed VM name
can then be removed with `multipass delete --purge <name>`.

## Local recovery and realistic testing data

`./disaster-recovery backup` keeps full production databases, uploaded objects,
source history, working changes, and available configuration on PILEDRIVE. Each
backup includes an isolated restore drill and a manifest of recovery gaps.
`./local-sync --org lifetech --include-private` creates a separate scoped testing
copy from the latest verified archive. See [DISASTER_RECOVERY.md](DISASTER_RECOVERY.md)
for credentials, daily scheduling, restore steps, retention, and current limits.

### Launcher WebRTC mesh

Launchers exchange readiness and Docker service names/states over encrypted WebRTC data channels. They use the enrolled fleet configuration, including the coordinator itself. The alphabetically earlier hostname initiates each connection; disconnected peers retry automatically. Each launcher keeps its current peer observations in `/tmp/launcher-peers.json` inside its container, and removes stale observations when a connection times out.

After the updated code is available on each machine, run `./run.py --peer-enroll` on the coordinator. This provisions a shared fleet key and machine-specific configuration through the saved SSH accounts at `~/.config/lifetech-launcher/peers.json` with owner-only permissions. Then run `./run.py --recreate-launcher` on each machine to rebuild and recreate its launcher with the host configuration directory mounted. Enrollment alone does not restart anything.

TCP port 8790 carries AES-GCM encrypted offer/answer signaling directly between enrolled LAN peers. LAN WebRTC uses direct UDP ICE candidates and DTLS data channels. Internet-enrolled launchers also use the authenticated public signaling service and its STUN/TURN configuration. Fleet machines need LAN connectivity on TCP 8790 and the UDP candidate ports. Signaling includes peer identities, expiring timestamps, single-use offer nonces and matching answer nonces. The shared fleet key is the trust boundary: all enrolled machines are trusted. Status messages contain no environment variables, credentials, logs, database content, or commands. The same LAN listener exposes a separately authenticated encrypted agent job API for source uploads, image imports, test events, cancellation and P2P browser control. Internet status enrollment alone does not grant access to this LAN execution API.

The launcher image installs `aiortc`, `aiohttp`, `zeroconf`, the Docker CLI and interface tools for WebRTC, DNS-SD and agent execution. This adds native dependencies to the launcher image and requires package downloads when rebuilding it; browsers downloading the website receive no additional dependencies.

### Agent readiness from the tests page

Start discovers `_lifetech._tcp.local.` services and checks enrolled agents over the Ethernet LAN. Enrolled endpoints can reconnect even when multicast discovery is absent. Authentication, running source revision, capabilities, machine identity and busy state determine eligibility. Unavailable nodes are reported and remaining application suites are reassigned. Incomplete fleet or P2P coverage is reported as partial coverage.

Start does not provision or update launchers and does not fall back to SSH. Start commits and pushes Organization, OrgPortal and PIdP on their existing branches, verifies the pushed commits, and asks local agents to fetch those exact GitHub revisions into fresh checkouts. Job control, cancellation and P2P browser control use the authenticated agent API. Image delivery publishes once to a loopback registry and uses encrypted agent requests to transfer only missing compressed layers; application and P2P phases share one resolver. No GitHub-hosted runners or automatic code repairs are involved. The coordinator participates through its own agent. Other local network collectors can still use their existing SSH probes.

Source updates are explicit: run `python3 scripts/launcher_update.py` on an enrolled machine to stage the current agent release, or add `--rollback` to select the previous release. The supervisor applies updates only when idle and restores the prior process release if startup readiness fails. Current agents do not reload. Image/dependency and supervisor changes require an explicit rebuild/recreation; initial activation of this migration requires `./run.py --recreate-launcher` on each participating machine after the updated source is present. See [agent protocol and limits](lan-monitor/README.md#authenticated-launcher-agents).

### Internet device registration

After deploying the updated peer Worker, the public registration page is `/register` and the API is `POST /launchers/register`. Any internet-connected device with a fleet invitation can enroll; registration grants status-mesh membership, not SSH access, test execution, or production credentials.

On the trusted coordinator, create one private invitation:

```sh
./peer-share launcher-invite --output /tmp/launcher-invitation.json
```

The file includes a private registration link and an invitation token valid for one hour. Give the invitation to its intended device. On that device:

```sh
./run.py --peer-register /path/to/launcher-invitation.json
./run.py --recreate-launcher
```

`--peer-register` needs only Python's standard library and HTTPS access. It creates a random device credential, registers through the public service and saves the credential in owner-only host configuration. Interrupted enrollment retries reuse the same pending credential. Existing working registration remains intact if a new enrollment fails. Registration lasts 90 days; the launcher renews its 30-minute signaling sessions automatically and reconnects after disconnections. Each fleet currently supports 24 active enrolled devices. Internet signaling discovers peers within that fleet and returns the existing short-lived STUN/TURN settings. TURN must be configured for networks that cannot establish a direct WebRTC route.

To revoke a device using its printed peer UUID:

```sh
./peer-share launcher-revoke --peer DEVICE_UUID
```

Only the coordinator's signing key can create enrollment/admin invitations. The server stores hashes of device credentials, rejects enrollment-token reuse by a different device credential, separates enrollment/admin/session audiences, and checks revocation before new signaling or ICE leases. Revocation closes the device's signaling connection; connected launcher clients then close their peer links. The public registry accepts no unauthenticated fleet membership. Deploying this Worker and enabling TURN are separate release actions; implementing these paths does not publish them.

### Automatic launcher registration

A launcher starts its enrollment watcher during startup, even when it has no peer configuration. It checks the host directory `~/.config/lifetech-launcher/` every five seconds. Put a single device invitation in `invitation.json` there: the launcher registers itself through the invitation's HTTPS service, persists its device credential in `peers.json`, and connects automatically. No container restart is needed when the invitation or configuration changes. The launcher has write access only to its mounted enrollment directory for this purpose.

Transient registration failures retry with jitter and exponential backoff up to five minutes. Expired, rejected, revoked, or already-consumed invitations wait for a new invitation file; the launcher does not repeatedly reuse them. Existing valid enrollment is preserved if a new registration fails. Revoked sessions stay disconnected until new credentials arrive. Local enrollment status is visible in `/tmp/launcher-enrollment.json` inside the container. LAN and internet sessions are supervised independently, so updating internet registration does not restart a healthy LAN mesh.

### Mounted source updates

Launcher peer code runs from the bind-mounted workspace (or the session's mounted source directory). A small supervisor watches the peer Python files and restarts only the peer subprocess after a source change; it does not recreate the container or restart application services. Enrollment files are watched separately and take effect without restarting the peer subprocess.

The LAN API uses a stable coordinator adapter that imports the current source for each new test run. An active run keeps its existing coordinator implementation; the next click of **Start all tests** uses the latest mounted coordinator, session-initialization, image-distribution and P2P modules. There is no service restart between revisions. Test preparation updates existing session source directories as well as new sessions, so their watchers pick up the revision.

Existing processes launched before this adapter/supervisor was installed need a one-time transition to it. A volume mount exposes file changes but cannot replace code already imported by those older processes. These changes have not been activated automatically.

### Shared LAN-first Docker module

`docker_utils/` is the shared Python module, packaged as `lifetech-docker-utils` by the root `pyproject.toml`. Install it with `python -m pip install -e .` from this workspace. The launcher, OrgPortal, PIdP, MedTech and distributed coordinator now use it instead of copied Docker utility implementations. PyPI publication is deferred.

Image resolution follows local cache → saved LAN SSH peers → registry. Container startup, SDK implicit pulls, explicit image pulls, readiness helpers, recovery image startup, release-runtime startup and Dockerfile external bases use the same policy. Images stream between Docker stores through memory pipes and their native platform/content are checked. Dockerfile bases are prepared before the engine builder runs, so a separate BuildKit store cannot silently fetch them again. Package downloads inside a Dockerfile remain separate network operations.

See [the module README](docker_utils/README.md) for API, CLI and inventory configuration. This policy governs these workspace entrypoints and its SDK client, not unrelated bare Docker/Compose commands or inactive vendored checkouts. Dependencies can be installed once in the shared Python environment; the launcher mounts the saved LAN inventory and SSH configuration read-only so its `HOME=/tmp` does not bypass peer discovery.
