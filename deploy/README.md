# Hosting Shadowfenced as a public demo

The hosted demo runs the same app with `SF_HOSTED=1`: it shows the
"Public demo. Use made-up data only." banner, keeps custom rules in
memory only (capped at 20), caps uploads at 1 MB, rate-limits requests
per client IP, and judges Tier 2 with Muse Spark when no local Ollama
is reachable. No GPU is needed. Hosting does not depend on any single
provider: Modal and Hugging Face below are two interchangeable
options, and the app itself runs anywhere a Dockerfile runs.

## Option A: Modal

Prerequisites: a Modal account and the `modal` CLI (`pip install modal`;
`modal setup`).

1. Store the Meta key in a Modal secret named `shadowfenced-meta`:
   `modal secret create shadowfenced-meta META_API_KEY=...`
   (any other settings from `.env.example` can go in the same secret).
2. Deploy from the repo root:
   `modal deploy deploy/modal_app.py`
3. Modal prints the public URL. `SF_HOSTED=1` is already set by
   `deploy/modal_app.py`, so the deployment is the public demo as-is.

## Option B: Hugging Face Docker Space

1. Create a new Space, pick the **Docker** SDK, and push this repo's
   files (the Space needs the `Dockerfile` at its root).
2. In the Space settings, add a repository secret `META_API_KEY` with
   the Meta key, plus a variable `SF_HOSTED=1`.
3. The Space builds the same `Dockerfile` (uvicorn on port 8765 bound
   to 0.0.0.0 inside the container).

Neither option is required to run locally: local use stays
`docker compose up --build` (or `uvicorn` directly) with `SF_HOSTED`
unset, which keeps the previous local behaviour unchanged.
