"""Legacy UI resources, separate from the authenticated V2 UI in app/web."""
from pathlib import Path

WEB = Path(__file__).with_name('web')


def read_asset(name):
    return (WEB / name).read_text(encoding='utf-8')


INDEX = read_asset('index.html')
TOKEN_GATE = read_asset('token_gate.html')
# Source previews embed their own CSS/JS so they remain self-contained under
# the existing restrictive Content-Security-Policy.
_PREVIEW_STYLE = '<style id="kgsz-style">' + read_asset('preview.css') + '</style>'
_PREVIEW_SCRIPT = '<script id="kgsz-script">' + read_asset('preview.js') + '</script>'
