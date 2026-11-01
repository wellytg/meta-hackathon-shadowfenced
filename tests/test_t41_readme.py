"""T41 README v3 tests: links resolve, banned absolute claims stay out.

No network, no models: plain text scans of ``README.md`` only.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"

BANNED_PHRASES = (
    "zero",
    "100%",
    "unhackable",
    "never leaks",
    "nothing sensitive crosses",
)

# Bare tokens with no path shape are placeholders, not file links.
PLACEHOLDERS = {"LIVE_DEMO_URL", "VIDEO_URL"}


def _link_targets(text: str) -> list[str]:
    """Inline markdown links plus HTML image/source paths in README order."""
    targets = [m.group(1).strip() for m in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", text)]
    targets += re.findall(r'<img[^>]+src="([^"]+)"', text)
    targets += re.findall(r'<source[^>]+srcset="([^"]+)"', text)
    return targets


def _is_external(target: str) -> bool:
    return (
        target.startswith(("http://", "https://", "mailto:", "#"))
        or target in PLACEHOLDERS
    )


def test_readme_relative_links_exist():
    targets = [t for t in _link_targets(README.read_text(encoding="utf-8")) if t]
    assert targets, "README should contain links and image paths"
    missing = []
    for target in targets:
        if _is_external(target):
            continue
        path = (ROOT / target.split("#")[0]).resolve()
        try:
            ok = path.is_file() or path.is_dir()
        except OSError:
            ok = False
        if not ok:
            missing.append(target)
    assert missing == [], f"README links with no file on disk: {missing}"


def test_readme_code_drawn_banner_not_generated_png():
    text = README.read_text(encoding="utf-8")
    assert "docs/assets/banner.svg" in text
    assert "docs/assets/generated/banner.png" not in text


def test_readme_has_no_banned_phrases():
    text = README.read_text(encoding="utf-8").lower()
    found = [phrase for phrase in BANNED_PHRASES if phrase in text]
    assert found == [], f"README contains banned absolute claims: {found}"
