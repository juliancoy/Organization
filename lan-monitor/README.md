# LAN ecosystem

Open **http://map.local** from the shared LAN. The view starts with the visiting device at the center. Clicking any particle or device-list button switches perspective; **My device** returns to the visitor.

The **Link map** draws ASUS Wi-Fi associations, ASUS LAN membership, this machine's negotiated Ethernet link, and its upstream gateway. Neighbor tables alone do not imply direct physical cables. The **IP hops** layer draws the selected device's UDP TTL trace, preserving unanswered intermediate hops; ASUS is a Layer 2 bridge and is not counted as an IP router. Devices without SSH observations stay selectable. Their attachment may still be verified through the ASUS association table; their own interface and routes remain unmeasured. Dim particles are shared inventory. Source, observation time, access failures, and measured interface are visible in Details. Browser identity comes from its actual connection IP, not a selected device or a client-supplied forwarding header. A proxy or SSH tunnel is identified as the proxy endpoint; select a device explicitly in that case.

## Run

The host collector uses Python's standard library plus `ip`, `ping`, `busctl`, and existing trusted SSH logins. It listens only on `127.0.0.1:8089`. Install and enable its user unit:

```bash
cp lan-monitor.service ~/.config/systemd/user/lan-monitor.service
systemctl --user daemon-reload
systemctl --user enable --now lan-monitor
cp lan-map-mdns.service ~/.config/systemd/user/lan-map-mdns.service
systemctl --user daemon-reload
systemctl --user enable --now lan-map-mdns
```

The unit path and network configuration target this machine. In this directory, start the Docker view:

```bash
docker compose up -d
```

The nginx container uses host networking and two **read-only bind mounts**: `./view` for HTML/CSS/JS, and `./nginx.conf` for its configuration. Editing view files takes effect on reload without rebuilding. No SSH keys, Docker socket, or credentials are mounted in the container. It has only the `NET_BIND_SERVICE` capability needed for port 80, with a read-only root filesystem. IPv4 binds only `10.42.0.1:80`; IPv6 listens on port 80 with access restricted to link-local sources. nginx overwrites the viewer-address header before proxying to the loopback collector.

Docker restarts the view unless stopped. The host collector starts with the user's systemd session; unattended boot before user login is not configured. Health:

```bash
docker compose ps
systemctl --user status lan-monitor
curl http://map.local/api/health
```

## Observations and tests

The browser polls every second (five seconds when hidden). Host neighbor scans run every 30 seconds. SSH-capable devices are inspected concurrently every five minutes, or after **Scan network**, using existing batch authentication for `julian` and `wolf` and strict known-host checking. Remote scripts inspect the device's default interface, scan its connected IPv4 subnet (/24 or smaller), sample interface byte counters, and probe an internet IP. They do not change routes, DHCP, DNS, or device settings. Inaccessible devices are explicitly unmeasured. IPv4 and IPv6 observations merge by MAC, preserving IPv6 SSH when IPv4 access is blocked.

The selected observer's RX/TX appears in the inspector. Host counters update continuously; remote counter measurements show a sample timestamp. Moving activity markers are limited to continuously measured host counters. Blue rings show the latest HTTP download benchmark on a 0–1000 Mbps scale, not physical link capacity or internet speed.

**Run web tests** runs HTTP/API and installed headless Chrome checks across accessible devices. Each accessible remote downloads three 4 MiB payloads directly over scoped LAN IPv6; the Chrome smoke test uses a temporary SSH reverse tunnel. Test results and observations live in memory until collector restart. No packages are installed remotely.

Run the perspective evidence tests with `npm test`. No dependencies or build step are required. The UI uses polling rather than P2P: a single collector already owns discovery and SSH observations, so peer signaling would add no useful data path.

`map.local` is advertised by `mdns.py` through the host Avahi daemon on `enp7s0` only. This requires the system Python D-Bus and GLib bindings (present on this machine). Clients need mDNS support; the direct address is http://10.42.0.1. The alias publisher restarts with the user session.


## Edge metrics and hop evidence

The inspector is the panel on the right and follows the selected node. It shows addresses, ports, router association, band, negotiated rates, route observations, and each incident edge's capacity/traffic provenance and timestamp.

An edge's outer width uses a logarithmic scale for its reported link rate. This is a capacity ceiling, not an available-bandwidth estimate. The inner stroke scales with observed throughput and changes from cyan to amber/red as the rate approaches the ceiling. Unknown capacity stays dashed; unknown traffic has no colored fill. Public transit links do not inherit access-link traffic or capacity.

