"""Generate T46 Classic-style visuals with Meta's Muse Image model.

Meta Model API docs: image generation lives under the same base URL as
chat (``META_BASE_URL``, default ``https://api.meta.ai/v1``) at
``POST {base}/images/generations`` with a Bearer key. The model id for
Muse Image is ``muse-image-1.0`` (overridable with ``META_IMAGE_MODEL`` or
``--model``). The request body follows the OpenAI-compatible shape::

    {"model": ..., "prompt": ..., "size": ..., "n": 1}

and the reply is ``{"data": [{"b64_json": ...}]}`` (preferred) or
``{"data": [{"url": ...}]}`` (downloaded with a second GET).

The key is read exactly the way ``upstream.py`` reads it -- through
``shadowfenced.config.get_settings`` (``META_API_KEY`` / ``MODEL_API_KEY``
alias / OS credential store) -- and is never printed, logged, or written
anywhere. ``--dry-run`` prints only the URL, model, size, output name and
prompt (never the Authorization header).

Run from the repo root (the owner's side, with network)::

    python scripts/make_visuals.py --dry-run
    python scripts/make_visuals.py --only banner
    python scripts/make_visuals.py
"""

from __future__ import annotations

import argparse
import base64
import datetime
import sys
from io import BytesIO
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402
import yaml  # noqa: E402
from PIL import Image  # noqa: E402

from shadowfenced.config import get_settings  # noqa: E402
from shadowfenced.upstream import _verify_for  # noqa: E402

DEFAULT_MODEL = "muse-image-1.0"
# Square sizes the image API accepts; non-square YAML sizes are requested
# at the nearest of these and then cropped/resized locally with Pillow.
SUPPORTED_SQUARE_SIDES = (256, 512, 1024)
PROMPTS_FILE = ROOT / "docs" / "assets" / "visual_prompts.yaml"
OUT_DIR = ROOT / "docs" / "assets" / "generated"

# Placeholder key used only to prove the dry-run path never leaks.
_DRY_RUN_PLACEHOLDER = "key-redacted"


def load_prompts(path: Path) -> tuple[str, list[dict[str, Any]]]:
    """Return (model, entries) from the prompts YAML file."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("images"), list):
        raise ValueError(f"{path} must map to an 'images' list")
    model = str(raw.get("model") or DEFAULT_MODEL)
    entries: list[dict[str, Any]] = []
    for item in raw["images"]:
        if not isinstance(item, dict):
            raise ValueError(f"bad entry in {path}: {item!r}")
        for field in ("name", "output", "size", "prompt"):
            if not item.get(field):
                raise ValueError(f"entry missing {field!r}: {item!r}")
        entries.append(item)
    names = [entry["name"] for entry in entries]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate entry names in {path}")
    return model, entries


def parse_size(size: str) -> tuple[int, int]:
    """Parse a ``WxH`` size string into (width, height)."""
    try:
        width_s, height_s = str(size).lower().split("x")
        width, height = int(width_s), int(height_s)
    except ValueError as exc:
        raise ValueError(f"bad size {size!r}: want WxH") from exc
    if width <= 0 or height <= 0:
        raise ValueError(f"bad size {size!r}: want WxH")
    return width, height


def request_size_for(size: str) -> str:
    """Nearest supported square API size for a YAML ``WxH`` size.

    The API only serves squares, so a non-square YAML size is requested
    at the supported square side closest to its longest edge (ties go to
    the larger square) and resized locally afterwards.
    """
    width, height = parse_size(size)
    side = width if width == height else max(width, height)
    nearest = min(
        SUPPORTED_SQUARE_SIDES, key=lambda s: (abs(s - side), -s)
    )
    return f"{nearest}x{nearest}"


def build_request(
    base_url: str, model: str, entry: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Return (url, json body) for one image entry (no credentials)."""
    url = base_url.rstrip("/") + "/images/generations"
    body = {
        "model": model,
        "prompt": entry["prompt"],
        "size": request_size_for(str(entry["size"])),
        "n": 1,
    }
    return url, body


def dry_run_report(
    url: str, model: str, entries: list[dict[str, Any]]
) -> str:
    """Render the requests that would be sent (key never included)."""
    lines = [f"POST {url}", f"model: {model}"]
    for entry in entries:
        lines.append(f"--- {entry['name']} -> {entry['output']} [{entry['size']}]")
        lines.append(str(entry["prompt"]))
    return "\n".join(lines) + "\n"


