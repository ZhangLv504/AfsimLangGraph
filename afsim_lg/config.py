"""项目配置：AFSIM 路径、模型名与本地存储位置。

配置集中为不可变的 Settings，环境变量可覆盖默认值，方便在不同机器或测试中切换。
"""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    """一次进程生命周期内使用的配置快照。"""

    project_root: Path
    afsim_home: Path
    afsim_bin: Path
    mission_exe: Path
    ollama_host: str
    llm_model: str
    embed_model: str
    chroma_dir: Path
    corpus_collection: str
    runs_dir: Path
    checkpoints_db: Path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """读取环境变量并返回配置单例。

    缓存保证所有节点拿到同一份配置，避免运行过程中路径或模型名漂移。

    Returns:
        当前进程的配置。
    """
    afsim_home = Path(os.getenv("AFSIM_HOME", PROJECT_ROOT / "afsim2.9en")).resolve()
    afsim_bin = Path(os.getenv("AFSIM_BIN", afsim_home / "bin")).resolve()
    return Settings(
        project_root=PROJECT_ROOT,
        afsim_home=afsim_home,
        afsim_bin=afsim_bin,
        mission_exe=afsim_bin / "mission.exe",
        ollama_host=os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434"),
        llm_model=os.getenv("AFSIM_LLM_MODEL", "qwen2.5-coder-7b"),
        embed_model=os.getenv("AFSIM_EMBED_MODEL", "bge-m3"),
        chroma_dir=Path(os.getenv("AFSIM_CHROMA_DIR", PROJECT_ROOT / "data" / "chroma")),
        corpus_collection="afsim_corpus",
        runs_dir=Path(os.getenv("AFSIM_RUNS_DIR", PROJECT_ROOT / "workspace" / "runs")),
        checkpoints_db=Path(
            os.getenv("AFSIM_CHECKPOINTS_DB", PROJECT_ROOT / "data" / "checkpoints.sqlite")
        ),
    )
