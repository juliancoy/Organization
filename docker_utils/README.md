# LifeTech Docker utilities

One local Python package for LAN-first Docker image resolution, streaming transfers,
Dockerfile base preparation, and container startup. No package has been published.

Install from the Organization workspace:

```sh
python -m pip install -e .
```

```python
import docker_utils
image_id = docker_utils.ensure_image('python:3.12-bookworm')
docker_utils.build_image('example:local', './example')
container = docker_utils.run_container({
    'image': 'nginx:alpine', 'name': 'example', 'detach': True,
})
client = docker_utils.from_env()  # SDK implicit pulls follow the same policy
```

`lan-docker pull IMAGE` and `lan-docker build -t TAG CONTEXT` expose the same policy.
A pull checks the local Docker image cache, then every saved LAN SSH peer, and only
then the registry. A cached peer image streams via `docker save | gzip` and
`gzip -d | docker load`; image archives are never stored on intermediate disk.
Destination image IDs/content metadata are verified. Unreachable peers do not
hide a later complete copy. Digest references use a deterministic local transfer
tag so Docker save/load does not lose their cache association.

By default, peers come from `~/.local/state/orgportal/lan-ssh-devices.json` and
use `~/.ssh/orgportal_lan_ed25519`, with strict known-host checking. Override
`DOCKER_UTILS_INVENTORY`, `DOCKER_UTILS_SSH_KEY`, and
`DOCKER_UTILS_KNOWN_HOSTS` for container mounts or other inventories. A missing
inventory means the local cache is checked before the registry; no arbitrary
network scan or unauthenticated Docker TCP endpoint is used.

Builds resolve external `FROM` and `COPY --from` references first, including
build arguments, stage aliases and digest references. Local bases are pinned in
the generated Dockerfile; CLI builds use the engine builder with `--pull=false`
to avoid a separate BuildKit cache pulling the same base from the WAN. BuildKit
only instructions and custom/remote SDK contexts are not supported by this
policy. Package-manager downloads inside `RUN` instructions are not Docker image
pulls and still use their configured repositories.

The policy applies to the workspace launchers, readiness helpers, recovery image
startup, distributed tests, and the package's SDK client. It cannot intercept
unrelated bare `docker pull`, external Compose invocations, or arbitrary
applications using their own Docker client. Use these entrypoints for LAN-first
behavior. Only explicitly configured SSH peers are trusted image sources;
mutable tags use the first complete local/LAN copy. Use digest references when
immutable upstream identity matters.

Imports do not connect to Docker. The image resolver uses Python's standard
library; SDK helpers load the `docker` dependency only when accessed.
