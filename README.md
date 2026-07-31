# chrome-in-a-box

Real Google Chrome in a box, so the browser you use for your own things is not the
one your machine's policy manages. One command up, one command down, and the
profile survives restarts.

```bash
cib box up  # start it
cib box open # open https://localhost:6901/?resize=remote
cib box down # stop it (profile is kept)
```

One file, and one optional dependency. Run it straight from a checkout with
`./cib.py box up`,
install it as a `cib` command, or download a self-contained binary — see
[Install](#install).

No login prompt. Accept the self-signed certificate the browser warns about and
you are in.

## Why

A browser can end up locked down by policy — password manager off, autofill off,
settings greyed out behind an "administrator" badge. That is fine for work, but it
also applies to everything else you do in that browser.

This runs a **separate** Chrome in a guest — a Linux container, or a macOS VM.
Host browser policy does not reach into a guest, so that Chrome is unmanaged: the
built-in password manager, autofill and account sync all work normally. Nothing on
the host is modified, disabled or worked around — it is simply a second browser
that happens to live in a box.

## Two variants

There is no single box that does everything, because the isolation that keeps host
policy out also keeps the host keychain out. So there are two, and they fail in
opposite directions.

|                                       | `cib box up` — **container**   | `cib vm …` — **macOS VM**               |
| ------------------------------------- | ------------------------------ | --------------------------------------- |
| Runs on                               | anything with podman or docker | Apple silicon only                      |
| Free of host browser policy           | yes                            | yes                                     |
| Google account sync, Password Manager | yes                            | yes                                     |
| **iCloud Keychain + its passkeys**    | **no**                         | **yes**                                 |
| Touch ID                              | no                             | no — falls back to the account password |
| Hardware security key (USB)           | no                             | no                                      |
| Weight                                | ~3 GB image, seconds to start  | ~40 GB, minutes to start                |
| Used from                             | a tab in your own browser      | its own window                          |

```mermaid
flowchart TD
    A{What do you need?} --> B[Google account sync,<br/>Password Manager]
    A --> C[iCloud Keychain<br/>passkeys]
    A --> D[USB security key<br/>or Touch ID]
    B --> E([container<br/>cib box up])
    C --> F{Apple silicon?}
    F -- yes --> G([macOS VM<br/>cib vm create])
    F -- no --> H([not possible])
    D --> I([use the host browser —<br/>no box can do this])
```

### The container

```mermaid
flowchart LR
    subgraph host [your machine]
        B[your browser<br/>localhost:6901]
    end
    subgraph ctr [Linux container]
        K[KasmVNC] --> C[Google Chrome]
    end
    B -- "HTTPS + websocket" --> K
    C -.-> G[(Google account:<br/>sync, passwords, passkeys)]
```

[kasmweb/chrome](https://hub.docker.com/r/kasmweb/chrome) serves a
[KasmVNC](https://github.com/kasmtech/KasmVNC) web client. Keyboard and mouse travel
over a **websocket** — which matters, see [Design notes](#design-notes). The macOS
keychain is not reachable: a Linux guest has no Secure Enclave.

### The macOS VM

```mermaid
flowchart LR
    subgraph host [your Mac]
        V[Virtualization.framework]
        S[[Secure Enclave]]
    end
    subgraph vm [macOS guest, not MDM-enrolled]
        C[Google Chrome] --> KC[iCloud Keychain]
    end
    V --> vm
    S -- "identity only,<br/>no Touch ID" --> vm
    KC -.-> A[(Apple account:<br/>passkeys sync)]
```

Since macOS 15, Apple supports signing a guest VM into an Apple Account, so iCloud
Keychain — and therefore passkeys — sync into it. The guest was never enrolled in
any MDM, so its Chrome reads no managed policy. It has no Secure Enclave of its own
and no Touch ID, so passkey use asks for the account password instead of a finger.

The VM is started with **bridged** networking, so it gets an address from the real
network and inherits a DNS resolver that works. tart's default shared mode hands
out the vmnet gateway as the resolver, and on some hosts that gateway does not
answer DNS at all — the guest then has an address but resolves nothing, which
Setup Assistant reports as "not connected to the Internet". Override with
`CIB_VM_NET=shared` or point at another interface with `CIB_VM_INTERFACE`.

> The VM must be **created from** a macOS 15+ installer. Upgrading or cloning an
> older VM does not get an Apple Account identity — `cib vm create` does it
> the right way.

## Requirements

- Python 3.10 or newer. Note that macOS ships 3.9, so install one (`brew install
  python@3.14`) or use the Homebrew/binary install below, which needs no Python to run
- For the container: **podman or docker**
- For the macOS VM: **Apple silicon**, [tart](https://tart.run)
  (`brew install cirruslabs/cli/tart`), ~40 GB free and 8 GB RAM to spare. The
  build patches the guest's disk, which needs `sudo` once — nothing else does.
  [Packer](https://packer.io) (`brew install hashicorp/tap/packer`) is **only**
  needed for the `CIB_VM_PACKER=1` fallback described below
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

## Install

Four ways, pick one:

```bash
# 1. from a checkout — nothing to install
git clone https://github.com/sapn95/chrome-in-a-box && cd chrome-in-a-box
./cib.py box up

# 2. with Homebrew (the tap lives in this repo, no second repo to add)
brew tap sapn95/tap https://github.com/sapn95/chrome-in-a-box
brew install sapn95/tap/cib     # the full name is what grants trust; plain
cib box up                      # `brew install cib` asks you to `brew trust` first

# 3. as a command, in its own environment
uv tool install git+https://github.com/sapn95/chrome-in-a-box    # or: pipx install ...
cib box up

# 4. a compiled build — no Python needed to run it
#    (download the asset for your platform from the Releases page)
tar -xzf cib-macos-arm64.tar.gz && ./cib-macos-arm64/cib box up
```

All four can build the macOS VM: the compiled builds carry `cibpatch.py` and the
packer template beside the binary, because `cib` spawns them rather than importing
them and Nuitka would otherwise leave them behind.

Compiled with [Nuitka](https://nuitka.net) — Python translated to C — so it needs no
interpreter and no dependencies to run.

One exception, and only for the VM: the patch step is a separate script that `cib`
spawns rather than imports, so it needs a working `python3` on the machine. Every
Mac has `/usr/bin/python3`, but without the Command Line Tools that is a stub that
exits the moment it runs — `cib` checks and says so, rather than failing halfway
through a build. `xcode-select --install` is enough. The container variant needs
nothing. It is not a single file: the archive holds
`cib` next to the shared objects it links against, so keep the folder together.
Homebrew and the Linux builds handle that for you; the Linux ones are compiled
inside UBI10 so they link against a supported base.

## Commands

| Command          | What it does                                             |
| ---------------- | -------------------------------------------------------- |
| `cib box up`     | start the container, wait for the desktop, launch Chrome |
| `cib box down`   | stop and remove the container (profile is kept)          |
| `cib box open`   | open the web UI in your browser                          |
| `cib box status` | show container state                                     |
| `cib box logs`   | show the last 200 log lines (`-f` follows instead)       |
| `cib box shell`  | shell into the container                                 |
| `cib box engine` | print the container engine that will be used             |
| `cib box reset`  | delete the browser profile (asks first)                  |

The macOS VM variant (Apple silicon):

| Command           | What it does                                     |
| ----------------- | ------------------------------------------------ |
| `cib vm create`   | build it, start it, install Chrome — one command |
| `cib vm prepare`  | redo just the offline preparation of a built VM  |
| `cib vm up`       | start it again — a window opens, shell blocks    |
| `cib vm setup`    | redo just the Chrome install, over SSH           |
| `cib vm open`     | start it if stopped, then show its screen        |
| `cib vm icon`     | write a clickable app into `~/Applications`      |
| `cib vm password` | print the generated guest password               |
| `cib vm login`    | print the guest account name and password        |
| `cib vm ssh`      | open a shell in the guest                        |
| `cib vm ip`       | print the guest's address                        |
| `cib vm down`     | stop it                                          |
| `cib vm status`   | list VMs and their state                         |
| `cib vm delete`   | delete the VM and everything in it (asks first)  |

`vm create` is **unattended**, and it never shows Setup Assistant at all. Setup
Assistant cannot be skipped without MDM, and driving it with synthetic keystrokes is
brittle — one changed pane and the sequence types into the wrong field. So `cib`
does the other thing: it boots the fresh guest once, then writes the state Setup
Assistant would have produced straight onto its disk, before the guest ever reaches
a login window. That is deterministic — no timing, no OCR, nothing to re-learn when
Apple moves a button.

What gets written: an account with a **generated** password, autologin, Remote
Login, cib's SSH key and the guest's host key, and **this host's keyboard layout**
(so the guest types where your fingers already do). Only that one step needs
`sudo`.

`cib` never prompts for a password itself, so **cache the credential first**:

```bash
sudo -v && cib vm create
```

Both in the **same terminal**, and that matters: sudo remembers a credential per
tty, so one cached in another window does not count — and a process started
without a tty (a launchd job, a wrapper, an agent) can never obtain one at all.

It is checked before the download starts, not after — and held open across the
build, because sudo forgets a credential in about five minutes and the build takes
thirty to sixty. If the patch step still fails, `cib vm prepare` redoes just it,
without rebuilding the VM.

Chrome **is** part of `vm create`: it builds the VM, starts it, waits for SSH and
installs Chrome and the clipboard agent, all from the one command. `cib vm setup`
exists to redo only that last step against a guest that is already up.

Every write onto the guest's disk is one Apple could change, so `CIB_VM_PACKER=1`
keeps the old path available: it drives Setup Assistant with Packer instead. It
needs Packer installed, and a VM that does not exist yet (`cib vm delete` first).

One thing stays manual, because Apple makes it interactive on purpose: the
**Apple Account sign-in**, in the guest's own window.

It cannot be scripted, and not for want of an unexplored trick. Joining the
iCloud Keychain sync circle is a cryptographic handshake, not a setting: a new
device is either *sponsored* by a device already in the circle, or it recovers
via an SMS code plus the iCloud security code. Both need a second party in the
moment. The `KEYCHAIN_SYNC` entry in `defaults read MobileMeAccounts` is a
read-only mirror of that state — accountsd owns the truth in a TCC-protected
database — and `otctl`, which does drive Octagon, is walled off behind
`com.apple.private.*` entitlements that only Apple can sign. A configuration
profile cannot help either: `allowCloudKeychainSync` only *permits*, and already
defaults to true.

The good news is that the sign-in is enough on its own. Signing in switches
Keychain sync on and joins the circle, so there is no second toggle to find:

```console
$ otctl status | grep State:
State: Ready
```

`cib vm up` runs the guest in the foreground: the window opens and the shell does
not come back until the VM shuts down, and Ctrl-C there kills the guest. Run the
steps after it from a **second terminal**.

Neither `cib vm setup` nor `cib vm ssh` asks you to type anything. `vm create`
generates an SSH key pair, installs the public half in the guest, and plants the
guest's own **host key** as well — so the very first connection is verified rather
than trusted. Everything lives beside the password in
`~/.config/chrome-in-a-box/<CIB_VM_NAME>/` — a directory per VM, so a second one
cannot reuse the first's key — and `cib vm delete` removes all of it. An older
cib kept them one level up; the first VM command you run moves them.

The guest has no Touch ID, so passkey confirmations do ask for the account
password. You never type that either — `cib vm password` prints it, and you paste
it.
Clipboard sharing between host and guest is not a flag: it needs
[tart-guest-agent](https://github.com/cirruslabs/tart-guest-agent) running inside
the guest, which `vm setup` installs.

`up` is idempotent: if the container is already serving it just re-applies the
resolution and revives Chrome, so your tabs survive. `CIB_FORCE=1` recreates it
instead, which is what you need after changing the image or an environment setting.

Overridable: `CIB_PORT`, `CIB_RESOLUTION`, `CIB_WAIT_SECS`, `CIB_ENGINE`,
`CIB_IMAGE`, `CIB_NAME`, `CIB_VOLUME`, `CIB_PASSWORD`, `CIB_LOG_TAIL`, `CIB_FORCE`,
and for the VM `CIB_VM_NAME`, `CIB_VM_CPUS`, `CIB_VM_MEMORY`, `CIB_VM_DISK`,
`CIB_VM_DISPLAY`, `CIB_VM_NET`, `CIB_VM_INTERFACE`, `CIB_VM_USER`, `CIB_VM_SHARE`,
`CIB_VM_PASSWORD` (a password of your own instead of a generated one; letters and
digits only, and no y or z, because the packer path types it as keystrokes),
`CIB_VM_CAPTURE_KEYS` (send Cmd+Space, Cmd+Tab and the rest to the guest while
its window has focus — off by default, because it is all-or-nothing and a host
launcher on Cmd+Space becomes unreachable until you click away),
`CIB_VM_FIRSTBOOT_SECS` (how long the guest is given to lay down its first-boot
state before the disk is patched — 180 s, raise it on a slow disk) and
`CIB_VM_IPSW` (the macOS installer: `latest` by default, or a URL or `.ipsw` path
to pin the guest to one version).

### The settings file

Everything above can live in `~/.config/chrome-in-a-box/cib.yaml` instead of your
shell profile. `CIB_CONFIG` points somewhere else.

```yaml
box:
  port: 6901
  resolution: 1280x800

vm:
  name: chrome-vm
  user: admin
  password: admin        # or leave it out for a generated one
  display: 1280x800
  net: bridged
  share: ~/Downloads/chrome-vm
  capture_keys: false
```

Keys are the environment variable names without their prefix, lowercased: `[box]`
holds the `CIB_*` ones and `[vm]` the `CIB_VM_*` ones. An environment variable
still wins, so exporting one for a single command overrides the file without
editing it.

This is the one thing outside the standard library — reading it needs PyYAML,
which the Homebrew, `uv tool` and compiled installs all bring with them. Running
`./cib.py` from a bare checkout without it still works; it only refuses if a
settings file is actually there, because ignoring one silently is the same as the
setting not working.

`CIB_VM_VIEWER` picks how you look at the guest. `window` (the default) is tart's
own window. `vnc` opens no window at all: tart serves the screen over VNC and
prints an address for it, which `cib vm viewer` reprints. That address carries a
password tart generates per run, so it is worth nothing once the guest restarts —
and a guest started without the address being captured cannot be reached at all.

**Chrome's** downloads in the guest land in `~/Downloads/chrome-vm` on the host.
The folder is shared into the VM and Chrome is pointed at it through its own
settings. Point it elsewhere with `CIB_VM_SHARE`.

Only Chrome. Replacing the guest's `~/Downloads` with a symlink would make every
app follow, but macOS refuses: TCC protects Downloads, Desktop and Documents, and
renaming one over SSH fails with `Operation not permitted` however much root you
have. Other apps in the guest save to the guest's own `~/Downloads`, where a link
named `on-the-host` points at the share for dragging things across by hand.

## Passkeys — what does and does not work

**A passkey cannot be forwarded from the host into a guest.** A Touch ID passkey is
bound to the Mac's Secure Enclave, and WebAuthn deliberately offers no way to relay
that — which is the property it exists for. Nor is the QR / "use your phone" flow a
way around it: the phone's Bluetooth advertisement is an *input to the key
derivation*, not a UI step, so a guest with no radio cannot complete the handshake.
Chrome checks for a Bluetooth adapter first and does not even offer the QR code.

So a passkey has to *live* in the box. There are two ways to arrange that:

- **Container → Google Password Manager.** Register a new passkey inside the
  container; it syncs with your Google account.
- **macOS VM → iCloud Keychain.** The passkeys you already have sync in, because the
  VM is signed into your Apple Account.

### In the container: Google Password Manager

Chrome on Linux is a first-class GPM passkey platform — it needs no TPM (on Linux
Chrome deliberately stores the identity key on disk instead), no USB and no
Bluetooth. It syncs over the network, which is the only transport a container has.

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
- **Why the desktop size is dynamic.** The client asks for `?resize=remote`, so
  KasmVNC resizes the desktop to your browser window and maximising actually gives
  you more desktop. Pinning a mode with `CIB_RESOLUTION` is possible but then the
  two fight: KasmVNC only ships modes up to 1920x1200, and larger values silently
  fall back to 1024x768.
- **Why there is a password despite no login prompt.** KasmVNC refuses to start with
  a password under 6 characters, so one is set and the prompt is disabled with
  `DisableBasicAuth`. The port is bound to `127.0.0.1`, so nothing is reachable from
  the network anyway.
- **Why HTTPS with a self-signed certificate.** The image generates its own
  certificate at boot and forcing plain HTTP breaks that setup, so the server exits.
  It generates a *new* one on every boot, so the browser asks again after a
  `CIB_FORCE=1` recreate; accepting it is still the smaller cost.
- **Why `--network bridge` is passed explicitly.** The image's startup script waits
  for a `veth` interface before it starts the desktop. Rootless podman's default
  network namespace (pasta/slirp4netns) has none, so the container boots forever and
  the web UI never answers. It is a no-op on docker and on rootful podman.

## Security

- The web UI is bound to `127.0.0.1` — never exposed to the network.
- The browser profile lives in a named volume, not in the repo.
- Settings that are known to kill the container — a password under 6 characters, a
  resolution that is not one of the modes KasmVNC ships — are rejected up front
  instead of failing obscurely. "In range" is not the same as available: 1600x900
  is smaller than the largest mode and still not one of them.
- CI runs ruff (lint + format), yamllint, actionlint, markdownlint, a gitleaks
  secret scan, and the unit tests on Python 3.10 and 3.14 with a coverage floor,
  everything Python inside a UBI10 container, plus a smoke job
  that boots the real container and asserts the UI answers 200 (not 401, which would
  mean the login prompt is back), Chrome is running, and the desktop really is at
  1920x1200. Every job has a timeout.

## Licence

MIT — see [LICENSE](LICENSE).
