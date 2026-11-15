"""mission.exe 子进程适配层。

AFSIM 的输出路径相对进程 cwd，因此运行前始终把工作目录切换到工程目录，否则
`event_output` 等文件会写到调用方的目录或直接失败。
"""

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from afsim_lg.config import Settings, get_settings


@dataclass
class MissionResult:
    """一次 mission.exe 执行的结果摘要。"""

    ok: bool
    exit_code: int
    workdir: Path
    input_file: str
    elapsed_sec: float
    errors: list[str] = field(default_factory=list)
    output: str = ""
    timed_out: bool = False


def extract_errors(output: str) -> list[str]:
    """提取 ERROR/FATAL 行，并把下一行的位置信息并入同一条错误。

    AFSIM 把错误位置单独放在下一行（如 `'main.txt', line 10`），拆开会让
    修复提示词失去定位信息。

    Args:
        output: mission.exe 的完整标准输出与标准错误。

    Returns:
        去重前的错误消息列表。
    """
    errors: list[str] = []
    lines = output.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith(("***** ERROR", "***** FATAL")):
            continue
        message = stripped.lstrip("* ").strip()
        # 位置信息单独打印在错误行的下一行，并入同一条便于模型定位
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
    """在指定工程目录运行 mission.exe。

    Args:
        workdir: 工程目录，子进程的 cwd。
        input_file: 入口文件名，相对 workdir。
        timeout_sec: 超时秒数。
        settings: 可注入的配置，测试时使用。

    Returns:
        包含通过状态、退出码、错误列表与完整输出的结果。

    Raises:
        FileNotFoundError: mission.exe 或入口文件不存在。
    """
    settings = settings or get_settings()
    workdir = Path(workdir).resolve()
    if not settings.mission_exe.is_file():
        raise FileNotFoundError(f"mission.exe not found: {settings.mission_exe}")
    if not (workdir / input_file).is_file():
        raise FileNotFoundError(f"input file not found: {workdir / input_file}")

    started = time.monotonic()
    try:
        proc = subprocess.run(
            # -es 是默认的事件步进执行器，显式写出避免依赖版本默认值
            [str(settings.mission_exe), "-es", input_file],
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            # AFSIM 的 Qt 组件偶尔输出非 UTF-8 字节，替换而不是整段丢弃
            errors="replace",
            timeout=timeout_sec,
        )
        elapsed = time.monotonic() - started
        output = proc.stdout + proc.stderr
        errors = extract_errors(output)
        # 退出码之外再用 FATAL 兜底，避免日志已报致命错误却被判成功
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
            # 超时没有真实退出码，用 -1 标记，调用方据此区分“跑挂”和“超时”
            exit_code=-1,
            workdir=workdir,
            input_file=input_file,
            elapsed_sec=elapsed,
            errors=[f"timeout after {timeout_sec}s"],
            output=output,
            timed_out=True,
        )


def check_env(settings: Settings | None = None) -> dict:
    """检查 AFSIM 安装并读取版本号。

    Args:
        settings: 可注入的配置，测试时使用。

    Returns:
        包含 afsim_home、mission_exe、是否存在与版本的字典。
    """
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
