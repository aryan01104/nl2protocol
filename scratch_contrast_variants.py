"""Render contrast-option variants for the middle column by injecting CSS overlays.

Scratch only. Reverts nothing in the template — each option is applied as a
runtime <style> overlay over the baseline demo, then column 2 is screenshotted.
Lets the user compare options side by side and pick.
"""
import base64
import json
import os
import tempfile
import time

from scratch_cdp_hover import CDP, launch_chrome, rect_of_raw, URL, VIEW_W, PORT

# Each variant overrides the step blocks / column-2 tray. Baseline = "".
VARIANTS = {
    "0_baseline": "",
    "A_white_cards_on_tray": (
        ".col-protocol-steps{background:#f8fafc;}"
        ".step{background:#fff;border:1px solid var(--grid);border-radius:4px;"
        "box-shadow:0 1px 2px rgba(15,23,42,.05);}"
    ),
    "B_grey_cards_white_col": (
        ".step{background:#f1f5f9;border:1px solid var(--grid);border-radius:4px;}"
    ),
    "C_border_shadow_only": (
        ".step{background:#fff;border:1px solid var(--grid);border-radius:4px;"
        "box-shadow:0 1px 3px rgba(15,23,42,.08);}"
    ),
    "D_warm_tray_cards": (
        ".col-protocol-steps{background:#f4f2ee;}"
        ".step{background:#fff;border:1px solid var(--grid);border-radius:6px;"
        "box-shadow:0 1px 3px rgba(15,23,42,.06);}"
    ),
}


def main():
    user_dir = tempfile.mkdtemp(prefix="cdp-")
    proc, ws_url = launch_chrome(user_dir)
    try:
        cdp = CDP(ws_url)
        cdp.cmd("Page.enable")
        cdp.cmd("Runtime.enable")
        cdp.cmd("Emulation.setDeviceMetricsOverride", width=VIEW_W, height=1300,
                deviceScaleFactor=2, mobile=False)
        for name, css in VARIANTS.items():
            cdp.cmd("Page.navigate", url=URL)
            cdp.wait_event("Page.loadEventFired", timeout=15)
            time.sleep(1.0)
            if css:
                cdp.cmd("Runtime.evaluate", expression=(
                    "(() => { const s = document.createElement('style');"
                    " s.textContent = %s; document.head.appendChild(s); })()" % json.dumps(css)))
                time.sleep(0.3)
            clip = rect_of_raw(cdp, ".col-protocol-steps")
            params = {"format": "png"}
            if clip:
                params["clip"] = {"x": max(0, clip["x"] - 16), "y": max(0, clip["y"] - 44),
                                  "width": min(VIEW_W, clip["w"] + 32), "height": 640, "scale": 1}
            shot = cdp.cmd("Page.captureScreenshot", **params)
            path = f"output/contrast_{name}.png"
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
