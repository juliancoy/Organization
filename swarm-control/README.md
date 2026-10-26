# Swarm Control

Swarm Control is the independently operated application for machine inventory,
distributed jobs, remote Codex conversations, controller enrollment, and Android
access. Its implementation and runtime live here; LifeTech and OrgPortal are not
application dependencies. PIdP remains the shared identity provider.

## Ownership

- `server/`: coordinator, REST/MCP operator API, network collectors, remote API,
  web console, controller sessions, and fixed-APK WebRTC delivery.
- `android/`: browser-based Android launcher; it shares the browser's authenticated
  session instead of maintaining a second WebView identity.
- Root `scripts/launcher_*.py`: machine-local agent transport and enrollment.
- Application repositories: their own test suites, release and backup operations.

Swarm Control schedules application-owned operations. It does not copy application
business data or replace application permissions. The packet collector has its
own restricted container; the public tunnel does not expose Docker or the native
operator API.

## Operate

From Organization:

```sh
python3 swarm-control/run.py install
python3 swarm-control/run.py start
python3 swarm-control/run.py status
python3 swarm-control/run.py stop
```

`swarm-control.target` owns the coordinator, authenticated remote API, HTTPS
tunnel, and collector containers. `deploy --revision SHA` extracts immutable application code and installs the reviewed implementation
and restarts this target. The configured all-repository release invokes it after
the distributed test gate. No second coordinator or old service API is retained.
The existing CompAI URL remains the access address during this extraction.

## Identity and recovery

Open `https://compai.lifetech.fyi/auth/login`. Swarm Control has a separate PIdP
client, audience `https://compai.lifetech.fyi/api/swarm`, and `swarm:control`
permission. LifeTech portal credentials are not requested. The configured owner
is an explicit PIdP-verified canonical person; linked credentials are checked
through authenticated introspection. Email and application working copies never
assign ownership. Initial registration is an operator action:

```sh
python3 swarm-control/server/setup-identity.py \
  --owner-subject YOUR_VERIFIED_PIDP_SUBJECT \
  --owner-canonical-id YOUR_VERIFIED_PIDP_PERSON_ID \
  --resource-secret-file /private/path/to/swarm-resource-secret
```

Client credentials and controller records are owner-private under
`~/.config/swarm-control/`. Each successful owner sign-in adds an independent controller. Multiple browsers
can remain signed in; reauthentication rotates only that browser’s session.
Lock revokes only the current browser, including its transfer authorization.
Controller records retain their label, creation, expiry, and last-seen times.
Local QR enrollment remains available through the owner launcher.

## Interfaces and state

- Public HTTPS: console, `/task-manager`, `/swarm-status`, `/swarm-control` APK receiver, and
  authenticated `/api/v1/swarm/*` operations. `/health` exposes readiness only.
- Loopback 8089: coordinator, native authenticated REST/MCP, local dashboard.
- Loopback 8091: the remote API, reached through the configured tunnel.
- Loopback 8092: separately authenticated local QR issuer.
- `~/.local/state/swarm-control/`: coordinator history, queues, metrics and captures.
- Workspace `.runtime/mobile/`: signed test APK; signing material stays private.

## Validate

```sh
python3 swarm-control/run.py test
node swarm-control/test-console.mjs
```

The transport check uses an isolated HTTPS fixture and a real browser-to-aiortc
channel. It validates authorization, APK bytes, SHA-256, receipt, narrow layout,
and disconnect; it never borrows a production session or claims handset
installation. Build the Android launcher with `npm --prefix swarm-control run build`.
The user must approve installation on the Galaxy.

Run the global distributed suite through the coordinator or the Swarm status
page. Queueing tests publishes reviewed source revisions before execution.

## Remote Task Manager

Open `https://compai.lifetech.fyi/task-manager` after signing in to Swarm Control.
CPU, GPU, GPU memory, RAM, disk, network, and task observations use one
same-origin authenticated HTTPS feed. The browser never contacts LAN addresses.
Measurements refresh every three seconds while the page is visible; missing or
stale telemetry remains unmeasured. Session expiry clears the displayed data.

## CPU reporting

CPU load uses differences in Linux counters across the entire interval between
snapshots, normally five seconds. No one-second sampling gaps or root privileges
are required. Startup, counter resets, reboot and long gaps warm up without
reporting idle. Missing, permission-denied and stale samples remain unmeasured.
Agents need the current metrics implementation; old agents show update required.

