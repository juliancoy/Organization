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
`~/.config/swarm-control/`. A successful owner sign-in creates a controller only
when none is active. Replacing another active controller requires an explicit,
short-lived confirmation. Revocation also invalidates transfer authorization.
Controller records retain their label, creation, expiry, and last-seen times.
Local QR enrollment remains available through the owner launcher.

## Interfaces and state

- Public HTTPS: console, `/swarm-status`, `/swarm-control` APK receiver, and
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
