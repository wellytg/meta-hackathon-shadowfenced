"""Screenshot redaction with Muse Glimmer + SAM 3.1 (T43).

Flow: local Muse Glimmer (Ollama model ``muse-glimmer:30b``, vision
capable) looks at the attached PNG/JPEG and lists sensitive regions as
text prompts and/or pixel boxes; Meta SAM 3.1 (Model API model
``sam-3.1``, served through the Responses API) turns them into exact
masks; Pillow fills the masks solid black. Only the redacted image may
be sent: when a region was found but no mask or box is usable, this
module raises and the caller must fail closed (never the original).

Request shapes follow the public docs:

- SAM image request: ``POST {META_BASE_URL}/responses`` with
  ``{"model": "sam-3.1", "input": [{"type": "message", "role": "user",
  "content": [{"type": "input_text", "text": prompt},
  {"type": "input_image", "image_url": data_url}]}]}`` (see
  ``dev.meta.ai/docs/sam/segmenting``: one concept per request, or one
  request carrying box/point prompts for several object ids).
- SAM output: special-token text in one ``output_text`` lane, one line
  per frame: ``<Nf>`` then comma-separated
  ``id<|box;...|><|mask;...|>`` records (see the SAM 3 protocol doc in
  ``meta-models/meta-sam``). Boxes are plain text and are extracted
  directly; mask rasters are decoded with the ``meta-sam-parser``
  package (``image_segmentation_format`` + ``decode_mask_to_raster``),
  imported lazily so this app still starts when it is not installed.
- Glimmer request: local Ollama ``/api/chat`` with the image on the
  message (``images: [b64]``), like :mod:`judge` but vision-capable.

The Meta key is read from settings (``META_API_KEY`` / ``MODEL_API_KEY``
via :mod:`config`); it is only placed in the request header and is never
printed or logged here.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings, get_settings
from .upstream import _verify_for

logger = logging.getLogger(__name__)

#: Image suffixes handled by this module (spec: PNG/JPEG only).
IMAGE_SUFFIXES: tuple[str, ...] = (".png", ".jpg", ".jpeg")

#: Local vision model that finds the sensitive regions.
GLIMMER_MODEL = "muse-glimmer:30b"

#: Cloud segmentation model that turns prompts into exact masks.
SAM_MODEL = "sam-3.1"

#: Path appended to ``META_BASE_URL`` (default ``https://api.meta.ai/v1``).
SAM_RESPONSES_PATH = "/responses"

SAM_TIMEOUT_S = 90.0
GLIMMER_TIMEOUT_S = 120.0

#: Exact notice shown when an image arrives while redaction is off.
NOTICE_TEXT = (
    "Images are checked on this PC by Muse Glimmer. "
    "Exact masks use Meta SAM 3.1 in the cloud, "
    "which receives the original image."
)

#: Wire boxes: <|box;x1=..;y1=..;x2=..;y2=..;w=..;h=..|> (x2/y2 inclusive).
_BOX_RE = re.compile(
    r"<\|box;x1=(\d+);y1=(\d+);x2=(\d+);y2=(\d+);w=\d+;h=\d+\|>"
)


class ImageRedactError(RuntimeError):
    """Image redaction failed; the caller must fail closed."""


#: Stray chat special tokens (``<|eot|>`` and kin) Glimmer sometimes
#: appends after otherwise-correct JSON. Stripped before parsing.
_SPECIAL_TOKEN_RE = re.compile(r"<\|.*?\|>")

#: Remembers whether the missing-parser warning was already logged so a
#: box-fill fallback does not spam the log once per image.
_SAM_PARSER_WARNED = False


def _warn_parser_missing_once() -> None:
    """Log the missing-``meta-sam-parser`` fallback once per process."""
    global _SAM_PARSER_WARNED
    if not _SAM_PARSER_WARNED:
        _SAM_PARSER_WARNED = True
        logger.warning(
            "meta-sam-parser not installed; falling back to box fill"
        )


def sam_parser_available() -> bool:
    """True when ``meta-sam-parser`` imports (logs once when missing)."""
    try:
        import meta_sam_parser  # noqa: F401
    except ImportError:
        _warn_parser_missing_once()
        return False
    return True


def parse_glimmer_json(content: object) -> object:
    """Parse one Glimmer vision reply tolerantly (shared helper).

    Strips ``<|...|>`` special tokens, then ``json.loads``; when that
    fails, retries on the outermost ``{...}`` slice so prose around the
    JSON still parses. Raises :class:`GlimmerError` only when no JSON
    object can be recovered.
    """
    if not isinstance(content, str) or not content.strip():
        raise GlimmerError("glimmer returned no content")
    text = _SPECIAL_TOKEN_RE.sub("", content).strip()
    if not text:
        raise GlimmerError("glimmer returned non-JSON content")
    try:
        parsed = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise GlimmerError(
                "glimmer returned non-JSON content"
            ) from None
        try:
            parsed = json.loads(text[start : end + 1])
        except ValueError as exc:
            raise GlimmerError(
                "glimmer returned non-JSON content"
            ) from exc
    return parsed


def _keep_alive() -> Any:
    """Ollama keep_alive: int seconds, duration string, or -1 (resident).

    Mirrors :mod:`judge` so the vision model stays resident instead of
    reloading on every screenshot (``SF_KEEP_ALIVE``, default ``-1``).
    """
    import os as _os

    raw = (_os.environ.get("SF_KEEP_ALIVE") or "-1").strip()
    if raw == "-1":
        return -1
    try:
        return int(raw)
    except ValueError:
        return raw


class GlimmerError(ImageRedactError):
    """The local Glimmer region lookup failed."""


class SamError(ImageRedactError):
    """The cloud SAM 3.1 segmentation failed."""


def is_image_file(name: str) -> bool:
    """True when the file name has a supported image suffix."""
    lower = (name or "").lower()
    return any(lower.endswith(suffix) for suffix in IMAGE_SUFFIXES)


@dataclass
class Region:
    """One sensitive region reported by Glimmer."""

    prompt: str = ""
    box: tuple[int, int, int, int] | None = None  # x1, y1, x2, y2 pixels
    text: str = ""  # text Glimmer read inside the region, if any


@dataclass
class Mask:
    """One redaction shape: a full-frame raster mask and/or a box."""

    box: tuple[int, int, int, int] | None = None
    raster: bytes | None = None  # row-major 0/1, width * height bytes
    width: int = 0
    height: int = 0


@dataclass
class RedactedImage:
    """Result of :func:`redact_image`."""

    png: bytes  # redacted PNG bytes (EXIF/metadata stripped)
    regions: list[Region] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)  # Tier 1 scans these
    method: str = "none"  # "sam" | "box" | "none"
    method_label: str = ""  # human-readable chip text, e.g. "Box fill"


def _clamp_box(box: Any, width: int, height: int) -> tuple[int, int, int, int] | None:
    """Normalise a ``[x1, y1, x2, y2]`` box to image bounds, or None."""
    try:
        x1, y1, x2, y2 = (int(round(float(v))) for v in box)
    except (TypeError, ValueError):
        return None
    x1, x2 = sorted((max(0, x1), min(width - 1, x2)))
    y1, y2 = sorted((max(0, y1), min(height - 1, y2)))
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2, y2)


def _open_image(data: bytes) -> Any:
    """Open image bytes for display (EXIF orientation applied)."""
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise ImageRedactError("image support unavailable: Pillow missing") from exc
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        raise ImageRedactError(f"cannot parse image: {type(exc).__name__}") from exc
    try:
        image = ImageOps.exif_transpose(image)
    except Exception:
        pass
    return image.convert("RGB")


def encode_png(image: Any) -> bytes:
    """Encode as PNG with no EXIF or metadata chunks."""
    from PIL import Image

    canvas = Image.new("RGB", image.size, (255, 255, 255))
    canvas.paste(image)
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def png_data_url(png: bytes) -> str:
    """Wrap PNG bytes as a ``data:`` URL (never logged)."""
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def thumbnail_png(png: bytes, max_width: int = 320) -> bytes:
    """Downscale redacted PNG bytes for the attachment chip."""
    from PIL import Image

    image = Image.open(io.BytesIO(png)).convert("RGB")
    if image.width > max_width:
        image = image.resize(
            (max_width, max(1, round(image.height * max_width / image.width))),
            Image.LANCZOS,
        )
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


_GLIMMER_PROMPT = """You check a screenshot or photo for sensitive regions \
that must be hidden before sharing: email addresses, names on badges, \
card or account numbers, passwords or API keys on a terminal screen, and \
other readable private text.