`packet_monitor.py` runs in the `traffic` Docker service with host networking and only `NET_RAW`. It counts frame lengths by peer MAC on `enp7s0`, retains no payload, and writes one-second counters to the ignored `.runtime/` bind mount. These counters cover traffic crossing this gateway, including dashboard activity. They exclude peer-to-peer traffic that remains inside ASUS and are not a physical wire-rate measurement. The frame collector has no SSH keys or router credentials. A stale or missing sample is unknown, not zero.

Router membership and PHY rates refresh every 15 seconds using the read-only ASUS client-list endpoint. Credentials are kept only in `~/.config/lan-monitor/router.json` (mode 0600), outside the repository and outside Docker mounts. `measurements.py` reads Ethernet/Wi-Fi link rates and runs bounded UDP TTL probes with the Linux ICMP error queue. Trace gaps remain explicit. Remote SSH observations include the same link and route measurements; no remote packages are installed.

The alias publisher also sends scoped unicast IPv4 answers for `map.local` queries received on Ethernet. This avoids relying on downstream Wi-Fi multicast delivery; Avahi continues the normal multicast publication. No other DNS names are answered by that helper.

The default link map includes the gateway’s full observed upstream trace. Route segments are IP-hop evidence, not physical cable claims; unanswered hops stay explicit and transit capacities stay unknown. The canvas starts with minimal node labels; use **Labels** for all labels. Perspective, legend, traffic samples, and link details are in the sidebar.

## On-demand browser benchmark and selection motion

**Run network benchmark** tests this browser's HTTP path to compai, with five warm-connection RTT samples and three 4 MiB transfers per direction. The UI reports median/min/max latency (ms), aggregate application throughput (decimal Mbps), elapsed time, endpoints and timestamp. It includes nginx/browser/server overhead and does not measure internet speed or physical capacity. Running on compai measures a local path. Cancel or the 30-second deadline stops browser requests. The collector admits one session per 45 seconds and caps each session at six pings and three transfers per direction; uploads have size and read deadlines. Reloading loses the displayed result. A busy, unavailable, interrupted or expired test is explicitly reported. No benchmark runs automatically.

Node selection retains existing coordinates, pan and zoom. The selected node gently approaches the center through bounded physics steps; rapid clicks use current positions. Fit is explicit after initial loading. Pause and reduced-motion preferences keep positions still, including when selecting nodes; changing the system preference is handled live.

There is no `build.sh`: the bind-mounted UI is served directly. Validate with `npm test`, `python3 -m unittest discover -s tests`, syntax checks and browser checks; no alternate build is needed.

## Passive outbound destination map

The `traffic` service now listens on the discovered LAN and default-route Ethernet/Wi-Fi interfaces using host networking and `NET_RAW` only. It takes bounded 256-byte header snapshots, immediately discards frame data, and retains only source MAC/IP, destination IP, capture interface, byte counts, timestamps and attribution basis. `recvmsg(MSG_TRUNC)` supplies the original packet length, so snapshot truncation does not undercount accepted IP packets or existing peer frame counters. IPv4 options, IPv6 extension/fragment headers and up to two 802.1Q/802.1ad VLAN tags are parsed defensively. Malformed/truncated headers and IPv6 jumbograms are skipped. No application payload, DNS query, URL, port history or reverse-DNS lookup is retained or performed.

LAN traffic is counted **once on ingress before NAT**, only for unicast frames addressed to the gateway MAC with a source IP on its LAN prefixes. The observed source MAC identifies the LAN sender, not a verified human identity; routers behind that MAC can hide additional devices. Gateway-host egress requires the source interface MAC, a host-assigned IP, and an exact connected TCP/UDP tuple in `/proc/net/{tcp,tcp6,udp,udp6}`. Forwarded packets after NAT are not assigned to compai merely because they carry its upstream IP. Tuple lookup is refreshed every second and bounded to 8,192 entries; unconnected UDP, short-lived sockets, other protocols and fragmented host packets can be missed. Captured LAN fragments still contribute their own IP bytes. No response traffic is inferred. Existing monitor probes can appear as real traffic.

