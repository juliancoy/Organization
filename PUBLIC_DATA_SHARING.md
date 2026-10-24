# Public data over LAN and existing P2P

The public sharing path is separate from authenticated working copies, fixture
databases and DR archives. It exports only the existing unauthenticated OrgPortal
version-3 public replication feed: organization metadata, public support/evidence
relationships and financing projections. It does not export memberships,
invitations, private contacts, chat, account newsletters, credentials or sessions.
This feed does not currently include events or binary media.

```sh
./peer-share prepare-public --output .local/public-data/my-snapshot
./peer-share serve-public --package .local/public-data/my-snapshot --bind 10.42.0.1 --port 8792
```

Use the machine's current LAN IP. The server defaults to loopback unless a bind
address is explicitly supplied. Anyone able to reach the chosen address can read
the public snapshot; no account or device invitation is required for LAN reads.

- `/snapshot.json`: the validated public snapshot.
- `/manifest.json`: the existing content-addressed peer package manifest.
- `/chunks/SHA256`: only verified chunks referenced by that manifest.
- `/health`: dataset hash, public visibility and expiry.

There is no directory listing, file upload, private sync route or filesystem path
mapping. The server validates and pins the package bytes before listening. It
rejects non-public package kinds, unexpected fields, symlinked chunk files and
corrupt hashes. It never accepts authentication headers when fetching its public
source and does not follow redirects. Keep it bound to the LAN interface.

Public packages expire after 24 hours. This is a point-in-time snapshot, not a
background bidirectional sync. Prepare a new output directory and restart the
server to refresh it. Receivers must not overwrite private working copies with
this public projection, infer production write permission, or push it upstream.

For WebRTC transfer, reuse the existing peer invitations and directory seeding:

```sh
./peer-share invite --package .local/public-data/my-snapshot --devices 2 --output .local/public-data/invitations.json
```

Open a separate invitation on each device and seed the prepared package directory
from the sending device. Invitations remain private, expire independently, and
authorize only their immutable dataset room. A refresh has a new dataset hash and
requires new invitations. Archive bytes still travel on the existing verified
RTCDataChannel, with the existing direct/STUN/TURN connection policy.

The peer browser source now recognizes `kind: public`, enforces the fixed
`public-snapshot.json` path and size/expiry bounds, and cleans up expired public
copies. The workspace release pipeline deploys the peer browser and verifies its
source hash at `https://organization-archive-peers.jcloiacon.workers.dev/peer.js`.
LAN serving works independently of a Cloudflare deployment.
