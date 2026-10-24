# Swarm Control on Android

This is a separate debug test launcher (`app.swarmcontrol.test`), not the
OrgPortal APK. It opens the CompAI console using Android Custom Tabs and the normal browser
session. Its browser menu includes **Swarm Control P2P connection**, which returns
to `https://compai.lifetech.fyi/swarm-control`. It uses a connected-node swarm icon. No isolated WebView login or device administrator is introduced.

The CompAI console delivers the APK to a paired phone over an authenticated,
short-lived WebRTC data channel. HTTPS carries the offer/answer; APK bytes travel
through the channel. The browser verifies SHA-256 before creating the download.
A separately labeled HTTPS download is available if direct connectivity fails.
There is no TURN relay configured. Android installation requires the user's
approval on the phone; a download receipt does not prove installation.

## Build and publish

From `swarm-control`, run `npm run build`. This builds and verifies the
APK signature, then atomically publishes `.runtime/mobile/swarm-control-test.apk` in
the parent Organization workspace. SDK API/build-tools 36 and JDK 21 are required.
Set `ANDROID_HOME` and `SWARM_JAVA_HOME` if they are installed outside the
existing compai toolchain caches. Gradle uses its cached distribution/dependencies
in offline mode. An uncached machine must provision those dependencies first.

For initial owner-server setup, run from Organization:

```sh
python3 lan-monitor/setup-test-deployment.py
```

It installs the pinned optional P2P runtime in an isolated user venv and sets a
user-service override for the existing `compai-codex.service`. Pairing and tunnel
credentials remain where they already live. Existing paired browsers stay valid.

## Receive on the phone

1. Open `https://compai.lifetech.fyi/swarm-control` in the paired console browser.
2. Tap **Connect P2P**, then **Receive test APK**.
3. Open the downloaded APK and approve Android's installation prompt. If requested,
   permit this browser to install the test APK using Android's normal settings.
4. Launch **Swarm Control**. Its browser menu opens the CompAI P2P test panel.

The console home also includes the deployment panel after a page refresh. Merely
having a paired console session does not establish a WebRTC channel or permit
silent app installation. This connection cannot enable ADB, control the handset,
execute shell commands, or perform a background installation.

## Verification

`node test-console.mjs` runs a real browser-to-aiortc channel through an
isolated local owner-console fixture. It transfers the published APK, compares
all bytes, validates the browser checksum and server receipt, checks the narrow
layout and disconnects. No existing browser cookies or phone state are used.

`SWARM_LIVE_CONSOLE_TEST=1 node test-console.mjs` repeats the transport
check through the live `compai.lifetech.fyi` server using the local owner's
credential, injected only into the fixed test-app API requests. It does not read
Codex conversations, issue Codex commands, or claim physical-device installation.
Reports/screenshots are saved in `artifacts/swarm-control-deployment` in the
parent workspace. The report explicitly identifies that it is a desktop browser
transport test, not an S22 result.

Python boundary tests live in `lan-monitor/tests/test_lifetech_deployment.py` and
`test_codex_remote.py`. Run them with `PYTHONPATH=lan-monitor` and the P2P venv.

## Retention and permissions

The server exposes a fixed APK, never an arbitrary file or URL. Owner credentials
or an existing paired console cookie are required; offers require the same
origin. The channel supports only ping, manifest, bounded chunk requests, and a
checksum receipt. Sessions are limited to four connections, ten minutes, a
16 MiB APK, 16 KiB chunks, and bounded request rates. The device's authorization
is checked on each message. Revoked cookies cannot continue downloading.
Connection/receipt state is held only in memory and removed on close, failure,
expiry, or server restart; it is not included in backups. No phone identity,
conversation content, or credentials are logged by the deployment channel.
The fixed APK remains until replaced/deleted by the owner; test artifacts contain
only the public app bytes and synthetic/browser transport results. The phone's
Downloads file and installed app remain until the user deletes/uninstalls them.
Rebuild/re-receive for a new native launcher; live website content follows the
website's normal update cycle.
