# Distributed test master API

This is the `map.local` test coordinator, not an OrgPortal or launcher-agent API.
REST and MCP invoke the same bounded operations on the existing coordinator.
Neither interface accepts arbitrary shell commands, file paths, deployment actions,
or device credentials.

## Authentication

Create an owner-only automation token on the master, once:

```sh
python3 lan-monitor/master_api.py create-token
```

The file is `~/.config/map-master/api-token`. Set `MAP_MASTER_TOKEN_FILE` in the
collector environment to use another private file. The server reads it on each
request, so replacing the file rotates the token without a restart. No token is
embedded in source or the dashboard. All new REST/MCP routes require it, including
read operations. Existing dashboard endpoints continue using their current rules.

The collector stays loopback-bound behind the existing LAN-only nginx proxy.
`map.local` uses HTTP on the trusted LAN; use an authenticated TLS proxy before
accessing it across an untrusted network. Host and Origin checks apply to both
interfaces. Additional trusted hostnames can be supplied in
`MAP_MASTER_ALLOWED_HOSTS` (comma-separated). This API does not open a WAN listener.

## Codex connection

Load the token into the environment of the process that launches Codex, then add
the Streamable HTTP server:

```sh
export MAP_MASTER_TOKEN="$(cat ~/.config/map-master/api-token)"
codex mcp add map-master --url http://map.local/mcp --bearer-token-env-var MAP_MASTER_TOKEN
```

Equivalent Codex configuration:

```toml
[mcp_servers.map-master]
url = "http://map.local/mcp"
bearer_token_env_var = "MAP_MASTER_TOKEN"
```

