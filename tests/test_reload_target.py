"""A pushed `reload` reloads the right document — and never drops the audio to reload the UI.

spec 013 + 023. The audio shell (`web/shell.html`) holds the AudioContext, the microphone and the
voice socket; the UI frame (`web/index.html`) sits inside it and the canvas iframe inside that. So a
UI edit reloads ONLY the UI frame, live; a canvas (`page.py`) edit reloads only the canvas; and the
one reload that drops the audio — the shell's own — fires only when `shell.html` itself changed,
and waits for the orb to be off.

Each page is SERVED stamped with its own signature and the `reload` event carries the signatures on
disk, so the open page decides. The server's `target` is only what the CLI reports, and its bias
holds: `page` is returned only on positive evidence the shell moved.
"""
import os

from command_bridge.canvas import aio
from command_bridge.canvas import page as page_mod

SHELL, UI = aio.SHELL_PAGE, aio.UI_PAGE


def _web(name: str) -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(aio.__file__)), "web", name)
    with open(path, encoding="utf-8") as f:
        return f.read()


# --- what the CLI is told the reload did -------------------------------------

def test_a_shell_change_is_a_full_reload():
    """The shell moved => the one reload that drops the audio, deferred while live (FR6)."""
    assert aio._reload_target({SHELL: "a", UI: "u"}, {SHELL: "b", UI: "u"}) == "page"


def test_a_ui_change_reloads_only_the_ui_frame():
    """The UI moved and the shell did not => the UI frame reloads live, audio untouched (FR5)."""
    assert aio._reload_target({SHELL: "a", UI: "u"}, {SHELL: "a", UI: "v"}) == "ui"


def test_neither_page_changed_is_a_canvas_reload():
    """Only page.py changed => the canvas iframe reloads itself (spec 013 FR1)."""
    assert aio._reload_target({SHELL: "a", UI: "u"}, {SHELL: "a", UI: "u"}) == "canvas"


def test_uncertainty_never_reports_a_full_reload():
    """No baseline, or an unreadable file, is NOT evidence the shell changed."""
    assert aio._reload_target({}, {SHELL: "b", UI: "v"}) == "canvas"
    assert aio._reload_target({SHELL: "a", UI: "u"}, {SHELL: None, UI: "u"}) == "canvas"


def test_the_signatures_read_the_real_page_documents():
    for name in (SHELL, UI):
        sig = aio.page_signature(name)
        assert isinstance(sig, str) and len(sig) == 16


def test_a_served_page_is_stamped_with_its_own_signature():
    """The stamp is what lets an open page tell, after a restart, whether ITS file changed."""
    for name in (SHELL, UI):
        text, sig = aio.stamp_page(name)
        assert aio.SIG_PLACEHOLDER not in text
        assert f'"{sig}"' in text
        assert sig == aio.page_signature(name)


# --- the client honours it (wiring guards) -----------------------------------

def test_the_canvas_iframe_reloads_itself_unless_the_ui_is_reloading_around_it():
    """page.py reloads its own iframe on a reload; a `ui` target is skipped because the UI frame that
    holds the canvas is recreating it anyway."""
    assert 'addEventListener("reload"' in page_mod.PAGE
    assert 'target !== "ui"' in page_mod.PAGE


def test_the_ui_frame_reloads_on_its_own_signature_and_is_never_deferred():
    html = _web(UI)
    assert html.count(aio.SIG_PLACEHOLDER) == 1
    assert "d.ui_sig === UI_SIG" in html
    assert "liveNow" not in html, "the UI frame holds no audio, so it must not wait for the orb"


def test_the_shell_reloads_only_for_its_own_change_and_waits_for_the_orb():
    html = _web(SHELL)
    assert html.count(aio.SIG_PLACEHOLDER) == 1
    assert "d.shell_sig === SHELL_SIG" in html
    assert "reloadPending = true" in html and "liveNow()" in html
