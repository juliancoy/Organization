# Organization workspace

Clone and run the Organization ecosystem from one directory. This repository
contains launcher scripts only. The six applications listed in
[repositories.txt](repositories.txt) are ordinary independent Git checkouts,
ignored by this repository. There are no submodules.

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

## Verify the workspace scripts

```bash
python3 -m unittest discover -s tests
```
