"""T46b: make_visuals.py against the measured image API behavior.

No test touches the network: an httpx MockTransport returns a tiny WebP
image made with Pillow in the test, exactly like the real API's
``{"data": [{"b64_json": ...}]}`` shape. Key values are synthetic sentinels.
"""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

import httpx
import yaml
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import make_visuals as mv  # noqa: E402

PROMPTS = ROOT / "docs" / "assets" / "visual_prompts.yaml"
SENTINEL = "unit-test-sentinel-t46b-abc123"


def _webp_b64(width: int = 48, height: int = 32) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, format="WEBP")
    raw = buf.getvalue()
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    return base64.b64encode(raw).decode("ascii")


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _ok_handler(request: httpx.Request) -> httpx.Response:
    body = __import__("json").loads(request.content.decode("utf-8"))
    assert body["model"] == "muse-image-1.0", body
    assert body["size"].endswith("x" + body["size"].split("x")[1])
    width_s, height_s = body["size"].split("x")
    assert width_s == height_s, body  # API only serves squares
    return httpx.Response(
        200, json={"created": 1, "data": [{"b64_json": _webp_b64()}]},
    )


def _write_prompts(path: Path, images: list[dict]) -> None:
    path.write_text(
        yaml.safe_dump({"model": "muse-image-1.0", "images": images}),
        encoding="utf-8",
    )


def test_default_model_is_measured_id():
    assert mv.DEFAULT_MODEL == "muse-image-1.0"
    raw = yaml.safe_load(PROMPTS.read_text(encoding="utf-8"))
    assert raw["model"] == "muse-image-1.0"


def test_non_square_requests_square_and_saves_yaml_size(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("META_API_KEY", SENTINEL)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    prompts = tmp_path / "prompts.yaml"
    _write_prompts(
        prompts,
        [
            {
                "name": "banner",
                "output": "banner.png",
                "size": "1280x400",
                "prompt": "a wide hero banner",
            },
            {
                "name": "panel-1-typing",
                "output": "panel-1-typing.png",
                "size": "64x64",
                "prompt": "a small square panel",
            },
        ],
    )
    out_dir = tmp_path / "out"
    rc = mv.main(
        ["--prompts", str(prompts), "--out-dir", str(out_dir)],
        transport=_transport(_ok_handler),
    )
    assert rc == 0
    out = capsys.readouterr()
    assert "ok banner.png 1280x400" in out.out
    assert "ok panel-1-typing.png 64x64" in out.out
    assert SENTINEL not in (out.out + out.err)
    for name, size in (("banner.png", (1280, 400)), ("panel-1-typing.png", (64, 64))):
        data = (out_dir / name).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", name  # PNG, not WebP
        with Image.open(io.BytesIO(data)) as img:
            assert img.size == size, name
    manifest = (out_dir / "MANIFEST.md").read_text(encoding="utf-8")
    assert "muse-image-1.0" in manifest
    assert "centre-cropped/resized locally to `1280x400`" in manifest


def test_one_failure_does_not_stop_the_others(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("META_API_KEY", SENTINEL)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    prompts = tmp_path / "prompts.yaml"
    _write_prompts(
        prompts,
        [
            {
                "name": "broken",
                "output": "broken.png",
                "size": "64x64",
                "prompt": "FAIL this one",
            },
            {
                "name": "good",
                "output": "good.png",
                "size": "64x64",
                "prompt": "a fine panel",
            },
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content.decode("utf-8"))
        if "FAIL" in body["prompt"]:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(
            200, json={"created": 1, "data": [{"b64_json": _webp_b64()}]},
        )

    out_dir = tmp_path / "out"
    rc = mv.main(
        ["--prompts", str(prompts), "--out-dir", str(out_dir)],
        transport=_transport(handler),
    )
    assert rc != 0
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "error broken:" in combined
    assert "500" in combined
    assert "ok good.png 64x64" in out.out
    assert SENTINEL not in combined
    assert not (out_dir / "broken.png").exists()
    with Image.open(io.BytesIO((out_dir / "good.png").read_bytes())) as img:
        assert img.size == (64, 64)
    assert (out_dir / "MANIFEST.md").is_file()
