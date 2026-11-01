# Containerized enrolled agents

`scripts/agent_container.py` renders the existing user agent service with Python
inside the verified launcher Docker image. It preserves enrollment, machine
identity, signed source updates, and the host job paths used by Docker bind mounts.
It does not install or upgrade host Python. Administrative enrollment and source
staging are separate from the long-running agent runtime.

Use a locally verified immutable image ID with Python 3.12 and the dependencies
from `Dockerfile.launcher`. Verify its Python version and imports before switching.
The current service supports the system Docker socket and host networking.
Rootless Docker needs a separate networking migration; the generator rejects it.

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
