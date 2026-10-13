"""Spec 033 — the app is the bridge, not a browser in a box.

JJ on app 0.3.0, 2026-10-09: his wave never moved, two titles, no output caret on the mic, no
transcript caret, a pair button and a cog that don't belong in the app, a light page on a dark phone,
and no way to see which build he is on. These pin the page's and the app's halves of each fix; what
only the phone can show (route, look, screen awake) is his manual check.
"""
import pathlib
import re

from command_bridge import server

WEB = pathlib.Path(server.WEB_DIR)
SHELL = (WEB / "shell.html").read_text(encoding="utf-8")
PAGE = (WEB / "index.html").read_text(encoding="utf-8")
ANDROID = pathlib.Path(__file__).resolve().parents[1] / "android" / "app" / "src" / "main"
MANIFEST = (ANDROID / "AndroidManifest.xml").read_text(encoding="utf-8")
ACTIVITY = (ANDROID / "java" / "dev" / "commandbridge" / "companion" / "MainActivity.kt").read_text(encoding="utf-8")


def _native_block():
    start = SHELL.index("window.cbNative = {")
    return SHELL[start:SHELL.index("window.cbShell = {", start)]


def test_the_app_feeds_his_level_and_speech_to_the_meter():          # AC3, FR1
    block = _native_block()
    assert re.search(r"level\(l, speaking\) \{[^}]*levelTarget = ", block)
    assert "speakingSignal = Boolean(speaking);" in block
    # Off or channel closed: the meter drops, the wave goes flat.
    assert "if (!running || !channelOpen) { levelTarget = 0; speakingSignal = false; }" in block


def test_the_app_hands_the_page_its_outputs_and_takes_the_pick():   # AC3, FR3
    assert "routes(r) {" in _native_block()
    assert 'if (nativeRoutes) ui("nativeRoutes", nativeRoutes);' in SHELL   # replayed on a UI reload
    assert 'nativeCall("setRoute", { type: Number(type) });' in SHELL
    assert "if (!NATIVE) return;" in SHELL[SHELL.index("setNativeRoute(type)"):]


def test_only_the_top_frame_can_drive_the_app():                      # review SHOULD 2
    """The canvas frames render agent-authored HTML on the same origin. A JavaScript interface is
    injected into every frame; a web message listener reports which frame posted, and the app drops
    anything not from the main frame, and any route the phone doesn't have."""
    assert "addJavascriptInterface" not in ACTIVITY
    assert "WebViewCompat.addWebMessageListener" in ACTIVITY
    assert "if (!isMainFrame) return" in ACTIVITY
    assert "window.CBNative.postMessage(" in SHELL
    assert "if (!availableTypes().contains(type)) return" in ACTIVITY


def test_the_route_caret_is_welded_to_mute_and_app_only():          # AC4, FR3
    mute = PAGE.index('<button id="mute"')
    pick = PAGE.index('<label class="pick input" id="routepick" hidden>')
    assert mute < pick < PAGE.index('id="devpick"')
    assert "if (!NATIVE || !$routepick || !$approute) return;" in PAGE
    assert "$routepick.hidden = list.length < 2;" in PAGE


def test_no_pair_button_in_the_app():                                 # AC4, FR5 (spec 035: anywhere)
    assert 'id="pairbtn"' not in PAGE
    # The attribute alone lost to `.orbbtn { display:grid }`: a hidden button stayed on screen.
    assert ".orbbtn[hidden] { display:none; }" in PAGE


def test_the_transcript_caret_shows_on_a_narrow_phone_in_the_app():  # AC4, FR4
    assert "@media (max-width:479.98px) { html:not(.native) #logpick { display:none; } }" in PAGE
    assert "@media (max-width:479.98px) { #logpick { display:none; } }" not in PAGE
    assert 'document.documentElement.classList.toggle("native", NATIVE);' in PAGE


def test_the_version_badge_renders_only_a_dotted_version():          # AC4, FR8
    assert '<span id="appver" hidden></span>' in PAGE
    rule = re.search(r"!/(\^.*?\$)/\.test\(v\)", PAGE).group(1)
    ok = re.compile(rule.replace("\\\\", "\\"))
    assert ok.match("0.4.0") and ok.match("1.2")
    assert not ok.match("0.4.0<img src=x>") and not ok.match("") and not ok.match("v1")
    # never innerHTML; the path word (spec 034 FR7) only from a fixed pair
    assert 'el.textContent = "v" + v + (via === "home" || via === "tunnel" ? " · " + via : "");' in PAGE


def test_the_app_has_one_title_and_follows_the_system_theme():       # AC5, FR2
    assert 'android:theme="@style/Bridge"' in MANIFEST
    styles = (ANDROID / "res" / "values" / "styles.xml").read_text(encoding="utf-8")
    assert 'parent="@android:style/Theme.DeviceDefault.DayNight"' in styles
    assert '<item name="android:windowActionBar">false</item>' in styles
    assert '<item name="android:windowNoTitle">true</item>' in styles


def test_no_cog_and_no_native_settings_ui():                          # AC5, FR6
    assert "⚙" not in ACTIVITY
    assert "gear" not in ACTIVITY


def test_the_screen_stays_on_only_while_the_conversation_is_on():    # FR7
    assert "FLAG_KEEP_SCREEN_ON" in ACTIVITY
    # Spec 044: driven, the server's `awake` directive decides (it follows the channel); an app on an
    # older server keeps the channel rule.
    assert "if (VoiceService.driven) VoiceService.awakeWanted else VoiceService.channelOpenNow" in ACTIVITY
    assert "VoiceService.running && wanted" in ACTIVITY


def test_the_app_tells_the_page_its_version():                         # FR8
    assert "?native=1&app=" in ACTIVITY
