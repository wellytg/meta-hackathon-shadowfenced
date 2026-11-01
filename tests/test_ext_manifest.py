"""Extension manifest + source hygiene tests (T36).

Both store manifests must stay minimal (MV3, storage-only, loopback +
the four chat sites) and the extension sources must contain no remote
code or analytics.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

EXT = Path(__file__).resolve().parents[1] / "extension"
SRC = EXT / "src"

EXPECTED_HOSTS = {
    "http://127.0.0.1:8765/*",
    "https://chatgpt.com/*",
    "https://claude.ai/*",
    "https://gemini.google.com/*",
    "https://meta.ai/*",
    "https://www.meta.ai/*",
}
FOUR_SITES = ("chatgpt.com", "claude.ai", "gemini.google.com", "meta.ai")

REQUIRED_KEYS = (
    "manifest_version",
    "name",
    "version",
    "permissions",
    "host_permissions",
    "background",
    "action",
    "content_scripts",
)


def _load(name: str) -> dict:
    return json.loads((EXT / name).read_text(encoding="utf-8"))


def _manifests() -> dict[str, dict]:
    return {
        "manifest_chrome.json": _load("manifest_chrome.json"),
        "manifest_firefox.json": _load("manifest_firefox.json"),
    }


def test_required_mv3_keys():
    for name, data in _manifests().items():
        for key in REQUIRED_KEYS:
            assert key in data, f"{name} missing {key}"
        assert data["manifest_version"] == 3, name
        assert data["name"] and data["version"], name


def test_host_permissions_limited_to_loopback_plus_four_sites():
    for name, data in _manifests().items():
        hosts = set(data["host_permissions"])
        assert "http://127.0.0.1:8765/*" in hosts, name
        for site in FOUR_SITES:
            assert any(site in h for h in hosts), f"{name} missing {site}"
        assert hosts == EXPECTED_HOSTS, f"{name}: {sorted(hosts)}"


def test_permissions_storage_only():
    for name, data in _manifests().items():
        assert data["permissions"] == ["storage"], name


def test_firefox_manifest_has_gecko_id():
    data = _load("manifest_firefox.json")
    gecko = data.get("browser_specific_settings", {}).get("gecko", {})
    assert gecko.get("id"), "firefox manifest needs gecko id"


def test_content_scripts_cover_four_sites_and_exist():
    for name, data in _manifests().items():
        scripts = data["content_scripts"]
        assert scripts, name
        matches = [m for block in scripts for m in block.get("matches", [])]
        for site in FOUR_SITES:
            assert any(site in m for m in matches), f"{name} missing {site}"
        for block in scripts:
            for js in block.get("js", []):
                assert (EXT / js).is_file(), f"{name} references missing {js}"


def test_no_remote_code_in_sources():
    remote_script_src = re.compile(
        r"""<script[^>]+src\s*=\s*["']https?://""", re.IGNORECASE)
    remote_src_attr = re.compile(
        r'''\ssrc\s*=\s*["']https?://''', re.IGNORECASE)
    analytics = re.compile(
        r"google-analytics|googletagmanager|gtag\(|unpkg|jsdelivr"
        r"|analytics\.js|fbevents|hotjar|fullstory|segment\.io|mixpanel",
        re.IGNORECASE,
    )
    url_like = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
    for path in sorted(SRC.iterdir()):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert not remote_script_src.search(text), f"{path.name}: remote script src"
        assert not remote_src_attr.search(text), f"{path.name}: remote src attr"
        assert not analytics.search(text), f"{path.name}: analytics"
        for url in url_like.findall(text):
            assert url.startswith("http://127.0.0.1"), (
                f"{path.name}: non-loopback URL {url}"
            )
