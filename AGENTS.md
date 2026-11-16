# Workspace instructions

## LifeTech email campaigns

- Maintain LifeTech's private OrgPortal organization mailing list (`ef646755-9443-4c7b-ba4b-a7a29754f666`) as the source for future campaign audiences. Import authorized external audiences with their source recorded, using live organizer permissions and the exact preview/apply receipts. Preserve existing opt-outs, suppressions and bounces; do not create accounts or memberships for email-only contacts.
- Every future campaign email must include a working recipient-specific unsubscribe link that updates LifeTech's private email preferences. Prefer the managed campaign sender, which checks preferences before delivery and adds unsubscribe headers. For an explicitly authorized external send, use `prepare_email_contact_link` for the exact recipient and verify current subscription, suppression and bounce eligibility before sending. Never reuse another recipient's link or send an unresolved unsubscribe placeholder.
- Keep audience addresses, unsubscribe tokens and send receipts private and outside public repositories, Pages and organization directories. Honor unsubscribe replies as well as link-based opt-outs. Sending still requires user authorization; a list import never authorizes email delivery.

## Network

- This is a mixed IPv4/IPv6 network. Support for both address families is required because of the `xfinitywifi` upstream link; fleet connectivity must not assume IPv4-only operation.
- Use enrolled hostnames and mDNS/DNS-SD discovery for fleet access. Do not hardcode node IP addresses or force IPv4 (`ssh -4`, `AF_INET` only, or equivalent) in standard discovery, deployment, health checks, or data transfers.
- Resolve and try viable IPv4 and IPv6 addresses with bounded connection attempts and fallback. Preserve IPv6 link-local interface scopes. Refresh discovery after interface or address changes; a failure on one address or interface does not establish that a node is offline.
- Keep local fleet routes distinct from the upstream Internet route. Inspect actual interface addresses, routes, name resolution, and reachability before changing network configuration. Do not assume that devices on different Wi-Fi networks have a route between them.
- Preserve MPTCP support and ordinary TCP compatibility. Establish a working initial connection across available address families; configure additional MPTCP paths only over verified reachable routes. Do not advertise unreachable addresses as usable paths or claim multipath operation without verifying negotiated connections and working subflows.
- Validate network changes from the actual service or container environment, including hostname-based access, available IPv4/IPv6 paths, fallback when a path disappears, and recovery after network changes. Report unavailable paths separately from node or application failures; retain existing authentication and firewall protections.

## Testing

- Use host-based tests or the local Docker deployment by default.
- Do not use a virtual machine (VM) for testing unless the user explicitly requests it.
- Log every test run alongside the code it tests. Record the date, command, result, and the tested code revision or content fingerprint so previous results can be matched to the code.
- Do not rerun tests for unchanged code. Consult the adjacent test log and reuse recorded results when the tested code and relevant dependencies, configuration, and test inputs are unchanged. Rerun only the tests affected by changes.
