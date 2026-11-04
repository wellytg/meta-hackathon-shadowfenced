# Beyond the hackathon

Shadowfenced was built for the Meta Global AI Developer Hackathon. The problem
is real: people want to use AI without leaking what they work on.

## Small local judges for machines without a GPU

Most PCs have no 12 GB GPU. The Nov 4 experiment trained Llama 3.2 3B (QLoRA)
as a judge. On the sealed set it caught 23/26 sensitive and flagged 2/14
harmless at 3.5 s. Glimmer caught 25/26 and flagged 13/14 at 7.3 s (README).

Next: quantized 1B–3B judges (4-bit GGUF via Ollama). CPU-only latency targets.
More near-miss training rows for its 3 misses. All measured inside the full
pipeline, not judge-alone. Method: `docs/FINETUNE.md`.

## How well do local models protect their own host?

A test plan, not yet run. Which local models resist prompt injection aimed at
the judge. Leak rate vs size and quantization. CPU vs GPU latency. A public
benchmark built from the synthetic sets.

## Hardening before real use

See `docs/security/`. Fixes from the Nov 4 review. DOCX parts the file guard
misses. CSRF on the local console. Extension answer restore.

## More places

More AI sites in the extension. IDE agents through the proxy. A
store-published extension.

## Community

Issues are welcome. Good first issues are marked. Decisions are logged by
fingerprint, never by text. See the decision log in the README.