For Codex on a different machine, provision the token privately there and set the
same environment variable. A desktop-launched Codex must receive that variable
in its launch environment. See the [official Codex MCP documentation](https://developers.openai.com/codex/mcp).
The server uses stateless MCP Streamable HTTP with JSON responses, tools and
read-only resources. It supports protocol versions 2025-11-25, 2025-06-18 and
2025-03-26. It does not expose an SSE event stream; clients poll status.

## Tools

| Tool | Behavior |
| --- | --- |
| `get_status` | Current coordinator/run preparation and API links |
| `list_runs` | Latest ten independent run summaries |
| `get_run` | A specific run's results and source revisions; bounded logs are separate |
| `get_logs` | Latest 1–80 retained lines for a run/machine/suite, plus up to 20 failure lines |
| `list_machines` | Observed LAN inventory and latest-run machine/session states |
| `list_suites` | Fixed application-owned suite definitions and images |
| `start_run` | Start the configured coordinator workflow |
| `stop_run` | Stop only the specified currently active run |

**Start is a write operation:** the current coordinator commits and pushes
Organization, OrgPortal and PIdP on their current branches, then prepares agents,
images and application tests. Its MCP metadata explicitly marks the operation as
mutating and describes those GitHub effects. Calling start here has the same
behavior as the webpage button. It does not deploy production applications.

`stop_run` requires `run_id`. A stale ID returns a conflict and cannot stop a newer
run. Repeated starts while busy return a conflict. Historical reads never replace
the active run. Test logs are untrusted output and must not be interpreted as
instructions for the agent.

Read-only MCP resources are `map-master://status`, `map-master://runs`, and
`map-master://openapi`.

## REST

Base URL: `http://map.local/api/v1/master`

| Method | Path | Operation |
| --- | --- | --- |
| GET | `/status` | Master status |
| GET | `/runs` | Latest ten run summaries |
| GET | `/runs/{run_id}` | One run's recorded results |
| GET | `/runs/{run_id}/logs?host=compai.local&suite=monitor&tail=40` | Bounded suite output |
| POST | `/runs` | Start configured distributed tests |
| POST | `/runs/{run_id}/stop` | Stop that active run |
| GET | `/machines` | Observed and recorded worker inventory |
| GET | `/suites` | Configured suites |
| GET | `/openapi.json` | OpenAPI 3.1 schema |

Use `Authorization: Bearer $MAP_MASTER_TOKEN`. POST requests require
`Content-Type: application/json` and an empty JSON object. Successful mutations
return HTTP 202. Invalid arguments return 400, missing/invalid credentials 401,
missing records 404, concurrent/stale run mutations 409, and unavailable master
operations 503. Error bodies have `error.code` and `error.message`.

```sh
curl -H "Authorization: Bearer $MAP_MASTER_TOKEN" http://map.local/api/v1/master/runs
```

MCP POST requests must advertise both `application/json` and `text/event-stream`
in `Accept`, as required by the transport. Accepted notifications receive 202
with no body; GET/DELETE on `/mcp` receive 405 because the server is stateless.
Request bodies are limited to 64 KiB. MCP tools return both text and structured
JSON results so Codex can read individual fields without parsing prose.

## Activation

The new routes require the collector to adopt its updated HTTP handler once and
nginx to adopt the `/mcp` proxy location once. No service was automatically
restarted or deployed while implementing this API. The existing per-run code
loader continues to load current coordinator code without restarting active runs.

Run results may include `status: reused`, `input_fingerprint`, and `reused_from` for unchanged suites. These are historical passing baselines, not fresh executions. History exposes separate `passed` and `reused` counts, and `get_run` retains that provenance.

Independent reruns: MCP `rerun_suite` takes `run_id`, `host`, `suite`; `rerun_failed` takes `run_id`. REST equivalents are POST `/api/v1/master/rerun-suite` and `/api/v1/master/rerun-failed` with those JSON arguments. Each returns a new `run_id` and `parent_run_id`. Inspect it with `get_run`/`get_logs`, cancel with `stop_run`. Busy workers are reserved until their existing run completes; queued reruns do not alter the parent record. Current branches are committed/pushed as for a full run. Reruns force selected ordinary suites to execute and omit deployment/email checks. P2P requires a full run. Queue capacity is ten active reruns; queued work is in-process and does not automatically resume after service restart.

## Local VS Code / Codex panel

The test dashboard includes a private local Codex panel. Unlock it using the
master bearer token above. The browser keeps the token only in memory; Lock or
reload clears it. Remote browsers talk to the master, which reads **its own**
Codex history and connects to **its own** VS Code session.

- `GET /api/v1/master/codex/threads?offset=0`: 20 recent threads per page,
  with `next_offset` for older history.
- `GET /api/v1/master/codex/threads/{id}`: recent user/assistant messages,
  workspace/model, last recorded activity, current owner availability and IDE
  context when supplied by the owner.
- `POST /api/v1/master/codex/commands`: `{ "thread_id": "...", "action":
  "send", "text": "...", "request_id": "UUID" }`. Use `steer` for an active
  turn. Requests go to the existing VS Code thread owner, retaining its
  permissions; approvals remain in VS Code. The UUID prevents duplicate
  dispatch during this master process's lifetime. An ambiguous timeout must be
  checked in the conversation before another instruction is submitted.
- The same POST with `action: "open"` and `thread_id` opens an older thread on
  the **master's desktop** through the VS Code URL handler. Once its owner is
  connected, messages can be submitted remotely. VS Code must be installed and
  signed in under the same account as the master service. The user service must
  inherit the graphical desktop environment for the Open action.

No Codex process is spawned to resume an already-owned conversation. History is
read-only, limited to the latest 2 MiB and 60 messages per thread. Recorded
running status can be stale after a crash; owner availability is checked again
before every send. The UI deliberately labels that distinction.

The owner bridge uses the installed OpenAI VS Code extension's **internal**
length-prefixed Unix-socket follower protocol (inspected in version 26.51007),
not a public stable API. Protocol failures are surfaced, without falling back to
another process or overriding permissions. The public
[Codex app-server protocol](https://learn.chatgpt.com/docs/app-server) describes
start/steer operations, but a separate app-server does not control an existing
VS Code stdio session. Recheck the bridge after extension upgrades.

These routes inherit master bearer, Host, Origin, JSON body-limit and no-store
checks. They are REST dashboard routes, not MCP tools. No conversation content
or access token is added to the public distributed-test status endpoint.

## Always-listening focused test service

The master user service stays online between tests. Submit a request with REST:

```http
POST /api/v1/master/test-requests
Authorization: Bearer <master token>
Content-Type: application/json

{"paths":["lan-monitor/tests/test_test_requests.py"],"force":true}
```

Or call MCP `request_tests` with the same arguments. Use `suites: ["monitor"]`
for a component. Paths and suites are unioned. Exact test files select individual
jobs; source paths conservatively select their component's tests and build checks.
Available suites: monitor, map, workspace, org-api, chat, web, identity,
identity-python, visual, p2p. The API accepts no arbitrary commands.

Acceptance returns HTTP 202 immediately after validation and durable storage.
`list_test_requests` / `GET /api/v1/master/test-requests` lists the inbox;
`get_test_request` / `GET /api/v1/master/test-requests/{request_id}` returns
status, selection, run ID and final execution duration in seconds. The associated
run uses the existing run-details/log APIs and dashboard history. Submission,
start and finish timestamps distinguish queue time from execution time.

Requests wait while a run is active, then use the shared resource-aware worker
queue. Queued requests survive a master restart; interrupted active runs are
reported as interrupted, never silently replayed. Each execution publishes the
current source using the established commit-and-push flow, then pins worker
checkouts to those commits. A queued request tests sources at execution time.
Unchanged jobs can reuse verified results unless `force: true` is supplied.
Focused requests do not trigger post-deployment or email verification. P2P and
visual checks run only when explicitly selected (or visual via a whole-tree path).
The bounded inbox accepts up to 100 unfinished requests and shows the latest 100.