`capture_config.py` publishes local addresses, interface MACs, and directly routed prefixes (including globally addressed IPv6 on-link/delegated prefixes) from host `ip -j` address/routes into ignored `.runtime/capture-config.json`. This refreshes with each inventory scan. Default routes are not local prefixes. Multicast, broadcast, loopback, private/reserved/shared space and all discovered local networks are excluded from internet destinations. Configuration older than 90 seconds disables attribution and exposes a capture error; no stale network assumptions silently continue.

The collector retains **1,024 source/destination pairs**, evicts least recently observed pairs at capacity, and expires each after **five idle minutes**. Byte totals cover the retained lifetime of each pair, not an unlimited history or a rolling five-minute total. Rates are outbound IP bytes over the latest approximately one-second sample, in decimal Mbps; peer counters remain captured Ethernet frame bytes. A zero means no matching packets in that capture interval only. Unseen/unavailable traffic is unknown. Capture is best effort, excludes traffic switched inside ASUS or using other gateways, and may miss packets under load. Counts are observations, not billing totals or physical wire capacity.

The UI displays at most **75 destinations / 200 traffic relationships**, ranked by retained outbound bytes and prioritizing the selected local source or selected retained external destination. Dotted orange-node relationships show observed source → destination IP traffic, **not direct cables, IP-hop measurements, available bandwidth or link capacity**. Actual infrastructure links remain separate. Selecting a destination is a passive graph perspective: no external destination is added to device discovery, SSH observation, web-test or probe target lists. Details show source, interface, attribution, first/last timestamps, retained bytes, latest Mbps and scope. Capture snapshots older than ten seconds, errors, or expired pairs no longer supply current destination relationships. Existing coordinates/pan/zoom are preserved as inventory changes and on selection; reduced-motion behavior is unchanged. A bounded 2,048-entry, five-minute position cache preserves continuity when nodes leave/re-enter the display cap.

## Router custom-firmware analytics

Authenticated read-only inspection of the current router at the private configured address reports **ASUS RT-AX86U**, `firmver=3.0.0.4`, `buildno=388.12`, `extendno=2`, **AP/bridge mode (`sw_mode=3`)**. Firmware capabilities and units were checked against its served `Tools_Sysinfo.asp`, `Main_TrafficMonitor_realtime.asp` and JavaScript. There is no firmware/configuration mutation, shell execution, flashing, reset or installation. Login is the only session action. Credentials stay exclusively in `~/.config/lan-monitor/router.json` (0600), never in the container, source or API.

Every 15 seconds `router.py` reads bounded, whitelisted telemetry:

- `appGet.cgi?hook=get_clientlist()`: current associations, band, PHY TX/RX Mbps, signal dBm and firmware connection duration. These PHY rates describe negotiated/reported radio rates, not traffic throughput.
- Whitelisted individual `nvram_get()` fields: model, firmware version, mode and LAN MAC, cached for five minutes.
- `ajax_sysinfo.asp`: load averages over 1/5/15 minutes (not CPU percent), memory total/used/available/free in the firmware's labeled MB, and router conntrack/active counts. AP conntrack counts do not represent all LAN sessions.
- `ajax_coretmp.asp`: numeric CPU and available radio temperatures in °C. Disabled sensors stay unknown.
- `appGet.cgi?hook=netdev()`: aggregate BRIDGE/WIRED/WIRELESS0/WIRELESS1 byte counters and measured deltas in decimal Mbps, from router RX/TX perspective. First samples, resets and gaps over 60 seconds have unknown rates. These groups overlap: **never sum them or add them to gateway capture**. They can reveal aggregate activity not visible at compai, but cannot attribute switched traffic to a client/destination.
- `ajax_conntrack.asp`: the current firmware exposes empty connection and route arrays in AP mode. `get_clientlist()` totalTx/totalRx fields are also empty; no per-client byte totals or router destination records are invented. `get_ethernet_ports()` exposed no data during inspection; no Ethernet PHY capacity is inferred. WAN router service/health/capacity is not applicable in AP mode; compai is the internet gateway.

Select **ASUS access point** for firmware health, counters, sources and timestamps; select a client for association/rate/signal/duration. Values older than 45 seconds are marked stale; endpoint failures are exposed without discarding other successful telemetry. Firmware JavaScript responses are parsed only as whitelisted numeric arrays/assignments, never evaluated. Router metrics are separate from physical graph capacity and gateway-derived destination edges.

Validation: `npm test`, `python3 -m unittest discover -s tests`, Python/JavaScript syntax checks, and live API/browser checks. No `build.sh` exists; this project serves bind-mounted source directly, so no alternate build command is used. Restart `lan-monitor` for Python server/router changes and `docker compose restart traffic` for collector changes; view edits take effect on reload without a container recreate.


