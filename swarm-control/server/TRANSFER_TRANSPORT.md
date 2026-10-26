# Swarm transfer transports

Encrypted agent HTTP connections negotiate Linux MPTCP when available and retain
TCP compatibility with older peers. The listener stays on the enrolled LAN
address. MPTCP uses the host's existing path-manager endpoints; this code does not
change routes, interface metrics, enrollment, or firewall rules. A connected
second interface alone is not proof of aggregation: verify established subflows
and transferred bytes on both paths.

Image recipients retain complete SHA-256-verified compressed layers and manifests
in the private launcher configuration directory's `layer-cache`. Retention is
bounded to 2 GiB. Incomplete objects are never served. Low free disk space skips
caching. Image content is still verified by Docker and by the expected image
fingerprint before tagging. Later recipients try enrolled cached peers first,
then the coordinator. Cache misses or unavailable peers fall back per object.
`image.pull` returns source byte counts; transfer progress includes those counts.

Explicit agent updates synchronize the coordinator's already-enrolled peer list
so recipients can authenticate one another. No discovered device is added.
Agent updates use the existing atomic release supervisor. Busy agents retain
their running workloads and switch releases when idle. Old agents remain TCP
compatible; new distributed work requires the peer-cache and MPTCP transport
capabilities so an old agent cannot silently pass as an upgraded one.
