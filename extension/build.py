"""Build Shadowfenced dist/ + zips from the shared sources (stdlib only).

Runnable as:  python extension/build.py [--dev]   (from the repo root)

Release (default) writes:
  extension/dist/chrome/   (sources + manifest_chrome.json -> manifest.json)
  extension/dist/firefox/  (sources + manifest_firefox.json -> manifest.json)
  extension/dist/shadowfenced-chrome.zip
  extension/dist/shadowfenced-firefox.zip

Dev (--dev) writes sibling -dev outputs whose manifests additionally
match the local test page (extension/testpage/index.html, served over
loopback port 8000). The release manifests never mention the test page.
"""

import argparse
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

# Loopback-only origins that serve the local test page (dev builds only).
DEV_TEST_HOSTS = (
    "http://127.0.0.1:8000/*",
    "http://localhost:8000/*",
)


def validate_manifest(data: dict, target: str, dev: bool = False) -> list[str]:
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
    allowed = {"http://127.0.0.1:8765/*",
               "https://chatgpt.com/*", "https://claude.ai/*",
               "https://gemini.google.com/*", "https://meta.ai/*",
               "https://www.meta.ai/*"}
    if dev:
        allowed |= set(DEV_TEST_HOSTS)
    if len(hosts - allowed) != 0:
        errors.append(f"host_permissions has unexpected entries: {sorted(hosts)}")
    if dev:
        for extra in DEV_TEST_HOSTS:
            if extra not in hosts:
                errors.append(f"dev host_permissions missing {extra}")
    else:
        for extra in DEV_TEST_HOSTS:
            if extra in hosts:
                errors.append(f"release host_permissions must not include {extra}")
    matches = [m for block in data.get("content_scripts", [])
               for m in block.get("matches", [])]
    if dev:
        for extra in DEV_TEST_HOSTS:
            if extra not in matches:
                errors.append(f"dev content_scripts.matches missing {extra}")
    else:
        for m in matches:
            if "8000" in m or "testpage" in m or "localhost" in m:
                errors.append(f"release content_scripts.matches must not include {m}")
    if target == "firefox" and not data.get("browser_specific_settings", {}).get("gecko", {}).get("id"):
        errors.append("firefox manifest needs browser_specific_settings.gecko.id")
    return errors


def apply_dev_overlay(manifest: dict) -> dict:
    """Return a copy of the release manifest with the test-page pattern."""
    data = json.loads(json.dumps(manifest))
    data["name"] = data.get("name", "Shadowfenced") + " (dev)"
    hosts = list(data.get("host_permissions", []))
    for extra in DEV_TEST_HOSTS:
        if extra not in hosts:
            hosts.append(extra)
    data["host_permissions"] = hosts
    for block in data.get("content_scripts", []):
        matches = list(block.get("matches", []))
        for extra in DEV_TEST_HOSTS:
            if extra not in matches:
                matches.append(extra)
        block["matches"] = matches
    return data


def zip_dir(src_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(src_dir))


def build(target: str, dev: bool = False) -> Path:
    out_name = target + ("-dev" if dev else "")
    zip_name = ZIP_NAMES[target].replace(".zip", "-dev.zip" if dev else ".zip")
    out = DIST / out_name
    if out.exists():
        shutil.rmtree(out)
    (out / "src").mkdir(parents=True)
    manifest_src = TARGETS[target]
    manifest = json.loads(manifest_src.read_text(encoding="utf-8"))
    if dev:
        manifest = apply_dev_overlay(manifest)
    errors = validate_manifest(manifest, target, dev=dev)
    if errors:
        raise SystemExit(f"{manifest_src.name}{' (dev)' if dev else ''} invalid: {'; '.join(errors)}")
    shutil.copytree(SRC, out / "src", dirs_exist_ok=True)
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    zip_path = DIST / zip_name
    if zip_path.exists():
        zip_path.unlink()
    zip_dir(out, zip_path)
    return zip_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Shadowfenced extension.")
    parser.add_argument("--dev", action="store_true",
                        help="also build -dev outputs matching the local test page")
    args = parser.parse_args(argv)
    if not SRC.is_dir():
        raise SystemExit(f"src/ not found at {SRC}")
    DIST.mkdir(exist_ok=True)
    for target in TARGETS:
        zp = build(target, dev=args.dev)
        label = f"{target}-dev" if args.dev else target
        print(f"{label}: wrote {DIST / (target + ('-dev' if args.dev else ''))} + {zp.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
