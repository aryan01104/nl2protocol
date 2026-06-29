"""Serve the LIVE nl2protocol UI rendered with report4.html.jinja.

Scratch only (untracked). The live server (`nl2protocol.server.app`) renders
`report.html.jinja` at app.py:_render_live_page. This launcher monkeypatches
Jinja's get_template so the live page (and the end-of-run static archive) load
`report4.html.jinja` instead — without editing app.py, reporting.py, or
report.html.jinja. Run under the project venv:

    venv/bin/python scratch_serve_report4.py

then open http://127.0.0.1:8000/ and pick an example (e.g. bradford_assay).
The header band, fonts, and hover toggle render on a plain page load; running an
example end-to-end streams events and needs ANTHROPIC_API_KEY (from .env).
"""
import jinja2

_orig_get_template = jinja2.Environment.get_template


def _get_template(self, name, *args, **kwargs):
    if name == "report.html.jinja":
        name = "report4.html.jinja"
    return _orig_get_template(self, name, *args, **kwargs)


jinja2.Environment.get_template = _get_template


def main():
    try:
        from dotenv import load_dotenv, find_dotenv
        load_dotenv(find_dotenv(usecwd=True))
    except Exception:
        pass
    from nl2protocol.server import run_serve
    run_serve(host="127.0.0.1", port=8000, open_browser=False,
              examples_dir="test_cases/examples")


if __name__ == "__main__":
    main()