The image is {w}x{h} pixels. Reply with JSON only, exactly:
{"regions": [{"prompt": "<short noun phrase naming the thing>",
"box": [x1, y1, x2, y2], "text": "<text you read inside, or empty>"}]}
One entry per sensitive region; pixel coordinates in 0..{w} and 0..{h}.
Use a tight box around each region. Empty list when nothing is sensitive."""


def find_regions(
    image_bytes: bytes,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> tuple[list[Region], tuple[int, int]]:
    """Ask local Glimmer for the sensitive regions of one image.

    Returns ``(regions, (width, height))``. Raises :class:`GlimmerError`
    on any failure so the caller fails closed.
    """
    cfg = settings or get_settings()
    try:
        image = _open_image(image_bytes)
    except ImageRedactError as exc:
        raise GlimmerError(str(exc)) from exc
    width, height = image.size
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    body = {
        "model": GLIMMER_MODEL,
        "messages": [
            {
                "role": "user",
                "content": _GLIMMER_PROMPT.replace("{w}", str(width)).replace("{h}", str(height)),
                "images": [b64],
            }
        ],
        "format": "json",
        "stream": False,
        "think": False,
        "keep_alive": _keep_alive(),
        "options": {"num_ctx": 2048},
    }
    url = cfg.ollama_url.rstrip("/") + "/api/chat"
    try:
        with httpx.Client(
            transport=transport, timeout=cfg.image_timeout_s,
            verify=_verify_for(url),
        ) as client:
            response = client.post(url, json=body)
    except Exception as exc:
        raise GlimmerError(
            f"glimmer unreachable: {type(exc).__name__}"
        ) from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise GlimmerError("glimmer returned bad JSON") from exc
    if response.status_code != 200:
        raise GlimmerError(f"glimmer status {response.status_code}")
    message = payload.get("message", {}) if isinstance(payload, dict) else {}
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise GlimmerError("glimmer returned no content")
    parsed = parse_glimmer_json(content)
    items = parsed.get("regions", []) if isinstance(parsed, dict) else []
    if not isinstance(items, list):
        raise GlimmerError("glimmer returned a bad region list")
    regions: list[Region] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt", "") or "").strip()
        text = str(item.get("text", "") or "").strip()
        box = _clamp_box(item.get("box"), width, height)
        if not prompt and box is None and not text:
            continue
        regions.append(Region(prompt=prompt, box=box, text=text))
    return regions, (width, height)


def _object_prompt(regions: list[Region], width: int, height: int) -> str:
    """Build one box/point prompt covering every boxed region."""
    parts = []
    for index, region in enumerate(regions, start=1):
        assert region.box is not None
        x1, y1, x2, y2 = region.box
        parts.append(
            f"{index}<|box;x1={x1};y1={y1};x2={x2};y2={y2};w={width};h={height}|>"
        )
    return f"<0f>{','.join(parts)}"


def _sam_output_texts(payload: Any) -> str:
    """Collect the ``output_text`` lane texts of a Responses payload."""
    texts: list[str] = []
    if not isinstance(payload, dict):
        return ""
    output = payload.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)
            ):
                texts.append(part["text"])
    return "".join(texts)


def _wire_boxes(output_text: str) -> list[tuple[int, int, int, int]]:
    """Extract box records straight from the SAM wire grammar."""
    return [
        (int(x1), int(y1), int(x2), int(y2))
        for x1, y1, x2, y2 in _BOX_RE.findall(output_text)
    ]


def _parser_masks(output_text: str) -> list[Mask]:
    """Decode mask rasters via meta-sam-parser (lazy import).

    Raises ImportError when the package is missing and ValueError when
    the text holds no usable masks; the caller then falls back to boxes.
    """
    try:
        from meta_sam_parser import (
            CompletedOutcome,
            decode_mask_to_raster,
            image_segmentation_format,
        )
    except ImportError:
        _warn_parser_missing_once()
        raise
    format_ = image_segmentation_format()
    parser = format_.create_parser()
    for snapshot in parser.push(output_text):
        _ = snapshot
    finished = parser.finish(CompletedOutcome())
    result = finished.result
    boxes = _wire_boxes(output_text)
    masks: list[Mask] = []
    try:
        records = list(result.records)
    except AttributeError as exc:
        raise ValueError("parser returned no records") from exc
    mask_records = [r for r in records if getattr(r, "kind", "") == "mask"]
    for index, record in enumerate(mask_records):
        try:
            raster = bytes(decode_mask_to_raster(record.mask))
            width = int(record.mask.width)
            height = int(record.mask.height)
        except Exception:
            continue
        box = boxes[index] if index < len(boxes) else None
        masks.append(Mask(box=box, raster=raster, width=width, height=height))
    if not masks:
        raise ValueError("parser decoded no masks")
    return masks


def _sam_request(
    image_bytes: bytes,
    prompt: str,
    mime: str,
    *,
    settings: Settings,
    transport: httpx.BaseTransport | None,
) -> str:
    """One SAM 3.1 Responses call; returns the ``output_text`` lane text."""
    if not settings.meta_available:
        raise SamError("meta upstream unavailable")
    data_url = (
        f"data:{mime};base64," + base64.b64encode(image_bytes).decode("ascii")
    )
    url = settings.meta_base_url.rstrip("/") + SAM_RESPONSES_PATH
    body = {
        "model": SAM_MODEL,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": data_url},
                ],
            }
        ],
    }
    headers = {"Authorization": "Bearer " + settings.meta_api_key}
    try:
        with httpx.Client(
            transport=transport, timeout=SAM_TIMEOUT_S,
            verify=_verify_for(url),
        ) as client:
            response = client.post(url, json=body, headers=headers)
    except Exception as exc:
        raise SamError(f"sam unreachable: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise SamError(f"sam status {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise SamError("sam returned bad JSON") from exc
    return _sam_output_texts(payload)


def segment(
    image_bytes: bytes,
    regions: list[Region],
    size: tuple[int, int],
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[Mask]:
    """Turn Glimmer regions into exact SAM 3.1 masks.

    Boxed regions share one object-prompt request; prompt-only regions
    get one concept request each (the API segments a single concept per
    request). Masks come from ``meta-sam-parser``; every wire box is kept
    alongside its mask so callers can still fill boxes when a raster is
    unusable. Raises :class:`SamError` when nothing usable comes back.
    """
    cfg = settings or get_settings()
    width, height = size
    boxed = [r for r in regions if r.box is not None]
    prompted = [r for r in regions if r.box is None and r.prompt]
    if not boxed and not prompted:
        raise SamError("no segmentable regions")
    try:
        from PIL import Image
    except ImportError as exc:
        raise SamError("image support unavailable: Pillow missing") from exc
    try:
        probe = Image.open(io.BytesIO(image_bytes))
        mime = Image.MIME.get(probe.format or "PNG", "image/png")
    except Exception:
        mime = "image/png"
    output_texts: list[str] = []
    if boxed:
        output_texts.append(
            _sam_request(
                image_bytes, _object_prompt(boxed, width, height), mime,
                settings=cfg, transport=transport,
            )
        )
    for region in prompted:
        output_texts.append(
            _sam_request(
                image_bytes, region.prompt.strip(), mime,
                settings=cfg, transport=transport,
            )
        )
    masks: list[Mask] = []
    for output_text in output_texts:
        try:
            masks.extend(_parser_masks(output_text))
            continue
        except (ImportError, ValueError):
            pass
        for box in _wire_boxes(output_text):
            masks.append(Mask(box=box))
    # Regions SAM missed entirely still fail closed on their Glimmer box.
    if len(masks) < len(boxed):
        have = {
            m.box for m in masks
            if m.box is not None
        }
        for region in boxed:
            if region.box not in have:
                masks.append(Mask(box=region.box))
    if not masks:
        raise SamError("sam returned no masks or boxes")
    return masks


def apply_masks(
    image_bytes: bytes,
    masks: list[Mask],
    size: tuple[int, int] | None = None,
) -> bytes:
    """Fill every mask/box solid black; return stripped PNG bytes."""
    from PIL import Image, ImageDraw

    image = _open_image(image_bytes)
    overlay = Image.new("RGB", image.size, (0, 0, 0))
    width, height = image.size
    for mask in masks:
        raster, box = mask.raster, mask.box
        if raster and mask.width > 0 and mask.height > 0:
            try:
                hole = Image.new("L", (mask.width, mask.height))
                hole.putdata([255 if v else 0 for v in raster])
                if (mask.width, mask.height) != image.size:
                    hole = hole.resize(image.size, Image.NEAREST)
                current = image.copy()
                current.paste(overlay, (0, 0), hole)
                image = current
                continue
            except Exception:
                pass
        if box is not None:
            x1, y1, x2, y2 = box
            x1 = max(0, min(x1, width - 1))
            y1 = max(0, min(y1, height - 1))
            x2 = max(0, min(x2, width - 1))
            y2 = max(0, min(y2, height - 1))
            if x2 >= x1 and y2 >= y1:
                ImageDraw.Draw(image).rectangle([x1, y1, x2, y2], fill=(0, 0, 0))
    _ = size  # accepted for callers that track the SAM frame size
    return encode_png(image)


def describe_method(method: str, *, parser_missing: bool = False) -> str:
    """Human-readable chip text for a redaction method.

    ``"Box fill (SAM parser not installed)"`` when boxes were used
    because ``meta-sam-parser`` is missing; ``"Box fill"`` for plain
    local boxes; ``"SAM masks"`` for exact masks; ``""`` when nothing
    was hidden.
    """
    if method == "sam":
        return "SAM masks"
    if method == "box":
        if parser_missing:
            return "Box fill (SAM parser not installed)"
        return "Box fill"
    return ""


def redact_image(
    image_bytes: bytes,
    *,
    settings: Settings | None = None,
    glimmer_transport: httpx.BaseTransport | None = None,
    sam_transport: httpx.BaseTransport | None = None,
    sam_enabled: bool = True,
) -> RedactedImage:
    """Full pipeline: Glimmer regions, SAM masks, black fill.

    ``sam_enabled=False`` (the "keep images local" choice) skips the
    cloud call and fills Glimmer boxes directly. Any SAM failure also
    falls back to boxes; when a found region has neither a mask nor a
    box, raises :class:`ImageRedactError` so the original is never sent.
    """
    cfg = settings or get_settings()
    regions, size = find_regions(
        image_bytes, settings=cfg, transport=glimmer_transport
    )
    if not regions:
        image = _open_image(image_bytes)
        return RedactedImage(
            png=encode_png(image), method="none",
            method_label=describe_method("none"),
        )
    texts = [r.text for r in regions if r.text]
    masks: list[Mask] = []
    method = "box"
    if sam_enabled and cfg.meta_available:
        try:
            masks = segment(
                image_bytes, regions, size,
                settings=cfg, transport=sam_transport,
            )
            if any(m.raster for m in masks):
                method = "sam"
        except SamError:
            masks = []
    if not masks:
        masks = [Mask(box=r.box) for r in regions if r.box is not None]
        if len(masks) < len(regions):
            raise ImageRedactError(
                "region found but not localizable; refusing to send original"
            )
    parser_missing = method == "box" and not sam_parser_available()
    return RedactedImage(
        png=apply_masks(image_bytes, masks, size),
        regions=regions, texts=texts, method=method,
        method_label=describe_method(method, parser_missing=parser_missing),
    )
