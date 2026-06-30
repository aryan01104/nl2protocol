"""Drive headless Chrome over CDP to screenshot hover states of the report.

Scratch only (untracked). Loads the populated static demo, dispatches a real
mouse hover over chosen elements (triggers CSS :hover + JS mouseenter), and
screenshots each — so hover behavior can be verified visually instead of by
grepping HTML. Uses system Chrome + websocket-client (already in venv); no installs.

Usage: venv/bin/python scratch_cdp_hover.py [tag]
Outputs: output/hover_<tag>_<label>.png
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time

import httpx
import websocket

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
URL = "file://" + os.path.abspath("output/report4_live_demo.html")
PORT = 9222
VIEW_W, VIEW_H = 1700, 1400

# (label, selector) — selector resolved client-side; element scrolled into view.
TARGETS = [
    ("00_resting", None),
    ("01_step_block", ".step[data-prov-id]"),
    ("02_param_destination", ".col-protocol-steps [data-prov-source][data-prov-id$='-destination-wells']"),
    ("03_code_tok_destination", "pre.code .code-tok[data-prov-id$='-destination-wells']"),
    ("04_param_volume", ".col-protocol-steps [data-prov-source][data-prov-id$='-volume']"),
    ("05_cite_destination", ".instruction-text [data-cite-id~='s0-destination-wells']"),
]


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, max_size=None)
        self._id = 0

    def cmd(self, method, **params):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def wait_event(self, method, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            self.ws.settimeout(max(0.1, end - time.time()))
            try:
                msg = json.loads(self.ws.recv())
            except Exception:
                return
            if msg.get("method") == method:
                return msg


def launch_chrome(user_dir):
    proc = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
         "--remote-allow-origins=*",
         f"--user-data-dir={user_dir}", "--no-first-run", "--no-default-browser-check",
         "--disable-gpu", "--hide-scrollbars", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        try:
            r = httpx.get(f"http://127.0.0.1:{PORT}/json", timeout=0.5).json()
            page = next((t for t in r if t["type"] == "page"), None)
            if page:
                return proc, page["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError("Chrome CDP did not come up")


def rect_of(cdp, selector):
    expr = ("(() => { const el = document.querySelector(%s); if (!el) return null;"
            " el.scrollIntoView({block:'center'}); const r = el.getBoundingClientRect();"
            " return JSON.stringify({x:r.x, y:r.y, w:r.width, h:r.height}); })()"
            % json.dumps(selector))
    res = cdp.cmd("Runtime.evaluate", expression=expr, returnByValue=True)
    val = res.get("result", {}).get("value")
    return json.loads(val) if val else None


def rect_of_raw(cdp, selector):
    expr = ("(() => { const el = document.querySelector(%s); if (!el) return null;"
            " const r = el.getBoundingClientRect();"
            " return JSON.stringify({x:r.x, y:r.y, w:r.width, h:r.height}); })()"
            % json.dumps(selector))
    res = cdp.cmd("Runtime.evaluate", expression=expr, returnByValue=True)
    val = res.get("result", {}).get("value")
    return json.loads(val) if val else None


def move_mouse(cdp, x, y):
    cdp.cmd("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, buttons=0)


def main():
    tag = sys.argv[1] if len(sys.argv) > 1 else "now"
    user_dir = tempfile.mkdtemp(prefix="cdp-")
    proc, ws_url = launch_chrome(user_dir)
    try:
        cdp = CDP(ws_url)
        cdp.cmd("Page.enable")
        cdp.cmd("Runtime.enable")
        cdp.cmd("Emulation.setDeviceMetricsOverride", width=VIEW_W, height=VIEW_H,
                deviceScaleFactor=2, mobile=False)
        for label, selector in TARGETS:
            # Fresh page per target → no hover-state leakage between captures.
            cdp.cmd("Page.navigate", url=URL)
            cdp.wait_event("Page.loadEventFired", timeout=15)
            time.sleep(1.0)  # let fonts + wireHover() settle
            move_mouse(cdp, 4, 4)
            time.sleep(0.15)
            if selector:
                info = cdp.cmd("Runtime.evaluate", returnByValue=True, expression=(
                    "(() => { const el = document.querySelector(%s); return el ? "
                    "JSON.stringify({pid: el.getAttribute('data-prov-id'), "
                    "txt: (el.textContent||'').trim().slice(0,30)}) : 'NULL'; })()"
                    % json.dumps(selector)))
                print(f"  {label}: hovering {info.get('result', {}).get('value')}")
                r = rect_of_raw(cdp, selector)  # no scroll — keep columns stable
                if not r:
                    print(f"  ! selector not found: {selector}")
                    continue
                move_mouse(cdp, r["x"] + r["w"] / 2, r["y"] + r["h"] / 2)
                time.sleep(0.4)
            else:
                time.sleep(0.2)
            # Clip to the instruction column (where the cite wash shows) so the
            # per-hue vs uniform detail is readable instead of a tiny full page.
            clip = rect_of_raw(cdp, os.environ.get("CLIP_SEL", ".instruction-text"))
            shot_params = {"format": "png"}
            if clip:
                shot_params["clip"] = {"x": max(0, clip["x"] - 16), "y": max(0, clip["y"] - 40),
                                       "width": min(VIEW_W, clip["w"] + 32),
                                       "height": int(os.environ.get("CLIP_H", "360")), "scale": 1}
            shot = cdp.cmd("Page.captureScreenshot", **shot_params)
            path = f"output/hover_{tag}_{label}.png"
            with open(path, "wb") as f:
                f.write(base64.b64decode(shot["data"]))
            print("WROTE", path)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
