"""A pushed `reload` reloads the right document — and never drops the audio to reload the canvas.

spec 013. The parent page (`web/index.html`) holds the AudioContext and the voice socket; the canvas
is an iframe inside it. So a canvas (`page.py`) edit must reload ONLY the iframe, and a full parent
reload — the one that blips the audio — must fire only when `index.html` itself actually changed.

The load-bearing rule is the BIAS: `page` (full reload, audio drops) is returned only on positive
evidence the parent moved. Any uncertainty — a first-ever hash, an unreadable file — resolves to
`canvas`, because guessing `page` cuts JJ off mid-sentence for nothing.
"""
import os

from command_bridge.canvas import aio
from command_bridge.canvas import page as page_mod


def _index_html() -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(aio.__file__)), "web", "index.html")
    with open(path, encoding="utf-8") as f:
        return f.read()


# --- the pure decision -------------------------------------------------------

def test_parent_change_is_a_full_reload():
    """Different signatures => index.html moved => the parent must full-reload (FR5)."""
    assert aio._reload_target("aaa", "bbb") == "page"


def test_canvas_only_change_reloads_the_iframe():
    """Same signature => only page.py changed => the iframe reloads itself, audio untouched (FR1)."""
    assert aio._reload_target("aaa", "aaa") == "canvas"


def test_first_ever_reload_never_drops_the_audio():
    """No baseline yet (prev None) is NOT evidence the parent changed. Bias to canvas."""
    assert aio._reload_target(None, "bbb") == "canvas"


def test_an_unreadable_parent_never_drops_the_audio():
    """cur None (index.html could not be read) is uncertainty, not a parent change. Bias to canvas."""
    assert aio._reload_target("aaa", None) == "canvas"
    assert aio._reload_target(None, None) == "canvas"


def test_index_signature_reads_the_real_parent_document():
    """The baseline is a real hash of the shipped index.html, so a later edit is detectable."""
    sig = aio._index_signature()
    assert isinstance(sig, str) and len(sig) == 64  # sha256 hex


# --- the client honours the target (wiring guards) ---------------------------

def test_the_canvas_iframe_reloads_itself_on_a_canvas_target():
    """page.py must carry a `reload` listener that reloads the iframe on a `canvas` target — this is
    what makes a page.py edit audio-safe. Removing it silently reverts to the parent-reload bug."""
    assert 'addEventListener("reload"' in page_mod.PAGE
    assert 'target === "canvas"' in page_mod.PAGE


def test_the_parent_reloads_only_on_a_page_target():
    """index.html must GATE its full reload on a `page` target. Without the gate, a canvas reload
    full-reloads the parent and drops the audio — the exact regression spec 013 closes."""
    html = _index_html()
    assert 'target !== "page"' in html
    # And the full reload is still there for a genuine parent change (FR5).
    assert "location.reload()" in html
