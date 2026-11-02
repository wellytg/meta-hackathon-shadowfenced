"""Serve Shadowfenced on Modal as a hosted public demo.

The same ASGI app runs with ``SF_HOSTED=1`` (in-memory rules capped at
20, 1 MB uploads, per-IP rate limiting, the public-demo banner, and the
Muse Spark Tier 2 fallback when no local Ollama is reachable). The Meta
key comes from a Modal secret named ``shadowfenced-meta`` holding
``META_API_KEY``; nothing provider-specific is baked into the image.

Deploy (see deploy/README.md)::

    modal deploy deploy/modal_app.py
"""

from __future__ import annotations

import os

import modal

app = modal.App("shadowfenced")

image = modal.Image.from_dockerfile("Dockerfile")


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("shadowfenced-meta")],
)
@modal.asgi_app()
def asgi():
    """Return the Shadowfenced ASGI app in hosted demo mode."""
    os.environ.setdefault("SF_HOSTED", "1")
    os.environ.setdefault("SF_LOG_PATH", "/tmp/shadowfenced/decisions.jsonl")
    from shadowfenced.app import app as web_app

    return web_app
