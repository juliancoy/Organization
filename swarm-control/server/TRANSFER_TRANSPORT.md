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
fingerprint before tagging. On a cold start, one recipient is seeded before parallel delivery to the rest.
Later recipients try enrolled cached peers first,
then the coordinator. Cache misses or unavailable peers fall back per object.
`image.pull` returns source byte counts; transfer progress includes those counts.

Explicit agent updates synchronize the coordinator's already-enrolled peer list
so recipients can authenticate one another. No discovered device is added.
Agent updates use the existing atomic release supervisor. Busy agents retain
their running workloads and switch releases when idle. Old agents remain TCP
compatible; new distributed work requires the peer-cache and MPTCP transport
capabilities so an old agent cannot silently pass as an upgraded one.


## Archive SSH transport

Agent/image MPTCP does not convert existing SSH streams. The WGS archive opts in
through its private `archive/transport.json`, naming the enrolled destination and
port 8792. SSH's ProxyCommand connects to an owner-only Unix socket. Inspiron
initiates a mutually HMAC-authenticated MPTCP connection back to Compai, using its
existing Ethernet/Wi-Fi subflow endpoints. The bridge carries unchanged SSH bytes;
existing SSH keys and host-key checks still protect archive content end to end.
The reverse service connects only to Inspiron's loopback SSH port, after Compai
requests a session. No routing changes or administrative privileges are needed.

The two user services are `swarm-archive-multipath.service`. Their shared transport
key is in the owner-only Swarm Control configuration directory, never source or
logs. Stop/restart an archive copy only through its own service; `.partial` data
is retained and resumed, and both SHA-256 and MD5 must match before publication.
Removing `archive/transport.json` and restarting the archive restores direct SSH.