Device history is retained in `.runtime/device-history.sqlite3` on the host and survives
collector/container restarts. It records MAC identities, Unicode names and aliases with
source/timestamps, IP addresses, previously responding ports, and last confirmed reachability
or ASUS association. Offline ASUS client records and the firmware remembered-device database
are imported; the latter refreshes every five minutes. “First recorded” is when this collector
first obtained evidence, not the original connection date. Cached neighbors and remembered
clients do not advance confirmed reachability. Private/randomized MAC addresses remain separate
records. History is searchable in the dashboard and retained without automatic expiry; it does
not create physical links or claim that remembered devices are currently connected. Back up the
SQLite file using SQLite's backup API if the collector is running.

The **Geographic map** checkbox pins particles to an offline Natural Earth land map
(equirectangular projection). Public-IP estimates are looked up over HTTPS through
https://ipwho.is/ only when the checkbox is enabled, at one request per second with
a maximum of 100 public addresses per refresh. Successful lookups are cached for
seven days in `.runtime/geography.json`; failures retry after an hour. Private,
link-local and multicast addresses are never submitted. LAN MAC identities share
the gateway's approximate public-IP site; this is not individual device GPS.
Unlocated nodes remain in the device list and are not drawn on the geographic canvas;
connections involving an unlocated endpoint are omitted from that view. Colocated
nodes keep their real projected anchor; stable pixel offsets and collision packing
separate the visible node rings without changing geographic coordinates. Tethers
show any displacement from the anchor. Geographic zoom now reaches 64×, with
Zoom + / Zoom − controls for touch and keyboard use. Fit uses only world-map bounds, never unlocated nodes. Location source and
observation time appear in the inspector. Geographic mode pins positions even
when animation is paused; pan, zoom, node selection and traffic styling remain.
Land geometry: Natural Earth `ne_110m_land.geojson`, public domain, downloaded from
https://github.com/nvkelso/natural-earth-vector/tree/master/geojson .

Selecting a remote IP starts an asynchronous public-IP identity lookup. The inspector
shows reverse hostname (or explicit resolver failure/no result), network operator,
ASN, ISP/network domain, approximate city/region/country, sources, and lookup time.
Resolved operators/hostnames label particles and can be searched alongside the raw
IP. A network domain is not attributed as the visited website: CDN/proxy addresses
can serve many domains. Results persist in `.runtime/remote-ips.json`; successful
network lookups are cached seven days, failures one hour. Requests are queued and
paced one second apart, with at most 100 pending. Private IPs are rejected. Reverse DNS uses bounded `dig -x` PTR queries through the configured DNS resolver;
network metadata comes from HTTPS ipwho.is. Observed remote destinations and route
hops are queued automatically, without selecting each node. Successful PTR answers
refresh at their TTL (bounded to 30 seconds–24 hours); missing records retry after
an hour, DNS failures after a minute. The inspector shows all PTR names, DNS status,
resolver, command and TTL/retry interval. Network-owner metadata is cached separately
for seven days and is never presented as a DNS hostname.


Website request analysis for CodeCollective and LifeTech is available at
`https://orgportal.cc/admin?tab=stats`. See [WEBSITE-OBSERVATORY.md](WEBSITE-OBSERVATORY.md)
for evidence semantics, retention, hourly collection, runtime dependencies and the
path to server/edge/real-user telemetry.


**Group providers** is available on the LAN map and website observatory. LAN remote
endpoints group using verified ASN metadata or explicit DNS/domain suffix families
(Google, Cloudflare, Facebook/Meta, Apple, Microsoft, Amazon/AWS, Canonical), with
exact network-domain/ASN fallback. LAN devices stay separate. Website origins group
by explicit domain families, not a hosting provider's IP ownership. Group inspectors
retain member endpoints/origins and traffic totals; rendered links deduplicate
unordered pairs while retaining underlying relationship evidence. Multiple
geographic locations are not collapsed onto an invented provider coordinate.
Toggle grouping off to inspect the original individual nodes.

