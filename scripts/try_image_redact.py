"""Redact one screenshot/photo for real (T43 manual run, outside tests).

Uses local Muse Glimmer (Ollama ``muse-glimmer:30b``) and, in ``on``
mode, Meta SAM 3.1 in the cloud (``MODEL_API_KEY``/``META_API_KEY``),
exactly like the upload path. Writes ``<stem>.redacted.png`` next to
the input. Run: ``python scripts/try_image_redact.py shot.png [--mode on|local]``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shadowfenced import image_redact  # noqa: E402
from shadowfenced.app import image_redaction_consent  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Redact one image for real.")
    parser.add_argument("image", type=Path)
    parser.add_argument("--mode", choices=("on", "local"), default="on")
    args = parser.parse_args()
    if not args.image.is_file():
        print(f"no such file: {args.image}", file=sys.stderr)
        return 2
    data = args.image.read_bytes()
    choice = image_redaction_consent({"choice": args.mode})
    print(f"mode: {choice['mode']}")
    try:
        redacted = image_redact.redact_image(data, sam_enabled=(args.mode == "on"))
    except image_redact.ImageRedactError as exc:
        print(f"redaction failed: {exc}", file=sys.stderr)
        return 1
    out = args.image.with_suffix("").with_name(args.image.stem + ".redacted.png")
    out.write_bytes(redacted.png)
    print(f"wrote {out} ({redacted.method}, {len(redacted.regions)} regions)")
    for text in redacted.texts:
        print(f"  read: {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
