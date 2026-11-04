"""Shadowfenced doctor: check this machine, recommend a setup profile.

Stdlib only, Windows/macOS/Linux. Checks the Python version, free RAM,
NVIDIA GPU + VRAM (via ``nvidia-smi`` when present), whether Ollama is
reachable and which judge models are pulled, whether a Meta key is set
or stored (yes/no only, the value is never read into output), and
whether the ``shadowfenced`` package is installed. Prints a verdict:
the recommended profile, the exact start command, and what is degraded.

Exit 0 always, unless Python itself is too old (exit 1). ``--json``
prints the same report as JSON for agents.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

MIN_PYTHON = (3, 11)

GLIMMER_MODEL = "muse-glimmer:30b"
LIGHT_MODELS = ("llama3.2:3b", "llama3.2:1b")
WATCH_MODELS = (GLIMMER_MODEL,) + LIGHT_MODELS

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

REPO_ROOT = Path(__file__).resolve().parents[1]


def python_ok() -> tuple[bool, str]:
    """True + version string when this interpreter is new enough."""
    version = ".".join(str(part) for part in sys.version_info[:3])
    return sys.version_info[:2] >= MIN_PYTHON, version


def free_ram_gb() -> float | None:
    """Free RAM in GiB, or None when it cannot be measured."""
    try:
        if sys.platform == "win32":
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_phys", ctypes.c_ulonglong),
                    ("avail_phys", ctypes.c_ulonglong),
                    ("total_page", ctypes.c_ulonglong),
                    ("avail_page", ctypes.c_ulonglong),
                    ("total_virt", ctypes.c_ulonglong),
                    ("avail_virt", ctypes.c_ulonglong),
                    ("avail_virt_ext", ctypes.c_ulonglong),
                ]

            status = _Status()
            status.length = ctypes.sizeof(_Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return round(status.avail_phys / (1024**3), 1)
            return None
        if sys.platform == "darwin":
            out = subprocess.run(
                ["vm_stat"], capture_output=True, text=True, timeout=10
            )
            if out.returncode != 0:
                return None
            pages = {}
            for line in out.stdout.splitlines():
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                digits = "".join(ch for ch in value if ch.isdigit())
                if digits:
                    pages[key.strip()] = int(digits)
            free_pages = (
                pages.get("Pages free", 0)
                + pages.get("Pages inactive", 0)
                + pages.get("Pages speculative", 0)
            )
            page_size = 16384
            try:
                sysctl = subprocess.run(
                    ["sysctl", "-n", "hw.pagesize"],
                    capture_output=True, text=True, timeout=10,
                )
                page_size = int(sysctl.stdout.strip() or page_size)
            except (ValueError, OSError):
                pass
            return round(free_pages * page_size / (1024**3), 1)
        meminfo = Path("/proc/meminfo")
        if meminfo.is_file():
            for line in meminfo.read_text(encoding="utf-8").splitlines():
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    return round(kb / (1024**2), 1)
    except (OSError, ValueError):
        pass
    return None


def gpu_info() -> dict[str, object]:
    """NVIDIA GPUs found, or ``{"present": False}`` with a plain reason."""
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return {"present": False, "detail": "no NVIDIA GPU found"}
    if proc.returncode != 0:
        return {"present": False, "detail": "no NVIDIA GPU found"}
    names: list[str] = []
    vram: list[float] = []
    for line in proc.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 2:
            continue
        names.append(parts[0])
        digits = "".join(ch for ch in parts[1] if ch.isdigit())
        vram.append(round(int(digits or 0) / 1024, 1))
    if not names:
        return {"present": False, "detail": "no NVIDIA GPU found"}
    return {
        "present": True,
        "detail": "; ".join(f"{name} ({size} GiB)" for name, size in zip(names, vram)),
        "count": len(names),
        "max_vram_gb": max(vram),
    }


def _http_get_json(url: str, timeout: float) -> object:
    """GET one URL and parse the JSON body (raises on any failure)."""
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise OSError(f"status {response.status}")
        return json.loads(response.read().decode("utf-8"))


def ollama_status(base_url: str, timeout: float = 3.0) -> dict[str, object]:
    """Is Ollama reachable, and which watched models are pulled?"""
    base = base_url.rstrip("/")
    try:
        _http_get_json(base + "/api/version", timeout)
    except Exception as exc:
        return {
            "reachable": False,
            "detail": f"Ollama unreachable at {base} ({type(exc).__name__})",
            "models": [],
            "pulled_all": [],
        }
    try:
        payload = _http_get_json(base + "/api/tags", timeout)
        models = [
            entry.get("name", "")
            for entry in (payload.get("models", []) or [])
            if isinstance(entry, dict)
        ]
    except Exception:
        models = []
    watched = [name for name in WATCH_MODELS if name in models]
    return {
        "reachable": True,
        "detail": f"Ollama reachable at {base}",
        "models": watched,
        "pulled_all": models,
    }


def _dotenv_values() -> dict[str, str]:
    """Non-empty values from ``.env`` (keys never leave this function)."""
    for path in (Path.cwd() / ".env", REPO_ROOT / ".env"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        values: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("\"'")
        return values
    return {}


def meta_key_present() -> dict[str, object]:
    """Is a Meta key set or stored? Presence only, never the value."""
    if os.environ.get("META_API_KEY", "").strip() or os.environ.get(
        "MODEL_API_KEY", ""
    ).strip():
        return {"present": True, "source": "env"}
    try:
        import keyring  # type: ignore[import-not-found]

        stored = keyring.get_password("shadowfenced", "meta") or ""
        if stored.strip():
            return {"present": True, "source": "stored"}
    except Exception:
        pass
    dotenv = _dotenv_values()
    if dotenv.get("META_API_KEY", "").strip() or dotenv.get(
        "MODEL_API_KEY", ""
    ).strip():
        return {"present": True, "source": ".env"}
    return {"present": False, "source": "none"}


def package_installed() -> dict[str, object]:
    """Is the ``shadowfenced`` package importable?"""
    import importlib.util

    if importlib.util.find_spec("shadowfenced") is not None:
        return {"installed": True, "detail": "shadowfenced imports ok"}
    hint = "run: pip install -e ."
    if (REPO_ROOT / "src" / "shadowfenced").is_dir():
        hint = f"run from {REPO_ROOT}: pip install -e ."
    return {"installed": False, "detail": f"package not installed ({hint})"}


def choose_profile(
    ollama_ok: bool, models: list[str], vram_gb: float | None
) -> dict[str, object]:
    """Recommend full | light | code from probe results (pure function).

    ``models`` holds the watched models that are pulled. ``vram_gb`` is
    advisory: a small GPU keeps the full profile but warns that the 30B
    judge will be slow.
    """
    have = set(models)
    if ollama_ok and GLIMMER_MODEL in have:
        degraded = [
            "nothing: Glimmer judges hard cases locally, code settles the rest",
        ]
        if vram_gb is not None and vram_gb < 11:
            degraded = [
                f"only {vram_gb} GiB VRAM (the 30B judge wants 12): it may "
                "spill to CPU and answer slowly "
                "(tens of seconds per held message)",
            ]
        return {
            "profile": "full",
            "judge_model": GLIMMER_MODEL,
            "degraded": degraded,
            "pull_hint": "",
        }
    if ollama_ok and (set(LIGHT_MODELS) & have):
        judge = LIGHT_MODELS[0] if LIGHT_MODELS[0] in have else LIGHT_MODELS[1]
        return {
            "profile": "light",
            "judge_model": judge,
            "degraded": [
                f"smaller judge ({judge}): weaker verdicts, more messages "
                "held for you, no 30B review",
            ],
            "pull_hint": "",
        }
    if ollama_ok:
        return {
            "profile": "light",
            "judge_model": LIGHT_MODELS[0],
            "degraded": [
                "no judge model pulled yet: code tiers run, everything "
                "ambiguous is held for you until you pull one",
            ],
            "pull_hint": f"ollama pull {LIGHT_MODELS[0]}",
        }
    return {
        "profile": "code",
        "judge_model": "",
        "degraded": [
            "no local judge: code tiers settle clear cases, every hard "
            "case is held for you (HOLD), nothing is auto-released",
        ],
        "pull_hint": "",
    }


def collect(ollama_url: str | None = None) -> dict[str, object]:
    """Run every probe and return the report dict (never raises)."""
    ok, version = python_ok()
    base = ollama_url or os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL)
    try:
        ollama = ollama_status(base)
    except Exception as exc:  # pragma: no cover - defensive
        ollama = {
            "reachable": False,
            "detail": f"Ollama check failed ({type(exc).__name__})",
            "models": [],
            "pulled_all": [],
        }
    try:
        gpu = gpu_info()
    except Exception:  # pragma: no cover - defensive
        gpu = {"present": False, "detail": "no NVIDIA GPU found"}
    try:
        ram = free_ram_gb()
    except Exception:  # pragma: no cover - defensive
        ram = None
    try:
        key = meta_key_present()
    except Exception:  # pragma: no cover - defensive
        key = {"present": False, "source": "none"}
    try:
        package = package_installed()
    except Exception:  # pragma: no cover - defensive
        package = {"installed": False, "detail": "package check failed"}
    vram = gpu.get("max_vram_gb")
    verdict = choose_profile(
        bool(ollama.get("reachable")),
        list(ollama.get("models", [])),
        float(vram) if isinstance(vram, (int, float)) else None,
    )
    return {
        "python": {"ok": ok, "version": version, "min": "3.11"},
        "ram_free_gb": ram,
        "gpu": gpu,
        "ollama": {"url": base, **ollama},
        "meta_key": key,
        "package": package,
        "verdict": verdict,
    }


def start_command(profile: str) -> str:
    """The exact start command for one profile on this OS."""
    if os.name == "nt":
        return f"powershell -ExecutionPolicy Bypass -File setup\\start.ps1 -Profile {profile}"
    return f"./setup/start.sh {profile}"


def render_text(report: dict[str, object]) -> str:
    """Human-readable doctor output (no secret values, ever)."""
    python = report["python"]
    assert isinstance(python, dict)
    gpu = report["gpu"]
    assert isinstance(gpu, dict)
    ollama = report["ollama"]
    assert isinstance(ollama, dict)
    key = report["meta_key"]
    assert isinstance(key, dict)
    package = report["package"]
    assert isinstance(package, dict)
    verdict = report["verdict"]
    assert isinstance(verdict, dict)
    lines = [
        "Shadowfenced doctor",
        f"  Python: {'ok' if python['ok'] else 'TOO OLD'} "
        f"({python['version']}, need >= {python['min']})",
        "  Free RAM: "
        + (
            f"{report['ram_free_gb']} GiB"
            if report["ram_free_gb"] is not None
            else "unknown"
        ),
        f"  GPU: {gpu['detail']}",
        f"  Ollama: {ollama['detail']}",
    ]
    pulled = list(ollama.get("models", []))
    for name in WATCH_MODELS:
        lines.append(f"    {name}: {'pulled' if name in pulled else 'not pulled'}")
    lines.append(
        f"  Meta key: {'yes' if key['present'] else 'no'}"
        + (f" ({key['source']})" if key["present"] else "")
        + " — needed only to call Muse Spark upstream"
    )
    lines.append(f"  Package: {package['detail']}")
    lines.append("")
    profile = str(verdict["profile"])
    lines.append(f"Verdict: {profile.upper()}")
    if verdict["judge_model"]:
        lines.append(f"  Judge model: {verdict['judge_model']}")
    lines.append(f"  Start: {start_command(profile)}")
    if verdict["pull_hint"]:
        lines.append(f"  First: {verdict['pull_hint']}")
    degraded = verdict["degraded"]
    assert isinstance(degraded, list)
    for note in degraded:
        lines.append(f"  Degraded: {note}")
    if not package["installed"]:
        lines.append("  Note: install the package first (pip install -e .).")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the doctor; exit 1 only when Python itself is too old."""
    parser = argparse.ArgumentParser(description="Check this machine.")
    parser.add_argument("--json", action="store_true", help="JSON for agents")
    parser.add_argument("--ollama-url", default=None, help="Ollama base URL")
    args = parser.parse_args(argv)
    ok, _ = python_ok()
    if not ok:
        print(
            f"Python {sys.version.split()[0]} is too old; "
            f"Shadowfenced needs >= {'.'.join(str(p) for p in MIN_PYTHON)}.",
        )
        return 1
    report = collect(args.ollama_url)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())