Map controls, DNS visibility/status, legend, benchmarks and physics now live in the
left settings column; the selected device, device list and history remain on the
right. View settings are saved under `lan-map.settings.v1` in browser localStorage:
topology layer, geographic mode, provider grouping, additional names, pause choice,
automatic remote lookup, and repulsion. Preferences are per browser origin, so
`map.local` and the numeric host address have separate settings. Reduced-motion
preferences remain respected. Corrupt/blocked storage falls back to session defaults.
The DNS summary distinguishes IPv6 PTR names, no-PTR responses and pending/failing
lookups. Grouping hides member endpoint labels by design. Automatic lookup queues
unique active destinations rather than the first 100 source/destination pairs;
one lookup/cache-write failure cannot terminate the resolver worker. Pausing lookup
only stops requests from this browser; other viewers can still request resolution.

The distributed tests page includes **Device levels**, an overlaid live chart with
Network, Disk, and RAM choices. Colors follow device identity across views;
legend buttons toggle individual traces. The metric choice persists locally.
The chart builds up to five minutes of in-tab history and leaves missing samples
as gaps. Network displays receive + send Mb/s: saved SSH hosts report totals
across physical interfaces, while other LAN devices use gateway-observed Ethernet
frames. These scopes differ and must not be summed. Disk displays the busiest
physical block device's Linux I/O busy percentage, not storage space or an
inferred saturation score. RAM displays `(MemTotal - MemAvailable) / MemTotal`.
Disk and RAM require a reachable saved SSH account. Unmeasured devices remain
listed, and samples older than 15 seconds are unavailable.

**Where throughput comes from** on `/tests.html` reports bidirectional Internet
flow accounting at compai. Download rankings group remote source IPs; upload
rankings group local devices. Direction and device filters apply before share
calculation. The table lists source/destination addresses, transport protocol and
ports, current IP Mb/s, share of the filtered observed traffic, retained bytes,
interface, observation method, and last seen. PTR and ASN labels describe network
ownership, never verified application identity. Rates use the latest capture
interval (usually one second), refreshed in the browser every three seconds.

LAN uploads are attributed at ingress before NAT and downloads at LAN egress
after NAT. Host packets require an exact connected kernel socket tuple in the
appropriate direction, excluding forwarded traffic. The bounded bidirectional
cache retains at most 1,024 records, expires after five minutes idle, and exposes
record evictions. Fragmented/non-TCP/UDP traffic can lack ports. Retained bytes
cover each record's lifetime, not a chart window. Kernel packet-socket drops are
reported per interval and since collector start; zero drops do not establish
complete visibility. Other gateways, switched LAN traffic, unconnected host UDP,
and sockets absent from the quarter-second socket inventory remain outside coverage.
No packet payloads are stored. The presentation follows IPFIX observation-point
and flow-key conventions (RFC 7011) without claiming to implement IPFIX export.

The source ranking can also group by **Host process**. A host-side `ss` inventory
joins recent exact socket tuples to process name and PID on compai. It returns only executable/script identity, never full command arguments,
environments, or remote process inventories. Unattributed
traffic remains in the denominator, and sockets with multiple owners are counted
once as a shared group. Ownership snapshots refresh every half second and are
not proof of ownership for all retained historical bytes. Grouping, direction,
and device filters persist in browser localStorage.


Process attribution now includes an interpreter's script filename (for example,
`recovery.py`) and a **Local files & database destinations** section. A bounded
snapshot of `/proc/PID/fd` and `fdinfo` reports unique regular-file paths and
permitted read/write access, prioritizing database filename hints. It does not
read file contents or assign network bytes to individual files. Read/write
access is capability, not observed I/O. Snapshots include at most 32 files per
process, scan at most 2,048 descriptors and cover at most 64 host processes with
visible connected sockets. Truncation and inaccessible descriptors are explicit.

SQLite/database suffixes are filename hints, not format verification. Connected
TCP endpoints on known database ports show address, port, an inferred engine,
and local/remote service role. Database names remain unknown. No SQL, file
contents, connection strings, credentials, or remaining command arguments are
returned. Resource context is available on compai; remote file names and
socket-to-file or socket-to-database-name mappings require application
instrumentation. File context refreshes separately every three seconds and expires after
ten seconds. Opening a process card reveals its file and database details.

### Distributed image preparation

Before dispatching suites, the coordinator refreshes each required registry
reference once and rebuilds derived images against the prepared base IDs.
Matching worker image IDs skip transfers. Changed images use checksummed encrypted agent uploads, with two destinations at a time.
Each worker records and executes its verified local image ID with `--pull=never`.
Image preparation is cancelled by **Stop all tests**.

