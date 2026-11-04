# Security policy

## Use at your own risk

Shadowfenced is a hackathon build. It has been tested: 1,100+ tests, a sealed
evaluation set, a red-team test folder, and an independent review on Nov 4. It
has not been audited.

Most of the code was written by an AI coding agent (Muse Code). It worked from
task briefs that were planned with AI and steered by a person. Briefs and code
can contain mistakes.

Do not rely on it as your only control for real secrets or regulated data. It
is provided as is, under the repo LICENSE.

## Supported version

`main` and release `v1.0-hackathon`. Older states get no fixes.

## How to report a vulnerability

Open a GitHub security advisory (private report) on the repo. Do not post
working exploits in public issues.

Include:

- What is affected (app, proxy, extension, and version or commit).
- Steps to reproduce, with synthetic data only.
- What an attacker gains.
- What you expected instead.

We will confirm receipt, assess the report, and fix or document it on `main`.

## Browser extension notes

The extension talks only to the local app on 127.0.0.1:8765. It uses a pairing
token. It is loaded unpacked. It has not been store-reviewed.

Page scripts on an AI site can read what you type before the extension acts.
This is a limit of any in-page guard. Review the code before loading it. See
`extension/README.md`.

Any other extension with access to an AI chat site can read what you type
on that page before Shadowfenced acts, as can built-in browser assistants
such as Gemini in Chrome or Copilot in Edge. Shadowfenced cannot block that
today. Check Chrome/Edge extension details → Site access to see what can
read each site.

## More detail

See `docs/security/` for the threat model, the red-team results, and the Nov 4
review. Some files land in parallel branches, so link the folder.
