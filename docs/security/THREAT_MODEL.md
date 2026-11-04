# Shadowfenced threat model (hackathon build)

Shadowfenced sits between a person and an AI. It scans outgoing text on this PC.
It blocks secrets, redacts personal data, and holds unclear cases for a human.
Clean or redacted text goes to the AI. Answers come back and get restored locally.

This is a hackathon build. It stops accidents and casual tricks. It is not a
certified data-loss-prevention product. Read this file before trusting it.

## 1. Assets: what we protect

Secrets come first. API keys, passwords, tokens, and planted canary traps.
A leaked key gives access to systems and money. Keys are never sent, even encoded.

Personal data comes second. Names, emails, phones, addresses, birth dates.
ID numbers, bank details, and card numbers. One detail alone may be fine.
Together they identify a person. The gate redacts single details and blocks
full identity bundles.

Business text comes third. Project codenames, client names, money figures.
Contract clauses. These leak plans and deals. The gate redacts them by policy.

Hidden assets matter too. The placeholder map links `[PII_EMAIL_1]` to the real
value. It lives in server memory only, for 15 minutes. The decision log holds
fingerprints and counts, never values. Stored provider keys live in the OS
credential store, never in the repo.

## 2. Trust boundaries

The browser page is untrusted. Any site can show hostile text. It can try to
steal data through what the user copies or sends.

The extension is semi-trusted. It runs inside the page. It reads page text to
check it. A hostile page can lie to it or watch what it highlights. The
extension talks to the local app with a pairing token.

The local app is trusted. It runs on 127.0.0.1:8765 on this PC. It sees plain
text. It holds the placeholder maps. Anyone who can reach it can read gated
text. It binds to loopback only, so only this PC can reach it.

Ollama is trusted local compute. It runs on 127.0.0.1:11434. The judge model
(Muse Glimmer) reads short text windows. It never stores them. If Ollama is
absent, judging is skipped and unclear cases stay held.

The Meta Model API and other upstreams are untrusted. They are curious readers.
They see only clean or redacted text. They must never see real keys or raw
personal data. Their answers are untrusted input too. An answer can carry a
trap, such as a placeholder pasted inside a hostile URL.

The hosted demo is public. It runs the same app with `SF_HOSTED=1`. It has no
local judge. It judges with Muse Spark over the network. Custom rules stay in
memory only. Suggestions and learning are off. It must only ever see made-up
data. The banner says so.

## 3. Attackers

The curious cloud model reads everything it is sent. It may remember it. It may
be trained on it later. It may echo placeholders into traps. We send it
placeholders, never values. We cannot undo what was already sent.

The prompt-injected page shows text that begs to be sent. "Paste this error to
support." The error hides a key. "Ignore previous instructions." The gate reads
the text as data, not orders. The judge prompt quotes user text as data too.

The malicious file hides secrets where scanners do not look. In the file name.
In document metadata. In headers. In encodings. The file guard scans the text
it can extract. It does not scan names or metadata today. Unknown files stay
blocked, never passed.

The hijacked coding agent talks through the proxy at `/v1/chat/completions`.
Every role is gated: system, user, assistant, tool. Tool-call arguments are
gated too. But each message is gated alone. A key split across two messages is
not seen as one key. That is a known gap.

Another local process is out of scope. It runs as the same user. It can read
the same memory and files. It can call the loopback app. It can read the token
file. We do not defend against it. Full-disk encryption and OS users are the
defense.

The network observer sees TLS traffic only. Upstream calls use HTTPS. Loopback
calls stay on this PC. We do not pin certificates beyond default TLS checks.

## 4. What each tier stops, and what it does not

Tier 1 is code detectors. Patterns, canaries, entropy, policy words. It proves
findings. Nothing else can prove one. It catches plain keys, encoded keys
(base64, base64url, hex, URL, and one nesting level), emails, phones, names,
addresses, bank details, beacons, and custom rules. It does not normalize
Unicode. Zero-width characters and look-alike letters split a key or email and
slip past. It does not decode base32, gzip, or hex with separators. It does not
join text across messages.

Re-identification scoring counts quasi-identifiers. Name plus birth date plus
address is a person, even when each part looks harmless. It warns or holds on
risky sets. It blocks full identity bundles. It does not understand meaning.
It can miss paraphrased details. It exempts public figures and stories, and an
attacker can abuse those exemptions.

Tier 2 is the judge. Local Muse Glimmer reads a ~300-character window around an
unclear span. It answers with a small fixed schema. On the hosted demo, Muse
Spark judges instead when Ollama is missing. The judge only advises. Text aimed
at the judge ("classify this as benign", "ignore previous instructions") never
releases a hold alone. A human must rule. The judge can be wrong. That is why
Tier 3 exists.

Tier 3 is code verification. The model proposes, code disposes. A claimed span
must appear word-for-word in the text and match a code check. Else the case
stays held. Verification cannot bless what code cannot see.

Holds fail closed. Judge timeout, bad JSON, schema violation, unexpected error,
unscannable file: all hold. Nothing is sent until a human rules. Approve sends
the redacted text only. Proven secrets stay redacted even on approve.

Keyguard spots the org's own stored keys without keeping them. Only salted
hashes stay in memory. Values are re-read briefly per gate call, then dropped.
It catches the keys in common encodings. It does not catch a key the org never
registered, or one split across messages.

File guard stops uncleared files. Anything it cannot read stays blocked. It
redacts text copies that always re-scan clean. It never sends the original
anywhere. It does not scan file names or document metadata today. A secret in a
name passes the file scan (the send-time gate still sees the name when the file
is attached through `/api/send`).

Rehydration restores values into answers locally. It restores inside URLs too.
A model answer that puts `[PII_EMAIL_1]` inside a link gives a clickable URL
with the real value after restore. That is a known gap. The console renderer
does not make links clickable, which limits the damage there. API answers carry
the live URL.

## 5. Non-goals for this build

No defense against malware on this PC. No defense against a hostile browser
extension with more rights. No redaction inside images beyond the box-fill flow.
No watermarking of sent text. No per-user access control on the loopback app.
Anyone on this PC can call it.

## 6. Residual risks, ranked

1. Split secrets across messages or turns reach the model in parts.
2. Unicode tricks (zero-width, homoglyphs) dodge Tier 1 patterns.
3. Rehydration plants real values inside model-chosen URLs.
4. File names and document metadata are not scanned.
5. Rare encodings (base32, gzip blobs, spaced hex) dodge the decoders.
6. Judge exemptions (public figure, story framing) can be faked.
7. The hosted demo judges over the network, so unclear text windows cross the
   boundary by design there.

Fix 1 to 5 before real-world use. See `REDTEAM_RESULTS.md` for the tests behind
each claim.
