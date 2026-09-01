"""Full-page screenshot of the live Command Bridge page, in its own headless browser.

**Why this exists rather than "drive the browser you already have".** An agent that
screenshots through the human's logged-in browser competes for the tab he is reading, and
another agent doing the same takes it back. So `shot` drives its OWN headless browser on a
throwaway context, captures, and exits. It contends with nothing and works whether or not
anyone has the page open. (Ported from tunnel-vision's `shot`; the reason is the same.)

**Two things this fixes versus the donor.** The donor captured a fixed 900x560 crop, which
clips the meeting layout; here the capture is **full-page** at a real, configurable viewport
(FR2). And the donor used a bare `--screenshot` flag whose shutter fires on `load` — too early
for a page that renders from a live socket. Playwright lets the shutter wait until the page has
actually rendered, which is the settle the live WebSocket needs (an open socket never reaches
network-idle, so that is not the wait to use).

Never raises: an agent that cannot screenshot should be TOLD so, in the same `{error, code,
remedy}` shape every other command uses, and carry on rather than crash mid-session.
"""

from __future__ import annotations

from pathlib import Path


def capture(url: str, out: str | Path, viewport: tuple[int, int] = (390, 844),
            lane: str = "", settle_ms: int = 4000,
            wait_selector: str = "", color_scheme: str = "") -> dict:
    """Render `url` in a private headless browser and write a full-page PNG to `out`.

    `viewport` is a real device size (a phone by default), not a crop — the whole document is
    captured, so a layout taller than the viewport is not clipped. `lane` shoots a specific
    lane's view by passing `?lane=` (the page decides what that means). `color_scheme`
    ('dark'/'light') emulates `prefers-color-scheme`, so a page whose theme (or an embedded frame's)
    follows the system setting can be shot in the theme it will actually be seen in — the meeting
    page is dark, so its embedded canvas must be shot dark too. Returns a dict; the caller reads
    `ok` or `error`.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"error": "playwright is not installed",
                "code": "no_playwright",
                "remedy": "pip install -e .[dev] && playwright install chromium"}

    out = Path(out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    full_url = url + (("&" if "?" in url else "?") + "lane=" + lane) if lane else url
    w, h = viewport

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:                       # noqa: BLE001 — report, don't crash
                return {"error": "could not launch a headless browser",
                        "code": "no_browser",
                        "remedy": "playwright install chromium",
                        "detail": str(exc)[:200]}
            # A fresh context is the throwaway profile: no cookies, no shared lock, nothing
            # another browser instance is holding open. device_scale_factor 2 matches the repo's
            # other harnesses so a shot reads at the density he sees.
            ctx_kwargs: dict = {"viewport": {"width": w, "height": h}, "device_scale_factor": 2}
            if color_scheme:
                ctx_kwargs["color_scheme"] = color_scheme
            ctx = browser.new_context(**ctx_kwargs)
            page = ctx.new_page()
            try:
                page.goto(full_url, wait_until="load", timeout=8000)
            except Exception as exc:                       # noqa: BLE001
                browser.close()
                return {"error": "could not load the page — is the server up?",
                        "code": "page_unreachable",
                        "remedy": "start it with `command-bridge serve`, or pass --url",
                        "url": full_url, "detail": str(exc)[:200]}
            # Settle: let the socket connect and the page render. A specific selector is exact;
            # absent one, a bounded pause. Either way the shutter waits, which is the whole point
            # of driving with an automation library rather than a one-shot `--screenshot`.
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=settle_ms)
                except Exception:                          # noqa: BLE001 — shoot what rendered
                    pass
            else:
                page.wait_for_timeout(min(settle_ms, 6000))
            try:
                page.screenshot(path=str(out), full_page=True)
                doc = page.evaluate(
                    "() => [document.documentElement.scrollWidth,"
                    " document.documentElement.scrollHeight]")
            finally:
                browser.close()
    except Exception as exc:                               # noqa: BLE001
        return {"error": "the screenshot failed", "code": "shot_failed",
                "detail": str(exc)[:200]}

    if not out.exists() or out.stat().st_size == 0:
        return {"error": "no image was written", "code": "no_image", "path": str(out)}
    return {"ok": True, "path": str(out), "bytes": out.stat().st_size,
            "viewport": [w, h], "page": doc}


def parse_viewport(spec: str, default: tuple[int, int] = (390, 844)) -> tuple[int, int]:
    """`"390x844"` -> `(390, 844)`. Falls back to a phone-realistic default on anything unparseable,
    because a bad size should not stop a shot — the wrong size is still a picture."""
    try:
        w, h = spec.lower().split("x", 1)
        return int(w), int(h)
    except (ValueError, AttributeError):
        return default