def detect_format(raw: bytes) -> str:
    """Detect an image format from magic bytes (never trusts extensions)."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    if raw[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if raw[:2] == b"BM":
        return "bmp"
    return "unknown"


def image_from_bytes(raw: bytes) -> Image.Image:
    """Decode raw image bytes (WebP, PNG, ...) into a Pillow image."""
    fmt = detect_format(raw)
    try:
        img = Image.open(BytesIO(raw))
        img.load()
    except Exception as exc:
        raise RuntimeError(
            f"image API returned undecodable bytes ({fmt})"
        ) from exc
    if "A" in img.getbands():
        return img.convert("RGBA")
    return img.convert("RGB")


def fit_to_size(img: Image.Image, size: str) -> Image.Image:
    """Centre-crop to the ``WxH`` aspect, then resize to exactly ``WxH``."""
    width, height = parse_size(size)
    src_w, src_h = img.size
    target_aspect = width / height
    src_aspect = src_w / src_h
    if src_aspect > target_aspect:
        # Too wide: crop the sides.
        crop_w = max(1, round(src_h * target_aspect))
        left = (src_w - crop_w) // 2
        img = img.crop((left, 0, left + crop_w, src_h))
    elif src_aspect < target_aspect:
        # Too tall: crop top and bottom.
        crop_h = max(1, round(src_w / target_aspect))
        top = (src_h - crop_h) // 2
        img = img.crop((0, top, src_w, top + crop_h))
    if img.size != (width, height):
        img = img.resize((width, height), Image.LANCZOS)
    return img


def save_image_bytes(raw: bytes, out_path: Path, size: str) -> None:
    """Decode raw bytes and save them as a PNG at exactly ``size``."""
    img = fit_to_size(image_from_bytes(raw), size)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG")


def save_image_payload(payload: Any, out_path: Path) -> None:
    """Save one ``/images/generations`` payload's first image as PNG."""
    try:
        datum = payload["data"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("image API returned bad JSON") from exc
    if isinstance(datum, dict) and datum.get("b64_json"):
        try:
            raw = base64.b64decode(datum["b64_json"])
        except (ValueError, TypeError) as exc:
            raise RuntimeError("image API returned bad base64") from exc
        image_from_bytes(raw).save(out_path, format="PNG")
        return
    if isinstance(datum, dict) and datum.get("url"):
        raise RuntimeError(
            f"image API returned a URL for {out_path.name}; "
            "downloading is handled by the caller"
        )
    raise RuntimeError("image API returned neither b64_json nor url")


def download_url(url: str, timeout: float = 90.0) -> bytes:
    """Download one generated image URL (owner's side only)."""
    with httpx.Client(
        timeout=timeout, verify=_verify_for(url)
    ) as client:
        response = client.get(url)
    if response.status_code != 200:
        raise RuntimeError(f"image download status {response.status_code}")
    return response.content


def write_manifest(
    out_dir: Path, model: str, entries: list[dict[str, Any]], date: str
) -> Path:
    """Write MANIFEST.md recording model, prompt and date per image."""
    lines = [
        "# Generated visuals (T46)",
        "",
        f"Model: `{model}`",
        f"Date: {date}",
        "",
        "Generated with Meta Muse Image (`scripts/make_visuals.py`). "
        "Prompts: `docs/assets/visual_prompts.yaml`.",
        "",
    ]
    for entry in entries:
        lines += [
            f"## {entry['name']}",
            "",
            f"- File: `{entry['output']}`",
            f"- Model: `{model}`",
            f"- Size: `{entry['size']}`",
            f"- Date: {date}",
        ]
        requested = request_size_for(str(entry["size"]))
        if requested != str(entry["size"]):
            lines.append(
                f"- Requested: `{requested}` (API serves squares; "
                f"centre-cropped/resized locally to `{entry['size']}`)"
            )
        lines += [
            "- Prompt:",
            "",
            "  ```",
        ]
        lines += [f"  {line}" for line in str(entry["prompt"]).splitlines()]
        lines += ["  ```", ""]
    path = out_dir / "MANIFEST.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def generate_one(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    model: str,
    entry: dict[str, Any],
    out_dir: Path,
) -> Path:
    """Send one prompt and save its PNG at the YAML size; return the path."""
    _, body = build_request(url.split("/images/generations")[0], model, entry)
    try:
        response = client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"request failed: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise RuntimeError(f"image API status {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("image API returned bad JSON") from exc
    out_path = out_dir / entry["output"]
    try:
        datum = payload["data"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("image API returned bad JSON") from exc
    if isinstance(datum, dict) and datum.get("b64_json"):
        try:
            raw = base64.b64decode(datum["b64_json"])
        except (ValueError, TypeError) as exc:
            raise RuntimeError("image API returned bad base64") from exc
        save_image_bytes(raw, out_path, str(entry["size"]))
    elif isinstance(datum, dict) and datum.get("url"):
        save_image_bytes(
            download_url(str(datum["url"])), out_path, str(entry["size"])
        )
    else:
        raise RuntimeError("image API returned neither b64_json nor url")
    return out_path


def main(
    argv: list[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Generate T46 visuals.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only", default=None, help="Run one entry NAME only")
    parser.add_argument("--prompts", default=str(PROMPTS_FILE))
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    parser.add_argument("--model", default=None)
    args = parser.parse_args(argv)

    try:
        file_model, entries = load_prompts(Path(args.prompts))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    import os

    model = (
        args.model
        or os.environ.get("META_IMAGE_MODEL", "").strip()
        or file_model
    )
    if args.only:
        entries = [entry for entry in entries if entry["name"] == args.only]
        if not entries:
            print(f"error: unknown entry {args.only!r}", file=sys.stderr)
            return 1

    settings = get_settings()
    base_url = settings.meta_base_url
    url, _ = build_request(base_url, model, entries[0])
    # Rebuild the canonical URL once (identical for every entry).
    url = base_url.rstrip("/") + "/images/generations"

    if args.dry_run:
        # The key is deliberately never part of this output.
        print(dry_run_report(url, model, entries), end="")
        return 0

    if not settings.meta_api_key:
        print("error: META_API_KEY (or MODEL_API_KEY) is not set", file=sys.stderr)
        return 2
    headers = {"Authorization": "Bearer " + settings.meta_api_key}
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    date = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    failures = 0
    succeeded: list[dict[str, Any]] = []
    with httpx.Client(
        transport=transport, timeout=90.0, verify=_verify_for(url)
    ) as client:
        for entry in entries:
            try:
                generate_one(client, url, headers, model, entry, out_dir)
            except (RuntimeError, httpx.HTTPError) as exc:
                # Never echo headers or the key in failure messages.
                failures += 1
                print(
                    f"error {entry['name']}: {exc}", file=sys.stderr
                )
                continue
            succeeded.append(entry)
            print(f"ok {entry['output']} {entry['size']}")
    write_manifest(out_dir, model, succeeded, date)
    print(f"wrote {out_dir / 'MANIFEST.md'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