Preparing images does not run a suite; stopped runs remain stopped. These changes
apply on the next explicitly requested run after the coordinator loads this code.

### Live container website view

On the main tests page, select a machine, open a running container, and choose
**Website** beside **Console**. Publicly bound web ports appear in the port
selector. The embedded website remains live and interactive while machine
observations refresh; **Reload** refreshes only the website. **Open in new tab**
is available for sites that disallow embedding or need browser sign-in.

A custom HTTP/HTTPS website or gateway URL can be entered for services reached
through another container's gateway. Loopback-only bindings are identified rather
than presented as reachable LAN endpoints. Containers without a published web
endpoint still offer their console and a custom URL field. Selecting Website
does not start a server, container, test, or deployment.

### Cross-device WebRTC tests

`Start all tests` includes a coordinated **Cross-device WebRTC P2P** suite after
application shards finish. It requires at least two distinct authenticated agent
machine identities. Each participant runs a disposable, non-root browser container with
all Linux capabilities dropped. This dedicated suite uses the host network for
real ICE connectivity; ordinary application test containers remain offline.
Only public SDK source is mounted. The coordinator keeps the enrollment signing
key and sends browsers short-lived invitations for a synthetic dataset.

The suite checks SHA-256 of received bytes, the selected ICE candidate pair and
physical-interface addresses, and RTCDataChannel byte counters. Loopback,
hidden/unverifiable routes, duplicate machine identities, and TURN relay paths
cannot satisfy the direct-P2P check. It also checks reseeding with the original
sender disconnected, repairing a missing cached chunk, and WASM consumption of
received fixtures. Cross-NAT and TURN coverage are reported separately; a LAN
pass does not establish either. Device clocks must be synchronized within one
minute so expiring invitations have consistent semantics.

The network suite runs through Start so SDK and fixture sources use the same published Organization commit as application tests.

Reports contain machine fingerprints, tested SDK hash, fixture hash, selected
routes and byte counts; invitations and signing keys are excluded. Normal
integrated reports follow the distributed runner's retention policy. A manually
chosen report remains until its operator removes it.

Browser images use the same refresh, content verification, and encrypted
agent delivery as application images. The SDK transfer is a small source-only bundle, separate
from container images. Browsers execute the verified local image ID with
`--pull=never`; Docker's ordinary image store remains necessary.

The coordinator participates through its own enrolled agent, using the same readiness checks and job protocol as other machines.

Distributed-run preparation verifies enrolled launcher agents, excludes unavailable machines and redistributes suites. Provisioning and updates are explicit operations outside Start.

The API loads current coordinator modules on each new start through `live_distributed.py`; active runs retain their implementation. Agent process code changes only through explicit installed releases.

The Network level chart includes **Docker images** operation labels. Exact image
references from the test coordinator distinguish registry pulls, LAN image
transfers, builds, and cached/ready images. Historical test selections do not
appear as current coordinator activity, and coordinator labels expire if live
polling stops. A read-only `DockerDownloads` worker also observes explicit local
`docker pull` / `docker image pull` command references and recent image pull
events from the local Engine socket. It never initiates a pull or reads registry
authentication. Event history is in memory for 15 minutes, capped at 64 records;
Docker itself retains only a bounded event history. Command observations mean a
pull CLI is active, not that bytes are moving, and ambient client daemon contexts
are not verified. Explicit alternate contexts are excluded. Compose, BuildKit,
API-initiated pulls and remote hosts may be absent unless the coordinator reports
them. The chart's device rate includes all traffic: no per-image Mb/s or layer
progress is invented. General host observation requires loading the updated
collector; coordinator labels use the existing test-context feed.

The distributed-tests page embeds the live particle map above execution details. It shows all discovered LAN devices, including inventory without SSH access, with a node count, searchable device inspector, and fit/zoom/pause controls. External traffic destinations stay in the full network map. The embedded view uses the same discovery feed and does not start tests or scans.

Each distributed run refreshes every required registry image once on the
coordinator before distributing it, and rebuilds the derived toolchain from its declared bases. Docker reuses unchanged layers;
unchanged peer contents are not retransmitted. A registry refresh failure stops
preparation rather than silently testing an old tag. Version and digest pins
remain explicit; refreshing does not upgrade a pinned version to another release.

Start commits and pushes the selected source repositories (Organization, OrgPortal and PIdP) on their existing branches, verifies the GitHub branch tips, and records their exact commit IDs. It uses only fast-forward reconciliation; divergence, rejected pushes, unresolved conflicts and sensitive/runtime files stop publication with an explicit error. Repositories publish independently: a later failure does not undo an earlier successful commit/push.

