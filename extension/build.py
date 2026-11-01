"""Build Shadowfenced dist/ + zips from the shared sources (stdlib only).

Runnable as:  python extension/build.py   (from the repo root)

Writes:
  extension/dist/chrome/   (sources + manifest_chrome.json -> manifest.json)
  extension/dist/firefox/  (sources + manifest_firefox.json -> manifest.json)
  extension/dist/shadowfenced-chrome.zip
  extension/dist/shadowfenced-firefox.zip
"""

import json
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # extension/
SRC = HERE / "src"
DIST = HERE / "dist"

TARGETS = {
    "chrome": HERE / "manifest_chrome.json",
    "firefox": HERE / "manifest_firefox.json",
}

ZIP_NAMES = {
    "chrome": "shadowfenced-chrome.zip",
    "firefox": "shadowfenced-firefox.zip",
}


def validate_manifest(data: dict, target: str) -> list[str]:
    errors = []
    if data.get("manifest_version") != 3:
        errors.append("manifest_version must be 3")
    if data.get("permissions") != ["storage"]:
        errors.append("permissions must be exactly ['storage']")
    hosts = set(data.get("host_permissions", []))
    if "http://127.0.0.1:8765/*" not in hosts:
        errors.append("host_permissions must include http://127.0.0.1:8765/*")
    for site in ("chatgpt.com", "claude.ai", "gemini.google.com", "meta.ai"):
        if not any(site in h for h in hosts):
            errors.append(f"host_permissions missing site {site}")
    if len(hosts - {"http://127.0.0.1:8765/*",
                    "https://chatgpt.com/*", "https://claude.ai/*",
                    "https://gemini.google.com/*", "https://meta.ai/*",
                    "https://www.meta.ai/*"}) != 0:
        errors.append(f"host_permissions has unexpected entries: {sorted(hosts)}")
    if target == "firefox" and not data.get("browser_specific_settings", {}).get("gecko", {}).get("id"):
        errors.append("firefox manifest needs browser_specific_settings.gecko.id")
    return errors


def zip_dir(src_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(src_dir))


def build(target: str) -> Path:
    out = DIST / target
    if out.exists():
        shutil.rmtree(out)
    (out / "src").mkdir(parents=True)
    manifest_src = TARGETS[target]
    manifest = json.loads(manifest_src.read_text(encoding="utf-8"))
    errors = validate_manifest(manifest, target)
    if errors:
        raise SystemExit(f"{manifest_src.name} invalid: {'; '.join(errors)}")
    for path in sorted(SRC.iterdir()):
        if path.is_file():
            shutil.copy2(path, out / "src" / path.name)
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    zip_path = DIST / ZIP_NAMES[target]
    if zip_path.exists():
        zip_path.unlink()
    zip_dir(out, zip_path)
    return zip_path


def main() -> int:
    if not SRC.is_dir():
        raise SystemExit(f"src/ not found at {SRC}")
    DIST.mkdir(exist_ok=True)
    for target in TARGETS:
        zp = build(target)
        print(f"{target}: wrote {DIST / target} + {zp.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
