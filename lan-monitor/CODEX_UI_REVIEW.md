# Swarm Codex remote-control UI

The workspace uses a restrained hierarchy: application identity and connection
status, conversation selection, thread context, messages, then the next instruction.
The desktop sidebar supports searching the current page of conversations. On a
phone, selecting a conversation opens the reading/composing view; Conversations
returns to the list without discarding the draft. Search is explicitly page-scoped.

Polling retains unchanged message DOM and the reader's scroll position, keeps a
manually selected delivery mode while activity is unchanged, and updates owner
connection changes. Draft-switch confirmation and command request IDs remain in
place. The browser keeps using the existing owner/device authentication flow.

## Review and checks

Run the synthetic browser review from the Organization root:

```sh
CHROME_PATH=/usr/bin/google-chrome node lan-monitor/scripts/review-codex-ui.mjs artifacts/codex-ui
PYTHONPATH=lan-monitor python3 -m unittest discover -s lan-monitor/tests -p 'test_codex_remote.py' -q
```

The browser review serves the actual Codex JS/CSS with synthetic thread data on
loopback. Its command endpoint is a local in-memory stub: no real instruction,
owner token, private conversation or production mutation is used. It checks
connection, search and empty search, thread selection, draft/delivery retention,
unchanged message DOM, single-send behavior, disconnected owner state, lock,
horizontal overflow and script errors at 390 and 1440 pixels. A twenty-thread
fixture also checks that pagination stays inside the workspace. PNGs cover locked,
list and conversation states. The fixture isolates the Codex panel and does not
claim to review the separate LifeTech deployment panel or real paired-device flow.

The distributed visual suite invokes this review through
`OrgPortal/web/scripts/render-layout-fixtures.mjs`; its six PNGs and JSON report
are collected with the existing visual artifacts. Review images manually: passing
checks do not establish aesthetic quality or full accessibility conformance.

Design basis: [IBM typography](https://www.ibm.com/design/language/typography/type-basics/)
for restrained emphasis and alignment, and [GOV.UK action guidance](https://design-system.service.gov.uk/components/button/)
for explicit labels and hierarchy. They inform the interaction design; the app
retains its existing dark, teal-accented identity.
