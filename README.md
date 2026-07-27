# chrome-in-a-box

Real Google Chrome, running in an isolated container, used from a tab in your own
browser. One command up, one command down, and the profile survives restarts.

```bash
./run.sh up      # start it
./run.sh open    # open https://localhost:6901/?resize=scale
./run.sh down    # stop it (profile is kept)
```

It is a single Python file (`cib.py`) using only the standard library — no venv, no
`pip install`. `run.sh` is a shim, so `python3 cib.py up` works identically.

No login prompt. Accept the self-signed certificate once and you are in.

## Why

A browser can end up locked down by policy — password manager off, autofill off,
settings greyed out behind an "administrator" badge. That is fine for work, but it
also applies to everything else you do in that browser.

This runs a **separate** Chrome in a Linux container. Desktop browser policy does
not reach into a Linux guest, so this Chrome is unmanaged: the built-in Google
Password Manager, autofill and account sync all work normally. Nothing on the host
is modified, disabled or worked around — it is simply a second browser that happens
to live in a sandbox.

## How it works

```text
your browser  ──HTTPS/WebSocket──▶  KasmVNC  ──▶  Google Chrome
 (localhost:6901)                        └── container ──┘
```

The container is [kasmweb/chrome](https://hub.docker.com/r/kasmweb/chrome), which
serves a [KasmVNC](https://github.com/kasmtech/KasmVNC) web client. Keyboard and
mouse travel over a **websocket** — which matters, see [Design notes](#design-notes).

## Requirements

- podman or docker
- Python 3.10 or newer (macOS and every Linux ship one)
- On Apple Silicon: **Rosetta**, because Google ships Chrome for Linux on amd64 only.
  Without it, emulated Chrome is slow and crash-prone. To enable it for podman:

  ```bash
  mkdir -p ~/.config/containers
  printf '[machine]\nprovider = "applehv"\nrosetta = true\n' >> ~/.config/containers/containers.conf
  podman machine stop && podman machine rm -f podman-machine-default
  podman machine init --cpus 4 --memory 5722 --now
  ```

  ⚠️ Recreating the machine deletes all its images, containers and volumes. Only do
  this if it is not shared with other work.

## Commands

| Command            | What it does                                             |
| ------------------ | -------------------------------------------------------- |
| `./run.sh up`      | start the container, wait for the desktop, launch Chrome |
| `./run.sh down`    | stop and remove the container (profile is kept)          |
| `./run.sh open`    | open the web UI in your browser                          |
| `./run.sh status`  | show container state                                     |
| `./run.sh logs`    | show the last 200 log lines (`-f` follows instead)       |
| `./run.sh shell`   | shell into the container                                 |
| `./run.sh engine`  | print the container engine that will be used             |
| `./run.sh reset`   | delete the browser profile (asks first)                  |

`up` is idempotent: if the container is already serving it just re-applies the
resolution and revives Chrome, so your tabs survive. `CIB_FORCE=1` recreates it
instead, which is what you need after changing the image or an environment setting.

Overridable: `CIB_PORT`, `CIB_RESOLUTION`, `CIB_WAIT_SECS`, `CIB_ENGINE`,
`CIB_IMAGE`, `CIB_NAME`, `CIB_VOLUME`, `CIB_PASSWORD`, `CIB_LOG_TAIL`, `CIB_FORCE`.

## Passkeys — what does and does not work

**Passkeys cannot be forwarded from the host into the container.** A Touch ID
passkey is bound to the Mac's Secure Enclave, and WebAuthn deliberately offers no
way to relay that to another machine — the container is a different device with no
biometric sensor and no Bluetooth, so cross-device (QR code) sign-in is out too.
Anything claiming otherwise would be defeating the security property passkeys exist
for.

What **does** work: **the container holds its own passkey, in Google Password
Manager.** Chrome on Linux is a first-class GPM passkey platform — it needs no TPM
(on Linux Chrome deliberately stores the identity key on disk instead), no USB and
no Bluetooth. It syncs over the network, which is the only transport this container
has.

So when a site says *"no passkeys available on this device"*, that passkey lives in
iCloud Keychain and cannot come here. Register a **second** passkey from inside this
Chrome instead:

1. Sign into your Google account in this Chrome, and make sure
   `chrome://password-manager/settings` → *Offer to save passwords and passkeys* is
   on. Chrome refuses to create GPM passkeys otherwise.
2. Get into the site once by another route — a recovery code, a second factor that
   is not a passkey, or a session started on the host.
3. Add a new passkey / security key in the site's account settings. Chrome on Linux
   offers **Google Password Manager** by default; do not pick "this device", which
   dies with the container. Set the 6-digit GPM PIN and keep it somewhere safe: it
   is the only way to decrypt GPM passkeys on a new device.
4. Regenerate the site's recovery codes if you burned one.

The passkey then works both here and on the host, because it lives in your Google
account rather than on either machine.

**How much account to put in the box.** The profile volume is only lightly
obfuscated (Chrome runs `--no-sandbox`, and the image ships no keyring), so whoever
can read that volume or `exec` into the container gets every credential in the
Google account signed in there.

On a single-user machine you keep to yourself, that is the same trust boundary as
your own browser profile, and signing in with your normal account is a reasonable
call. Prefer a dedicated Google account when any of these is true: the host is
shared or managed by someone else, the port is exposed beyond `127.0.0.1`, other
people can run containers on that engine, or the account is one you would not want
to lose in one go.

Not possible, so do not spend an evening on it: forwarding a host passkey, the
QR-code / phone flow (the Bluetooth advertisement is an input to the handshake — no
radio, no ceremony), passing a USB security key into the VM (Apple's virtualisation
framework exposes only mass storage today), and exporting an iCloud Keychain passkey
into Google Password Manager (Chrome ships no importer).

## Design notes

- **Why KasmVNC and not a WebRTC-based streamer.** WebRTC-based remote browsers
  (Neko, Selkies) send input over a WebRTC data channel, which ad-blockers and
  WebRTC-leak-prevention extensions silently block: the screen renders, "take
  control" succeeds, and nothing you type arrives. KasmVNC uses a plain websocket,
  which those extensions leave alone.
- **Why real Chrome and not Chromium.** Chromium is arm64-native and faster, but
  third-party Chromium builds ship without Google's API keys, so there is no
  account sync and no Google Password Manager. That is the whole point here, so the
  amd64 Chrome image plus Rosetta wins.
- **Why the resolution is fixed at 1920x1200.** KasmVNC only ships modes up to that
  size; larger values silently fall back to 1024x768. `?resize=scale` then scales it
  to your window, which stays crisp on a HiDPI display.
- **Why there is a password despite no login prompt.** KasmVNC refuses to start with
  a password under 6 characters, so one is set and the prompt is disabled with
  `DisableBasicAuth`. The port is bound to `127.0.0.1`, so nothing is reachable from
  the network anyway.
- **Why HTTPS with a self-signed certificate.** The image generates its own
  certificate at boot and forcing plain HTTP breaks that setup, so the server exits.
  One certificate accept is the smaller cost.
- **Why `--network bridge` is passed explicitly.** The image's startup script waits
  for a `veth` interface before it starts the desktop. Rootless podman's default
  network namespace (pasta/slirp4netns) has none, so the container boots forever and
  the web UI never answers. It is a no-op on docker and on rootful podman.

## Security

- The web UI is bound to `127.0.0.1` — never exposed to the network.
- The browser profile lives in a named volume, not in the repo.
- Settings that are known to kill the container — a password under 6 characters, a
  resolution above 1920x1200 — are rejected up front instead of failing obscurely.
- CI runs ruff (lint + format), shellcheck, yamllint, actionlint, markdownlint, a
  gitleaks secret scan and 41 unit tests on Python 3.10 and 3.13, plus a smoke job
  that boots the real container and asserts the UI answers 200 (not 401, which would
  mean the login prompt is back), Chrome is running, and the desktop really is at
  1920x1200. Every job has a timeout.

## Licence

MIT — see [LICENSE](LICENSE).