Every verified local worker initializes fresh checkouts, fetches the three pinned commits directly from GitHub, verifies FETCH_HEAD and HEAD, and records per-repository synchronization events. Existing working copies are untouched. No GitHub-hosted runner or SSH execution path participates. Private repositories require independent worker-side GitHub access; coordinator credentials are never copied into jobs. Test failures are reported, not automatically edited or committed as repairs. The worker verifies its daemon can read the exact run manifest through its host-local bind mount before executing suites.

Map edges render up to two minutes of in-tab throughput history along the link.
Each trace auto-scales independently; its hover card reports current rate, recent
peak, and measurement scope. Endpoint relationships use matched bidirectional IP
flows when available; other links keep their actual interface measurement scope.
Unknown or stale data leaves gaps, not zero-rate samples. Time has a stable
orientation based on endpoint identity. History is bounded and disappears when
this tab closes. Hover an edge or tap it to inspect; Escape dismisses the card.

Hover download context ranks matched flows by current download rate. Where a
recent socket owner is known, its process/script and open writable filenames are
shown as receiver context, with database endpoint hints where available. These
are not proven per-flow file destinations. Concurrent Docker image operations
appear only for relevant host/interface context or a Docker-owned socket, and
are explicitly not assigned a per-image rate. The map reads existing coordinator
observations; it does not start tests or network benchmarks.

**All observed connections** switches to a topology-independent schematic view.
It shows all retained captured endpoint pairs, including incoming-only flows,
without the normal 75-destination / 200-relationship display cutoffs or the
selected perspective's connected-component filter. Multiple sockets between the
same device and remote IP remain one graph edge, with summed upload/download
rates. Provider grouping remains optional. The choice persists in localStorage.
Geographic and IP-hop layouts are paused while this mode is active, so unlocated
endpoints and disconnected observed components stay visible. This means all
available observations, not every physical or switched connection: collector
retention, its flow limit, socket visibility and capture coverage still apply.

Run history displays the latest ten independent records, including per-machine suites and bounded logs. Selecting a run reads that run ID and never replaces the latest run. Existing retention remains 30 days/up to 100 saved records; limiting the visible history to ten does not delete older records early.

### Agent image distribution and package caches

Distributed tests reuse worker images by immutable ID and transfer changed images through the authenticated agent API. Temporary image archives are removed after import. At most two destinations receive an image concurrently. Package caches remain separate from installed dependencies.

Workers retain npm/pip download caches under
`~/.cache/organization-tests/packages/`, separated by suite, checkout lockfiles,
image ID, and CPU architecture. Installed dependencies remain isolated in each
run's source directory; npm still uses `ci`. Cache files stay on their owning
Docker host and are mounted into that host's containers. Removing the package
cache directory while no tests are running forces fresh package downloads.
Registry storage and package caches persist independently of the 30-day reports.

