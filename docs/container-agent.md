# Containerized enrolled agents

`scripts/agent_container.py` renders the existing user agent service with Python
inside the verified launcher Docker image. It preserves enrollment, machine
identity, signed source updates, and the host job paths used by Docker bind mounts.
It does not install or upgrade host Python. Administrative enrollment and source
staging are separate from the long-running agent runtime.

Use a locally verified immutable image ID with Python 3.12 and the dependencies
from `Dockerfile.launcher`. Verify its Python version and imports before switching.
The control container uses the system Docker daemon for actual host networking.
Use `--worker-socket` to keep jobs on an existing rootless Docker daemon and
preserve its images, caches and containers. The agent only receives that worker
socket; its control daemon socket need not be mounted into the container.
Running the control container itself on rootless Docker is rejected because its
host network namespace differs from the physical host.

Before switching, close admission using owner maintenance, preserve the prior
worker preference, and wait for active jobs, preparations, resource leases,
browsers and live transfers to finish. Do not restart a busy agent. Stage the
reviewed bootstrap scripts in an immutable release directory. Render the unit
with that directory, the existing configuration and job paths, the owner's UID
and GID, and the Docker socket's actual group ID. Validate the candidate with
`systemd-analyze --user verify` and retain the previous unit for rollback.

The unit runs the supervisor and its child in Docker, with a read-only root,
private temporary directory, the existing writable enrollment/job directories,
and an explicit agent health check. CUDA nodes additionally need a verified
NVIDIA container runtime and `--gpus` when rendering their unit.

After starting, verify authenticated readiness, enrolled machine identity and
addresses, the pinned source revision, the container's Python version, and the
identity and continuity of existing user containers. Restore the previous unit
on failure. Resume the saved worker preference only after readiness succeeds.

Use `--fixed-release` when concurrent controllers are staging different source
versions. This runs the read-only mounted agent source directly, so changing the
shared update pointer cannot restart it. Upgrade by draining transient work and
replacing the service with another reviewed source directory. Other controllers
will reject a revision mismatch rather than interrupt this agent's jobs.