The fleet average is weighted by measured logical CPUs. A second average includes
only machines currently executing tests, including active reruns. Both describe
all processes on those machines, not CPU time attributed to test containers.
The UI shows coverage, interval, age and execution phase. I/O wait and stolen CPU
time are reported separately from executing CPU time. CPU pressure is the kernel's
10-second average of time tasks wait for CPU. Container CPU quotas still constrain
execution; they do not change the whole-machine utilization denominator.

## DNA research workflow

`/research` on the authenticated remote service (`/research.html` on the local
coordinator) maps the documented DNA analysis stages, file formats, completed QC
and existing-annotation audit, and pending coverage/annotation/comparison work.
Research links are available in the console, LAN dashboard, and swarm status.
The dated milestone is historical; the session-protected
`/api/v1/swarm/research` endpoint refreshes batch counts and public download
presence every 30 seconds while visible. It reads only operational state and an
allowlist of public dataset folders on PILEDRIVE; it never serves genomic records,
sample identifiers, report contents, or arbitrary filesystem paths. Missing state
is shown as unavailable. Partial downloads do not imply active transfer or
successful validation. Browser checks use synthetic status fixtures.

Verified reference-match snapshots can be rebuilt with
`python3 -m scripts.genome_demo --root <private-workbench> --reference-run <completed-private-run> --output <snapshot.js>`.
The reference run uses quality-qualified gVCF calls for personal data and official
GIAB benchmark regions minus both official and normalized variant REF spans.
Unknown quality stays unverified. Only aggregate counts enter the public snapshot;
private intervals, sample headers, and worker hostnames stay outside it.

## Concurrent tests and private DNA jobs

The durable request inbox admits up to eight independent runs concurrently and
keeps up to 100 pending/running requests. Ordinary tests and configured DNA
analyses use the same inbox. Publication is serialized; execution is not. Every
run owns its checkout and dependency trees. P2P/device/GPU runs serialize against other coordinated hardware runs, while
ordinary test and DNA jobs continue to share remaining worker capacity. P2P
browsers and GPU kernel probes also reserve their CPU and memory in the same pool.

All test/install containers and private DNA batches acquire CPU and memory from
one process-safe pool per worker. The pool also reserves each container’s PID
limit and leaves up to 256 process/thread slots free in finite cgroup ancestors for host
services and Docker. Combined reservations cannot exceed logical
CPUs or current schedulable RAM, including visible shared cgroup constraints.
Genomics jobs divide live available memory across remaining concurrency and queued
jobs; their Docker memory limits equal the resource leases. There is no fixed
512 MiB worker ceiling or fixed host-memory reserve. Agent-private container
limits do not constrain sibling worker containers. Fitting jobs backfill free slots. Groups with the smallest dominant CPU
or memory share get priority, and a waiter older than 30 seconds reserves
headroom to prevent starvation. Docker still enforces each container's limits.
The controller bounds pending compressed DNA payloads to 128 MiB per run and 64 in-flight batches. Process locks recover capacity after crashes; orphan container reservations remain
until the container exits. Private stream leases expire with container cleanup.
Worker policy, enrollment/attestation, disk space and bounded session counts still
apply. Cancellation affects one request/run and releases its owned resources.

The Research page lists test and DNA requests and provides queue/cancel controls.
DNA submissions use idempotent request IDs and named owner-only profiles in
`~/.config/swarm-control/research-jobs.json` (mode 0600), each containing `qc`,
`annotations`, and `output` paths. Those paths, private logs, and genomic reports
are never returned by the control API. Research code is snapshotted from an exact
Git commit into the private report directory before execution. Inputs remain
SSH-streamed into RAM-only containers. The general test artifact system receives
only operational progress. A coordinator restart marks unfinished requests
interrupted instead of silently repeating private work; explicit reruns are
available after reviewing the private reports.

Deployments drain new admissions, wait for running jobs, then restart. A timeout
or failure removes the drain flag and preserves active jobs.

Launcher updates raise undersized PID limits on the verified launcher container to
a bounded budget based on logical CPUs (at most 32,768). This uses Docker update
and does not restart containers; unrelated containers and unlimited limits are
left alone.


## Task Manager history

The coordinator's resource sampler retains a rolling five minutes of host CPU,
RAM, disk, network and GPU measurements in bounded memory, independently of
browser requests. Opening or reloading Task Manager restores that history from
the authenticated feed. Collection continues while the page is closed; a
coordinator restart clears the window. Unavailable samples remain gaps. Historical
gateway-only traffic is not reconstructed from current measurements. Private
process details, paths, credentials and genomic records are excluded.
