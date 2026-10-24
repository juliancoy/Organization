# Organization

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
./run.py                       # start or refresh the local stack
./local-login                  # sign in as a local member
./local-login --admin          # sign in as a synthetic local administrator
./local-login-guest            # local administrator in Chrome Guest mode
./run.py --port 9443 --login admin
./run.py --login admin --no-open  # print a one-use browser link
```

Local sign-in requires the running loopback deployment and its local Docker
containers. Administrator provisioning is explicit and local; it grants no
production access. The launcher preserves the historical `bmoremedtech-` Docker
volume namespace by default. Use `./run.py --help` for configuration.

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
