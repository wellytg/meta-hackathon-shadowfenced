"""Screenshot redaction tests (T43): Glimmer + SAM 3.1, consent, hosted.

No real network: Glimmer answers through an httpx ``MockTransport``
and SAM 3.1 answers through an httpx ``MockTransport`` carrying a
fixture string in the parser's wire format. Synthetic images are drawn
in the test with Pillow (white canvas, a fake email and a fake key);
fictional values only (``@corvane.io``).
"""

from __future__ import annotations

import io
import json
import sys
import types

import httpx
import pytest
from fastapi import HTTPException

from shadowfenced import app as app_module
from shadowfenced import holds, image_redact, uploads
from shadowfenced.app import (
    SendRequest,
    app,
    handle_upload,
    image_redaction_consent,
    image_redaction_status,
    send,
    upload_image,
)

EMAIL = "ada@corvane.io"
CANVAS = (400, 200)

#: SAM 3.1 output lane in the meta-sam-parser wire format: one frame
#: line, comma-separated ``id<|box;...|><|mask;...|>`` records.
SAM_FIXTURE = (
    "<0f>0<|box;x1=10;y1=10;x2=100;y2=40;w=400;h=200|>"
    "<|mask;x=0;y=0;data=400,200,~FAKE|>"
    ",1<|box;x1=10;y1=90;x2=200;y2=120;w=400;h=200|>"
    "<|mask;x=0;y=0;data=400,200,~FAKE|>"
)

