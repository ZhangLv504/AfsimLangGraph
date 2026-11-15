import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from afsim_lg.config import Settings, get_settings


@dataclass
class MissionResult:
    ok: bool
    exit_code: int
    workdir: Path
    input_file: str
    elapsed_sec: float
    errors: list[str] = field(default_factory=list)
    output: str = ""
    timed_out: bool = False


def extract_errors(output: str) -> list[str]:
    errors: list[str] = []
    lines = output.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith(("***** ERROR", "***** FATAL")):
            continue
        message = stripped.lstrip("* ").strip()
        location = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if location.startswith("'"):
            message = f"{message} @ {location}"
        errors.append(message)
    return errors


def run_mission(
    workdir: Path | str,
    input_file: str = "main.txt",
    timeout_sec: float = 600,
    settings: Settings | None = None,
) -> MissionResult:
    settings = settings or get_settings()
    workdir = Path(workdir).resolve()
    if not settings.mission_exe.is_file():
        raise FileNotFoundError(f"mission.exe not found: {settings.mission_exe}")
    if not (workdir / input_file).is_file():
        raise FileNotFoundError(f"input file not found: {workdir / input_file}")

    started = time.monotonic()
    try:
        proc = subprocess.run(
            [str(settings.mission_exe), "-es", input_file],
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
        )
        elapsed = time.monotonic() - started
        output = proc.stdout + proc.stderr
        errors = extract_errors(output)
        ok = proc.returncode == 0 and not any("FATAL" in e for e in errors)
        return MissionResult(
            ok=ok,
            exit_code=proc.returncode,
            workdir=workdir,
            input_file=input_file,
            elapsed_sec=elapsed,
            errors=errors,
            output=output,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        output = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        return MissionResult(
            ok=False,
            exit_code=-1,
            workdir=workdir,
            input_file=input_file,
            elapsed_sec=elapsed,
            errors=[f"timeout after {timeout_sec}s"],
            output=output,
            timed_out=True,
        )


def check_env(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    info = {
        "afsim_home": str(settings.afsim_home),
        "mission_exe": str(settings.mission_exe),
        "exists": settings.mission_exe.is_file(),
        "version": None,
    }
    if info["exists"]:
        proc = subprocess.run(
            [str(settings.mission_exe), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        info["version"] = proc.stdout.strip().splitlines()[1].strip()
    return info