Loopback registry handling follows the [Docker daemon registry policy](https://docs.docker.com/reference/cli/dockerd/#insecure-registries).


Download attribution now keeps bounded socket-owner observation windows rather
than only the latest `ss` output. Ownership is sampled every 0.5 seconds,
independently of the three-second open-file scan. The packet collector refreshes
its connected socket inventory every 0.25 seconds. A five-minute, in-memory
ownership history (4,096 tuples, up to four identity windows each) lets a closed
connection retain its observed process/script label while its flow is retained.

Matches require the exact protocol/address/port tuple and an ownership window
within 1.5 seconds of the last captured packet. This is temporal correlation,
not a kernel event joining every packet to a process. Conflicting identities
are **ambiguous**, never arbitrarily assigned. Process start ticks distinguish
PID reuse and prevent joining a retained owner to a new process's open files.
Observations without a readable process start identity are excluded. Labels
explicitly distinguish **live**, **retained**, **ambiguous**, and **unknown**
evidence; they do not attribute the entire historical byte count to one owner.

The throughput page reports the fraction of *observed compai download rate*
with a process match. Its denominator excludes remote devices and unseen traffic,
so 100% is not proof of complete network visibility. Resource filenames remain
current process context; exact per-file, image, or database-name attribution
requires downloader/application transfer events or equivalent instrumentation.
These source changes require reloading the collector and packet monitor to take
effect; neither is automatically restarted by editing the files.

The existing **Run history** panel on `/tests.html` now includes a saved-reason
preview on each of its ten run cards and a **Why this run ended** explanation for
the selected run. It shows the recorded coordinator or host error first, groups
matching suite errors, includes phase/exit status, source revision and snapshot,
and provides bounded diagnostic excerpts with direct navigation to recorded
suite output. Infrastructure shutdowns without suite-specific error evidence
are counted as blocked work, separately from suite failures, unexecuted jobs,
timeouts, unavailable workers and deliberate stops. The last image preparation
operation is context, not an inferred cause. Missing logs remain explicitly
unknown; no diagnosis is invented from an empty log.

Reason previews read existing saved reports, at most two concurrently, and cache
summaries for the ten displayed runs. They do not start or rerun any tests and do
not require a collector API change. Recorded terminal output includes captured
failure details as well as its bounded log tail. Historical unfinished job
statuses use the run's saved end/update time for duration, not the current clock.

### Authenticated launcher agents

Routine distributed runs use the existing launcher's encrypted `lifetech-agent.v1` API on TCP 8790, discovered through `_lifetech._tcp.local.` DNS-SD/mDNS (UDP 5353). Announcements contain only node/protocol metadata. They never enroll or authenticate a device. Missing multicast announcements can use an already-enrolled address, followed by the same authenticated readiness check. The coordinator requires its enrolled address on `enp7s0` and only schedules endpoints on that Ethernet subnet.

The enrollment file supplies the fleet credential and enrolled addresses. This is fleet-level trust: possession of that credential grants agent execution control. Requests and responses use AES-GCM, protocol-specific authenticated data, request IDs, freshness limits and replay rejection. There is no shell-command endpoint or routine SSH fallback. SSH remains in the explicit enrollment/provisioning workflow and unrelated network monitoring collectors.

Start checks the running source revision, Docker availability, physical addresses, machine fingerprint, capabilities and busy state. Current agents are reused. Offline, duplicate, outdated or incapable agents are excluded with a stage/reason; application suites are reassigned to surviving verified agents. Readiness is checked again after image preparation. Successful remaining work with exclusions is **Partial coverage**, not a full-fleet pass. One worker can run application suites; multi-device P2P remains explicitly unmeasured.

Source code is fetched directly from GitHub by exact commit; image archives and synthetic P2P fixtures use bounded, ordered 1 MiB encrypted chunks and SHA-256 verification. Agents reject unsafe archive paths/links, enforce staging limits and available disk space, and execute immutable worker image IDs. Jobs have run IDs, sequence-numbered event polling, cancellation acknowledgements and a two-hour stop deadline. An event-history gap cannot produce a verified result. Agent restarts mark running work interrupted and clean up owned test containers. Reports distinguish infrastructure unavailability, excluded machines, missing coverage and suite failures.

Image delivery currently stages a complete image tar temporarily at both ends (maximum 8 GiB per upload); it skips images already present by exact ID. It does not yet provide registry-layer delta transfers. P2P browser setup, WebDriver control, fixture delivery and cleanup use the agent API; Selenium remains loopback-only. Application containers are not restarted by Start.

Agent sources update separately from tests. On an enrolled machine, `python3 scripts/launcher_update.py` atomically stages the current peer-agent sources; `--rollback` selects the previous installed release. The supervisor drains new jobs, waits for active jobs/browsers to finish, validates source checksums, and restores the previous release if the replacement fails its startup readiness check. An unchanged installed release does not reload. Updates to image dependencies or the supervisor itself require an explicit launcher-image rebuild/recreation. Initial activation of this migration also requires that rebuild on each machine; editing the coordinator files alone cannot upgrade existing agents.

### Commit, push and test

The Start control is labeled **Commit, push & test** because it publishes local changes before testing. The run report shows each repository's publication phase and commit ID; each machine shows its fetched/verified revisions. Authentication, push rejection and history divergence are preparation errors; a worker GitHub-fetch failure is a machine error while other workers continue. Outdated or unavailable agents are excluded before scheduling. A second readiness check after publication redistributes suites if a worker disappears. No publication, test or deployment happens simply by editing these files.

The distributed master exposes authenticated REST at `/api/v1/master` and MCP Streamable HTTP at `/mcp`. See [MASTER_API.md](MASTER_API.md) for Codex configuration, token setup, operation semantics and the OpenAPI endpoint. Starting via either interface has the same GitHub publication and test effects as the dashboard.
