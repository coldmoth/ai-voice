import re
from pathlib import Path

from ai_voice.desktop import render_page

WEB = Path(__file__).resolve().parents[1] / "macos" / "desktop"


def test_app_js_has_no_hardcoded_devices():
    source = (WEB / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"\|\|\s*'(MIC|AI Voice)'", source)
    assert not re.search(r"input_device:\s*'MIC'", source)
    assert not re.search(r"output_device:\s*'AI Voice'", source)


def test_index_has_no_hardcoded_device_options():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "<option>MIC</option>" not in html
    assert "<option>AI Voice</option>" not in html


def test_render_page_inlines_onboarding_and_token():
    page = render_page(WEB, "x").decode()
    assert (WEB / "onboarding.js").read_text(encoding="utf-8") in page
    assert (WEB / "onboarding.css").read_text(encoding="utf-8") in page
    assert 'const APP_TOKEN = "x";' in page
    assert "__STYLE__" not in page and "__SCRIPT__" not in page
