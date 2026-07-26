# chrome-in-a-box

Real Google Chrome, running in an isolated container, used from a tab in your own
browser. One command up, one command down, and the profile survives restarts.

```bash
./run.sh up      # start it
./run.sh open    # open https://localhost:6901/?resize=scale
./run.sh down    # stop it (profile is kept)
```

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
| `./run.sh logs`    | follow container logs                                    |
| `./run.sh shell`   | shell into the container                                 |
| `./run.sh reset`   | delete the browser profile (asks first)                  |

Overridable: `CIB_PORT`, `CIB_RESOLUTION`, `CIB_IMAGE`, `CIB_NAME`, `CIB_VOLUME`,
`CIB_PASSWORD`.

## Passkeys — what does and does not work

**Passkeys cannot be forwarded from the host into the container.** A Touch ID
passkey is bound to the Mac's Secure Enclave, and WebAuthn deliberately offers no
way to relay that to another machine — the container is a different device with no
biometric sensor and no Bluetooth, so cross-device (QR code) sign-in is out too.
Anything claiming otherwise would be defeating the security property passkeys exist
for.

What **does** work, and is usually what people actually want:

- **Google Password Manager passkeys sync.** Passkeys saved to Google Password
  Manager (not iCloud Keychain) are available in any Chrome signed into that Google
  account — including this one. Unlock with your Google Password Manager PIN.
- **Passwords sync** the same way once you sign in.

So if a site says *"no passkeys available on this device"*, that passkey lives in
iCloud Keychain. Re-register it on that site from within this Chrome and choose
Google Password Manager, and it will then work in both places.

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

## Security

- The web UI is bound to `127.0.0.1` — never exposed to the network.
- The browser profile lives in a named volume, not in the repo.
- CI runs shellcheck, yamllint, actionlint, markdownlint, a gitleaks secret scan,
  unit tests, and a smoke test that boots the real container and verifies Chrome
  starts and no login prompt reappeared.

## Licence

MIT — see [LICENSE](LICENSE).
