import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
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