GLIMMER_REGIONS = {
    "regions": [
        {
            "prompt": "email address",
            "box": [10, 10, 100, 40],
            "text": EMAIL,
        },
        {
            "prompt": "badge number on a terminal screen",
            "box": [10, 90, 200, 120],
            "text": "",
        },
    ]
}


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    # No ambient Meta key: the sandbox exports one, and these tests must
    # prove the local-only paths never touch the cloud. Tests that need
    # SAM set their own key explicitly.
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    uploads.reset_for_tests()
    holds.reset_for_tests()
    app_module.reset_state()
    yield
    uploads.reset_for_tests()
    holds.reset_for_tests()
    app_module.reset_state()
    for attr in ("upstream_transport", "glimmer_transport", "sam_transport"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


def make_image(fmt: str = "PNG", with_metadata: bool = False) -> bytes:
    """White canvas with a fake email and a fake key drawn on it."""
    from PIL import Image, ImageDraw
    from PIL import PngImagePlugin

    image = Image.new("RGB", CANVAS, (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.text((10, 15), EMAIL, fill=(0, 0, 0))
    draw.text((10, 95), "api key: hunter2-test-key", fill=(0, 0, 0))
    buf = io.BytesIO()
    if with_metadata:
        info = PngImagePlugin.PngInfo()
        info.add_text("Author", "Mallory Redactor")
        image.save(buf, format=fmt, pnginfo=info)
    else:
        image.save(buf, format=fmt)
    return buf.getvalue()


def _glimmer_transport(regions=None, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        body = {"message": {"content": json.dumps(regions if regions is not None else GLIMMER_REGIONS)}}
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def _sam_transport(calls=None, status: int = 200, text: str = SAM_FIXTURE):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        assert request.url.path == "/v1/responses"
        payload = {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


def _install_fake_parser(monkeypatch, raster: bytes, width: int, height: int):
    """Stand in for meta-sam-parser: decode yields the canned raster."""
    module = types.ModuleType("meta_sam_parser")

    class CompletedOutcome:
        pass

    def decode_mask_to_raster(mask):
        assert (mask.width, mask.height) == (width, height)
        return bytes(raster)

    class _Parser:
        def push(self, text):
            assert "<0f>" in text
            return []

        def finish(self, outcome):
            record = types.SimpleNamespace(
                kind="mask",
                mask=types.SimpleNamespace(width=width, height=height),
            )
            return types.SimpleNamespace(result=types.SimpleNamespace(records=[record]), events=[])

    class _Format:
        def create_parser(self):
            return _Parser()

    def image_segmentation_format():
        return _Format()

    module.CompletedOutcome = CompletedOutcome
    module.decode_mask_to_raster = decode_mask_to_raster
    module.image_segmentation_format = image_segmentation_format
    monkeypatch.setitem(sys.modules, "meta_sam_parser", module)
    return module


def test_consent_required_by_default():
    png = make_image()

    def _no_network(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no network call expected before consent")

    dead = httpx.MockTransport(_no_network)
    uploaded = handle_upload(
        "shot.png", png, glimmer_transport=dead, sam_transport=dead
    )
    assert uploaded["consent_required"] is True
    assert uploaded["status"] == "consent_required"
    assert uploaded["notice"] == image_redact.NOTICE_TEXT
    assert uploaded["notice"] == (
        "Images are checked on this PC by Muse Glimmer. "
        "Exact masks use Meta SAM 3.1 in the cloud, "
        "which receives the original image."
    )
    assert uploaded["file_id"] == ""
    assert uploaded["choices"] == ["on", "local", "off"]
    assert uploads.UPLOADS.load("no-such-file") is None


def test_consent_choice_stored_and_bad_choice_rejected():
    assert image_redaction_consent({"choice": "local"}) == {"mode": "local"}
    assert image_redaction_status()["mode"] == "local"
    with pytest.raises(HTTPException) as exc:
        image_redaction_consent({"choice": "cloud"})
    assert exc.value.status_code == 400


def test_local_box_fill_redacts_and_strips_metadata():
    image_redaction_consent({"choice": "local"})
    png = make_image(with_metadata=True)
    from PIL import Image

    assert Image.open(io.BytesIO(png)).info.get("Author") == "Mallory Redactor"
    uploaded = handle_upload(
        "shot.png", png, glimmer_transport=_glimmer_transport()
    )
    assert uploaded["status"] == "flagged"  # Tier 1 sees the Glimmer email
    assert uploaded["regions"] == 2
    assert uploaded["method"] == "box"
    assert uploaded["summary_text"].startswith("2 regions hidden")
    assert uploaded["thumbnail"].startswith("data:image/png;base64,")
    record = uploads.UPLOADS.load(uploaded["file_id"])
    assert record is not None and record.image_png
    assert record.image_png != png  # the original is never kept
    redacted = Image.open(io.BytesIO(record.image_png)).convert("RGB")
    assert redacted.size == CANVAS
    assert redacted.getpixel((50, 25)) == (0, 0, 0)  # email box filled
    assert redacted.getpixel((50, 105)) == (0, 0, 0)  # key box filled
    assert redacted.getpixel((350, 180)) == (255, 255, 255)
    assert Image.open(io.BytesIO(record.image_png)).info == {}
    body = upload_image(uploaded["file_id"])
    assert body.media_type == "image/png"
    assert body.body == record.image_png
    with pytest.raises(HTTPException) as exc:
        upload_image("no-such-file")
    assert exc.value.status_code == 404


def test_sam_masks_win_over_boxes_with_fake_parser(monkeypatch):
    monkeypatch.setenv("META_API_KEY", "test-meta-key")
    image_redaction_consent({"choice": "on"})
    width, height = CANVAS
    raster = bytearray(width * height)
    for y in range(120, 151):
        for x in range(250, 301):
            raster[y * width + x] = 1
    _install_fake_parser(monkeypatch, bytes(raster), width, height)
    calls: list[httpx.Request] = []
    uploaded = handle_upload(
        "shot.png",
        make_image(),
        glimmer_transport=_glimmer_transport(),
        sam_transport=_sam_transport(calls),
    )
    assert uploaded["method"] == "sam"
    request = calls[0]
    assert request.headers["Authorization"].startswith("Bearer ")
    sent = json.loads(request.content.decode("utf-8"))
    assert sent["model"] == "sam-3.1"
    content = sent["input"][0]["content"]
    kinds = {part["type"] for part in content}
    assert {"input_text", "input_image"} <= kinds
    assert any(
        part.get("image_url", "").startswith("data:image/png;base64,")
        for part in content
        if part["type"] == "input_image"
    )
    from PIL import Image

    record = uploads.UPLOADS.load(uploaded["file_id"])
    redacted = Image.open(io.BytesIO(record.image_png)).convert("RGB")
    assert redacted.getpixel((260, 130)) == (0, 0, 0)  # mask raster filled
    assert redacted.getpixel((50, 25)) == (255, 255, 255)  # box ignored


def test_sam_failure_falls_back_to_boxes_fail_closed(monkeypatch):
    monkeypatch.setenv("META_API_KEY", "test-meta-key")
    image_redaction_consent({"choice": "on"})
    uploaded = handle_upload(
        "shot.png",
        make_image(),
        glimmer_transport=_glimmer_transport(),
        sam_transport=_sam_transport(status=500, text=""),
    )
    assert uploaded["method"] == "box"
    assert uploaded["regions"] == 2
    from PIL import Image

    record = uploads.UPLOADS.load(uploaded["file_id"])
    redacted = Image.open(io.BytesIO(record.image_png)).convert("RGB")
    assert redacted.getpixel((50, 25)) == (0, 0, 0)
    assert record.image_png != make_image()


def test_sam_on_without_key_stays_local():
    image_redaction_consent({"choice": "on"})

    def _no_network(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("SAM must not be called without a key")

    uploaded = handle_upload(
        "shot.png",
        make_image(),
        glimmer_transport=_glimmer_transport(),
        sam_transport=httpx.MockTransport(_no_network),
    )
    assert uploaded["method"] == "box"
    assert uploaded["regions"] == 2


def test_glimmer_failure_is_unscannable_and_holds_send():
    image_redaction_consent({"choice": "local"})
    uploaded = handle_upload(
        "shot.png", make_image(), glimmer_transport=_glimmer_transport(status=500)
    )
    assert uploaded["status"] == "unscannable"
    body = send(SendRequest(messages=[{"role": "user", "content": "look"}], file_ids=[uploaded["file_id"]]))
    assert body["decision"] == "HOLD"
    assert body["sent_text"] is None


def test_only_redacted_text_may_send():
    image_redaction_consent({"choice": "local"})
    calls: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "noted"}}]},
        )

    app.state.upstream_transport = httpx.MockTransport(upstream)
    uploaded = handle_upload(
        "shot.png", make_image(), glimmer_transport=_glimmer_transport()
    )
    assert uploaded["status"] == "flagged"
    body = send(
        SendRequest(
            messages=[{"role": "user", "content": "describe this"}],
            file_ids=[uploaded["file_id"]],
        )
    )
    assert body["decision"] == "REDACT"
    forwarded = json.loads(calls[0].content.decode("utf-8"))
    flat = json.dumps(forwarded)
    assert "Attached image shot.png:" in flat
    assert EMAIL not in flat
    assert "PII_EMAIL" in flat


def test_jpeg_input_and_no_regions_clean():
    image_redaction_consent({"choice": "local"})
    uploaded = handle_upload(
        "photo.jpg",
        make_image(fmt="JPEG"),
        glimmer_transport=_glimmer_transport(regions={"regions": []}),
    )
    assert uploaded["status"] == "clean"
    assert uploaded["regions"] == 0
    assert uploaded["method"] == "none"
    from PIL import Image

    record = uploads.UPLOADS.load(uploaded["file_id"])
    assert Image.open(io.BytesIO(record.image_png)).format == "PNG"


def test_wire_box_extraction():
    boxes = image_redact._wire_boxes(SAM_FIXTURE)
    assert boxes == [(10, 10, 100, 40), (10, 90, 200, 120)]


def test_hosted_mode_refuses_images(monkeypatch):
    monkeypatch.setenv("SF_HOSTED", "1")
    app_module.reset_state()
    with pytest.raises(HTTPException) as exc:
        handle_upload("shot.png", make_image())
    assert exc.value.status_code == 403
    assert "hosted demo mode" in exc.value.detail["message"]
    assert image_redaction_status()["mode"] == "off"
