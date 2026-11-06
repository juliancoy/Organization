# Containerized enrolled agents

JSwarm uses a dependency-only Docker image and host-mounted Git source. The
standard update fetches validated, pinned commits through each enrolled node's
signed API and verifies its runtime and readiness before continuing the rollout.

Follow [Git source deployment](../JSwarm/docs/node-source-deployment.md) and
[Fleet releases](../JSwarm/README.md#fleet-releases). From the Organization
workspace, after committing and pushing the source:

```sh
python -m scripts.deploy_swarm_inputs --publish
python -m scripts.swarm_node update
python -m scripts.deploy_swarm_inputs --status
```

Use `./build.sh` from JSwarm for initial provisioning or changed dependencies.
Code-only deployment does not rebuild the image. Enrolled identities, existing
configuration mounts and Docker-owned workers survive agent replacement. The
supervisor verifies checksums and readiness and restores the previous runtime if
startup fails. Deployment and explicit rollback require Git-backed publications.
