"""The browsers this can put in a box, and everything that differs between them.

One record each rather than branches spread through the code. What actually
differs is small but scattered: where the app comes from, what the archive holds,
which container image serves it, how it is told where to save downloads, and how
it is made the default. Keeping that in one table is what makes adding a fourth
a matter of adding a row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# Where the guest sees the host's shared folder.
GUEST_SHARE = "/Volumes/My Shared Files/downloads"


@dataclass(frozen=True)
class Browser:
    """One browser, from download to first-run settings."""

    key: str
    label: str
    # The bundle name as it lands in /Applications, and the executable inside it.
    # Tested on the executable rather than the bundle: an interrupted copy leaves a
    # directory that exists and cannot run, and a directory test calls that installed.
    app_name: str
    executable: str
    # "dmg" mounts and copies; "zip" unpacks. Chromium ships no installer at all.
    archive: str
    url: str
    # Chromium's download URL carries a build number that has to be looked up first.
    revision_url: str = ""
    # Where the .app sits inside the mounted image or the unpacked archive.
    inside: str = ""
    # The container variant serves a different Kasm image per browser.
    image: str = ""
    # "chromium" writes JSON preference files; "firefox" writes a user.js.
    settings: str = "chromium"
    # Relative to the guest's home. Chrome and Chromium differ only here.
    profile: str = ""
    # The container variant runs a Linux package inside Kasm's image, not a macOS
    # bundle, so none of the three fields above apply to it. Command names rather
    # than absolute paths, because that is what the image's own startup script uses
    # (/dockerstartup/custom_startup.sh), and it is the thing that is known to work.
    container_bin: str = ""
    # What `pgrep -x` sees, which is not always the command: Chrome is started as
    # google-chrome and appears as chrome.
    container_process: str = ""
    # Relative to the container user's home, /home/kasm-user.
    container_profile: str = ""
    # The icon's palette, clockwise from the top, and the shape it fills. Chrome
    # and Chromium share a shape and differ only in colour; Firefox does not.
    palette: tuple[tuple[float, float, float], ...] = ()
    mark: str = "wheel"

    @property
    def app(self) -> str:
        return f"/Applications/{self.app_name}"

    @property
    def binary(self) -> str:
        return f"{self.app}/Contents/MacOS/{self.executable}"


# Chrome's four, and the ones Mozilla and the Chromium project use for theirs.
GOOGLE_RED = (0.918, 0.263, 0.208)
GOOGLE_YELLOW = (0.984, 0.737, 0.020)
GOOGLE_GREEN = (0.204, 0.659, 0.325)
GOOGLE_BLUE = (0.259, 0.522, 0.957)
FIREFOX_ORANGE = (1.000, 0.604, 0.000)
FIREFOX_RED = (1.000, 0.286, 0.208)
FIREFOX_PURPLE = (0.608, 0.180, 0.788)
# The globe at the centre of Mozilla's mark, which is what stops it reading as Chrome.
FIREFOX_GLOBE = (0.121, 0.157, 0.400)
CHROMIUM_BLUE = (0.318, 0.510, 0.855)
CHROMIUM_GREY = (0.545, 0.588, 0.635)
CHROMIUM_DARK = (0.353, 0.404, 0.463)


BROWSERS = {
    "chrome": Browser(
        key="chrome",
        label="Google Chrome",
        app_name="Google Chrome.app",
        executable="Google Chrome",
        archive="dmg",
        url="https://dl.google.com/chrome/mac/universal/stable/GGRO/googlechrome.dmg",
        inside="Google Chrome.app",
        # renovate: datasource=docker depName=kasmweb/chrome
        image="docker.io/kasmweb/chrome:1.19.0",
        settings="chromium",
        profile="Library/Application Support/Google/Chrome/Default",
        container_bin="google-chrome",
        container_process="chrome",
        container_profile=".config/google-chrome",
        palette=(GOOGLE_RED, GOOGLE_GREEN, GOOGLE_YELLOW, GOOGLE_BLUE),
    ),
    "firefox": Browser(
        key="firefox",
        label="Firefox",
        app_name="Firefox.app",
        executable="firefox",
        archive="dmg",
        url="https://download.mozilla.org/?product=firefox-latest-ssl&os=osx&lang=en-US",
        inside="Firefox.app",
        # renovate: datasource=docker depName=kasmweb/firefox
        image="docker.io/kasmweb/firefox:1.19.0",
        settings="firefox",
        profile="Library/Application Support/Firefox/Profiles/cib.default-release",
        container_bin="firefox",
        container_process="firefox",
        # The root, not one profile: which profile inside it is current is Firefox's
        # own business, recorded in profiles.ini and named with a random prefix.
        container_profile=".mozilla/firefox",
        palette=(FIREFOX_ORANGE, FIREFOX_PURPLE, FIREFOX_RED, FIREFOX_GLOBE),
        mark="flame",
    ),
    "chromium": Browser(
        key="chromium",
        label="Chromium",
        app_name="Chromium.app",
        executable="Chromium",
        archive="zip",
        # The build number goes where {revision} is. Chromium publishes no stable
        # release for macOS — only per-commit snapshots — so the newest one has to
        # be looked up before anything can be downloaded.
        url=(
            "https://commondatastorage.googleapis.com/chromium-browser-snapshots"
            "/Mac_Arm/{revision}/chrome-mac.zip"
        ),
        revision_url=(
            "https://commondatastorage.googleapis.com/chromium-browser-snapshots"
            "/Mac_Arm/LAST_CHANGE"
        ),
        inside="chrome-mac/Chromium.app",
        # renovate: datasource=docker depName=kasmweb/chromium
        image="docker.io/kasmweb/chromium:1.19.0",
        settings="chromium",
        profile="Library/Application Support/Chromium/Default",
        container_bin="chromium",
        container_process="chromium",
        container_profile=".config/chromium",
        palette=(CHROMIUM_BLUE, CHROMIUM_DARK, CHROMIUM_GREY, CHROMIUM_BLUE),
    ),
}

ALL = "all"
# Not a browser: a mode. Only the VM can hold it, because a container image serves
# exactly one browser and there is no Kasm image with three.
BROWSERS[ALL] = Browser(
    key=ALL,
    label="every browser",
    app_name="",
    executable="",
    archive="",
    url="",
    mark="globe",
    palette=((0.851, 0.918, 1.0), (0, 0, 0), (0, 0, 0), (0.141, 0.361, 0.620)),
)


def expand(key: str) -> list[Browser]:
    """The browsers a choice means. Everything but `all` means just itself."""
    if key == ALL:
        return [browser for name, browser in BROWSERS.items() if name != ALL]
    return [BROWSERS[key]]


DEFAULT_BROWSER = "chrome"


def chromium_preferences(share: str) -> str:
    """Chrome and Chromium read this once, before their first launch.

    User preferences, not managed policy: a policy is the one thing a box like
    this exists to be free of, and it would put a "managed by your organization"
    banner in the menu. None of it touches passkeys — those come from iCloud
    Keychain by way of macOS, not from the browser's own password manager.
    """
    return json.dumps(
        {
            "download": {"default_directory": share, "prompt_for_download": False},
            "search": {"suggest_enabled": False},
            "alternate_error_pages": {"enabled": False},
            "safebrowsing": {"enabled": False, "enhanced": False},
            "spellcheck": {"use_spelling_service": False},
            # 2 is "do not preconnect or prefetch", which otherwise resolves and
            # opens connections to whatever a page merely hints at.
            "net": {"network_prediction_options": 2},
            "browser": {"has_seen_welcome_page": True},
            "credentials_enable_service": False,
            "profile": {"password_manager_leak_detection": False},
        }
    )


# Metrics consent is not a profile preference — it lives in Local State, beside the
# profiles rather than inside one, so writing it into Preferences does nothing.
CHROMIUM_LOCAL_STATE = json.dumps({"user_experience_metrics": {"reporting_enabled": False}})


def firefox_preferences(share: str) -> str:
    """Firefox's equivalent, which is a user.js rather than JSON.

    folderList = 2 means "use the folder named below"; without it Firefox ignores
    the path and keeps saving to its own idea of Downloads.
    """
    settings = {
        "browser.download.folderList": 2,
        "browser.download.dir": share,
        "browser.download.useDownloadDir": True,
        "browser.shell.checkDefaultBrowser": False,
        "datareporting.healthreport.uploadEnabled": False,
        "datareporting.policy.dataSubmissionEnabled": False,
        "toolkit.telemetry.enabled": False,
        "toolkit.telemetry.unified": False,
        "browser.newtabpage.activity-stream.feeds.telemetry": False,
        "browser.ping-centre.telemetry": False,
        "browser.search.suggest.enabled": False,
        "browser.aboutwelcome.enabled": False,
        "network.prefetch-next": False,
        "network.dns.disablePrefetch": True,
    }
    return "".join(
        f"user_pref({json.dumps(key)}, {json.dumps(value)});\n" for key, value in settings.items()
    )


# Firefox will not use a profile it has not been told about, so the directory alone
# is not enough — profiles.ini is what makes it the one that opens.
FIREFOX_PROFILES_INI = """\
[Profile0]
Name=default-release
IsRelative=1
Path=Profiles/cib.default-release
Default=1

[General]
StartWithLastProfile=1
Version=2
"""
