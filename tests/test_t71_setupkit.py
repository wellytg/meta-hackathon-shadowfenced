"""T71 setup kit: doctor profiles, try_cases logic, setup/ key-shape guard.

``setup/check.py`` and ``setup/try_cases.py`` are stdlib-only scripts
loaded here from their paths. Probe and HTTP layers are mocked: no
test here touches Ollama, the GPU, or the network.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SETUP / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load("check")
try_cases = _load("try_cases")

GLIMMER = "muse-glimmer:30b"


# --- doctor profile choice ----------------------------------------------------


def test_full_when_glimmer_pulled():
    verdict = check.choose_profile(True, [GLIMMER, "llama3.2:3b"], 24.0)
    assert verdict["profile"] == "full"
    assert verdict["judge_model"] == GLIMMER
    assert verdict["pull_hint"] == ""


def test_full_warns_on_small_vram_but_stays_full():
    verdict = check.choose_profile(True, [GLIMMER], 6.0)
    assert verdict["profile"] == "full"
    assert "6.0 GiB VRAM" in verdict["degraded"][0]


def test_full_no_warning_on_12gb_class_card():
    verdict = check.choose_profile(True, [GLIMMER], 11.9)
    assert verdict["profile"] == "full"
    assert "VRAM" not in verdict["degraded"][0]


def test_light_prefers_3b_over_1b():
    verdict = check.choose_profile(True, ["llama3.2:1b", "llama3.2:3b"], None)
    assert verdict["profile"] == "light"
    assert verdict["judge_model"] == "llama3.2:3b"


def test_light_falls_back_to_1b():
    verdict = check.choose_profile(True, ["llama3.2:1b"], None)
    assert verdict["profile"] == "light"
    assert verdict["judge_model"] == "llama3.2:1b"


def test_ollama_without_models_is_light_with_pull_hint():
    verdict = check.choose_profile(True, [], None)
    assert verdict["profile"] == "light"
    assert verdict["judge_model"] == "llama3.2:3b"
    assert verdict["pull_hint"] == "ollama pull llama3.2:3b"


def test_no_ollama_is_code_only():
    verdict = check.choose_profile(False, [], None)
    assert verdict["profile"] == "code"
    assert verdict["judge_model"] == ""
    assert any("HOLD" in note for note in verdict["degraded"])


def test_collect_uses_mocked_probes(monkeypatch):
    monkeypatch.setattr(
        check, "ollama_status",
        lambda base, timeout=3.0: {
            "reachable": True, "detail": "up",
            "models": [GLIMMER], "pulled_all": [GLIMMER],
        },
    )
    monkeypatch.setattr(
        check, "gpu_info",
        lambda: {"present": True, "detail": "Card (24.0 GiB)",
                 "count": 1, "max_vram_gb": 24.0},
    )
    monkeypatch.setattr(check, "free_ram_gb", lambda: 32.0)
    monkeypatch.setattr(
        check, "meta_key_present", lambda: {"present": False, "source": "none"}
    )
    monkeypatch.setattr(
        check, "package_installed",
        lambda: {"installed": True, "detail": "ok"},
    )
    report = check.collect("http://ollama.example")
    assert report["verdict"]["profile"] == "full"
    assert report["ollama"]["url"] == "http://ollama.example"
    assert report["ram_free_gb"] == 32.0
    text = check.render_text(report)
    assert "Verdict: FULL" in text
    assert "setup" in text and "start" in text


def test_doctor_json_round_trip_with_mocked_probes(monkeypatch, capsys):
    monkeypatch.setattr(
        check, "ollama_status",
        lambda base, timeout=3.0: {
            "reachable": False, "detail": "down",
            "models": [], "pulled_all": [],
        },
    )
    monkeypatch.setattr(
        check, "gpu_info", lambda: {"present": False, "detail": "x"}
    )
    monkeypatch.setattr(check, "free_ram_gb", lambda: None)
    monkeypatch.setattr(
        check, "meta_key_present", lambda: {"present": False, "source": "none"}
    )
    monkeypatch.setattr(
        check, "package_installed",
        lambda: {"installed": False, "detail": "missing"},
    )
    assert check.main(["--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["verdict"]["profile"] == "code"
    assert set(report) == {
        "python", "ram_free_gb", "gpu", "ollama",
        "meta_key", "package", "verdict",
    }


def test_doctor_never_prints_key_values(monkeypatch, capsys):
    sentinel = "SENTINEL-DOCTOR-KEY-4f8e2c9a1b7d"
    monkeypatch.setenv("META_API_KEY", sentinel)
    monkeypatch.setattr(
        check, "ollama_status",
        lambda base, timeout=3.0: {
            "reachable": False, "detail": "down",
            "models": [], "pulled_all": [],
        },
    )
    monkeypatch.setattr(
        check, "gpu_info", lambda: {"present": False, "detail": "x"}
    )
    monkeypatch.setattr(check, "free_ram_gb", lambda: None)
    monkeypatch.setattr(
        check, "package_installed",
        lambda: {"installed": True, "detail": "ok"},
    )
    assert check.meta_key_present() == {"present": True, "source": "env"}
    assert check.main([]) == 0
    assert sentinel not in capsys.readouterr().out
    assert check.main(["--json"]) == 0
    assert sentinel not in capsys.readouterr().out


# --- try_cases table/expectation logic (mock HTTP) ----------------------------


def test_build_cases_ten_with_expectations():
    cases, secrets = try_cases.build_cases()
    assert [c["id"] for c in cases] == [
        "key-plain", "key-base64", "email", "identity-bundle", "pointer",
        "welcome", "bank-row", "contract", "harmless", "person-search",
    ]
    assert {c["expected"] for c in cases} == {
        "BLOCK", "REDACT", "HOLD", "WARN", "ALLOW",
    }
    assert all(c["why"] for c in cases)
    # Minted shapes stay runtime-only: deterministic across calls.
    again, _ = try_cases.build_cases()
    assert [c["text"] for c in cases] == [c["text"] for c in again]
    assert len(secrets) == 2


def _all_expected_sender(**overrides):
    cases, _ = try_cases.build_cases()
    decisions = {c["id"]: c["expected"] for c in cases}
    decisions.update(overrides)

    def fake(base_url: str, text: str, provider: str | None):
        for case in cases:
            if case["text"] == text:
                decision = decisions[case["id"]]
                return {"decision": decision, "sent_text": None}
        raise AssertionError("unknown case text")

    return fake


def test_all_match_when_app_answers_expectations():
    report = try_cases.run_all("http://app.example", sender=_all_expected_sender())
    assert report["matched"] == 10
    assert report["total"] == 10
    assert all(row["match"] for row in report["rows"])
    table = try_cases.render_table(report)
    assert "10/10 matched" in table
    assert "key-plain" in table and "person-search" in table


def test_mismatch_detected_and_reported():
    report = try_cases.run_all(
        "http://app.example", sender=_all_expected_sender(email="ALLOW")
    )
    assert report["matched"] == 9
    row = next(r for r in report["rows"] if r["id"] == "email")
    assert row["expected"] == "REDACT"
    assert row["got"] == "ALLOW"
    assert row["match"] is False
    assert "9/10 matched" in try_cases.render_table(report)


def test_provider_is_forwarded_to_sender():
    seen: list = []

    def fake(base_url: str, text: str, provider: str | None):
        seen.append(provider)
        return {"decision": "ALLOW", "sent_text": text}

    try_cases.run_all("http://app.example", provider="meta", sender=fake)
    assert seen == ["meta"] * 10


def test_error_rows_after_retry(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    calls: list = []

    def failing(base_url: str, text: str, provider: str | None):
        calls.append(text)
        raise ConnectionError("down")

    cases, secrets = try_cases.build_cases()
    row = try_cases.run_case("http://app.example", cases[0], secrets, None, failing)
    assert row["match"] is False
    assert row["got"].startswith("ERROR")
    assert row["sent"] == "(nothing sent)"
    assert len(calls) == 2  # one retry


def test_mask_hides_minted_secrets():
    cases, secrets = try_cases.build_cases()
    assert len(secrets) == 2
    for value, label in secrets.items():
        assert try_cases.mask(f"got {value} here", secrets) == f"got {label} here"
    key_text = cases[0]["text"]
    assert isinstance(key_text, str)
    assert "[cloud-key]" in try_cases.mask(key_text, secrets)
    for value in secrets:
        assert value not in try_cases.mask(key_text, secrets)


def test_sent_text_masked_in_rows():
    cases, secrets = try_cases.build_cases()
    key = next(iter(secrets))

    def leaky(base_url: str, text: str, provider: str | None):
        return {"decision": "REDACT", "sent_text": f"echo {key} done"}

    row = try_cases.run_case(
        "http://app.example", cases[2], secrets, None, leaky
    )
    assert key not in row["sent"]
    assert "[cloud-key]" in row["sent"]


def test_main_exit_codes_with_mock_sender(monkeypatch, capsys):
    monkeypatch.setattr(try_cases, "post_send", _all_expected_sender())
    assert try_cases.main(["--url", "http://app.example"]) == 0
    assert "10/10 matched" in capsys.readouterr().out
    monkeypatch.setattr(
        try_cases, "post_send", _all_expected_sender(harmless="HOLD")
    )
    assert try_cases.main(["--url", "http://app.example", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["matched"] == 9


def test_main_local_flag_selects_loopback(monkeypatch, capsys):
    seen: list = []

    def fake(base_url: str, text: str, provider: str | None):
        seen.append(base_url)
        return {"decision": "ALLOW", "sent_text": text}

    monkeypatch.setattr(try_cases, "post_send", fake)
    try_cases.main(["--local"])
    assert seen == ["http://127.0.0.1:8765"] * 10
    capsys.readouterr()


# --- guard: no key shapes under setup/ ----------------------------------------


def _key_patterns() -> list[tuple[str, re.Pattern[str]]]:
    # Fragments are joined at runtime; no full prefix appears literally here.
    cloud_key = "AK" + "IA" + r"[0-9A-Z]{16}(?![0-9A-Z])"
    dev_token = "gh" + "p_" + r"[A-Za-z0-9]{36}(?![A-Za-z0-9])"
    live_secret = "s" + "k-" + r"[A-Za-z0-9]{20,}(?![A-Za-z0-9])"
    bot_token = "xox" + "b-" + r"[A-Za-z0-9-]{10,}"
    return [
        ("cloud_key", re.compile(cloud_key)),
        ("dev_token", re.compile(dev_token)),
        ("live_secret", re.compile(live_secret)),
        ("bot_token", re.compile(bot_token)),
    ]


def test_no_key_shapes_under_setup():
    patterns = _key_patterns()
    hits: list[str] = []
    for path in sorted(SETUP.rglob("*")):
        if not path.is_file():
            continue
        try:
            lines = path.read_bytes().decode("utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(lines, start=1):
            for kind, pattern in patterns:
                if pattern.search(line):
                    hits.append(f"{path.name}:{lineno} looks like {kind}")
    assert hits == [], f"key shapes under setup/: {hits}"


# --- start scripts: an early server exit must fail loudly ----------------------

START_PS1 = SETUP / "start.ps1"
START_SH = SETUP / "start.sh"
CRASH_SENTINEL = "stub-uvicorn-boom"
CRASH_MARKER = "it did not start"


def _stub_env(tmp_path, exit_code):
    """Env where ``python -m uvicorn`` fails fast with a loud error."""
    pkg = tmp_path / "stub" / "uvicorn"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__main__.py").write_text(
        textwrap.dedent(
            f"""\
            import sys
            sys.stderr.write("{CRASH_SENTINEL}: failing on purpose\\n")
            raise SystemExit({exit_code})
            """
        ),
        encoding="utf-8",
    )
    env = dict(os.environ)
    old = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(tmp_path / "stub") + (os.pathsep + old if old else "")
    return env


def _bash():
    exe = shutil.which("bash")
    if exe is None:  # pragma: no cover - depends on the box
        pytest.skip("bash not available")
    return exe


def _powershell():
    for name in ("pwsh", "powershell"):
        exe = shutil.which(name)
        if exe is not None:
            return exe
    pytest.skip("no PowerShell available")  # pragma: no cover - depends on box


def test_start_sh_reports_early_crash(tmp_path):
    proc = subprocess.run(
        [_bash(), START_SH.as_posix(), "code"],
        cwd=ROOT,
        env=_stub_env(tmp_path, 3),
        capture_output=True, text=True, timeout=120,
    )
    combined = proc.stdout + proc.stderr
    assert "profile: code (your choice)" in combined
    assert "Starting Shadowfenced on http://127.0.0.1:8765" in combined
    assert CRASH_SENTINEL in combined  # the server's error output is shown
    assert CRASH_MARKER in combined
    assert proc.returncode == 3


def test_start_sh_quick_clean_exit_is_still_failure(tmp_path):
    proc = subprocess.run(
        [_bash(), START_SH.as_posix(), "code"],
        cwd=ROOT,
        env=_stub_env(tmp_path, 0),
        capture_output=True, text=True, timeout=120,
    )
    assert CRASH_MARKER in proc.stdout + proc.stderr
    assert proc.returncode != 0


def test_start_ps1_reports_early_crash(tmp_path):
    proc = subprocess.run(
        [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(START_PS1), "-Profile", "code"],
        cwd=ROOT,
        env=_stub_env(tmp_path, 3),
        capture_output=True, text=True, timeout=180,
    )
    combined = proc.stdout + proc.stderr
    assert "profile: code (your choice)" in combined
    assert "Starting Shadowfenced on http://127.0.0.1:8765" in combined
    assert CRASH_SENTINEL in combined  # the server's error output is shown
    assert CRASH_MARKER in combined
    assert proc.returncode == 3


def test_start_ps1_quick_clean_exit_is_still_failure(tmp_path):
    proc = subprocess.run(
        [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(START_PS1), "-Profile", "code"],
        cwd=ROOT,
        env=_stub_env(tmp_path, 0),
        capture_output=True, text=True, timeout=180,
    )
    assert CRASH_MARKER in proc.stdout + proc.stderr
    assert proc.returncode != 0
