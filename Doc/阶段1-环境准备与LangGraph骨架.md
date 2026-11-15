# 阶段 1：环境准备与 LangGraph 骨架

- 日期：2026-11-15
- 对应提交：`2ddf132 初始化 AfsimLangGraph 工程`

## 目标

打通“自然语言需求 → 检索技能语料 → 生成 AFSIM 工程 → mission.exe 真实校验”的最小闭环，先验证环境与调用方式，再逐步补全生成和修复能力。

## 环境

| 组件 | 内容 |
|---|---|
| Python | `.venv`，3.12.7（uv 创建） |
| 依赖 | langgraph 1.2.22、langchain-core 1.8.0、chromadb 1.5.9、ollama 0.6.3、pydantic 2.14.3、fastapi 0.163.1、pytest 9.2.0、langgraph-checkpoint-sqlite 3.1.3 |
| 生成模型 | Ollama `qwen2.5-coder-7b`（本地已有） |
| 嵌入模型 | Ollama `bge-m3`（F16，1024 维，8192 上下文） |
| AFSIM | 项目根 `afsim2.9en/`，mission.exe 版本 2.9.0 02-25-2022，66 个官方 demo |
| 向量库 | `data/chroma`（Chroma 持久化） |
| 检查点 | `data/checkpoints.sqlite`（SqliteSaver） |

显存实测（RTX 5060 Laptop 8 GB）：qwen 5.4 GB + bge-m3 689 MB，均为 100% GPU，可同时驻留。

## AFSIM 调用方式（实测结论）

1. **输出路径相对进程 cwd**：从父目录传 `proj/main.txt` 时 `event_output` 打开失败；必须在工程目录内启动。
2. **include 会在 cwd 和输入文件目录两处查找**，但为了行为稳定，统一约定 `cwd = 工程目录`。
3. **本机 2.9.0 构建不认识 `thread_count`**：官方 engage demo 第 9 行使用了它，实测报 `Unknown command: thread_count`，因此生成规则中禁用该命令。
4. 错误行以 `***** ERROR` / `***** FATAL` 开头，位置信息在下一行（`'file', line N`）。

## 实现结构

| 模块 | 职责 |
|---|---|
| `afsim_lg/config.py` | AFSIM_HOME/BIN、模型名、Chroma/运行目录等配置，环境变量可覆盖 |
| `afsim_lg/afsim/runner.py` | `run_mission()` 以工程目录为 cwd 调 `mission.exe -es`，解析错误；`check_env()` 读版本 |
| `afsim_lg/rag/chunker.py` | Markdown 按标题栈 + 代码围栏原子切块；AFSIM .txt 按 `xxx … end_xxx` 完整块切分，附带 skill/path/kind/heading/block 元数据 |
| `afsim_lg/rag/embed.py` | 直接调 Ollama `/api/embed`，`trust_env=False`，每批 16 条 |
| `afsim_lg/rag/index.py` | Chroma cosine 集合，**显式传入 bge-m3 向量**（`embedding_function=None`，避免默认模型联网下载）；提供 `search()` |
| `afsim_lg/llm.py` | 调 Ollama `/api/chat`，支持 JSON 模式、8192 上下文 |
| `afsim_lg/graph/state.py` | 图状态：需求、检索结果、文件、尝试次数、mission 结果、验收结论 |
| `afsim_lg/graph/nodes.py` | retrieve / generate / write / validate / assess / fix / finalize 七个节点及文件标记解析 |
| `afsim_lg/graph/build.py` | 组装条件边，SqliteSaver 按 `thread_id` 持久化 |
| `afsim_lg/cli.py` | `check-afsim`、`validate`、`index`、`search`、`run` 五个子命令 |
| `tests/` | 真实 mission.exe 用例 2 个，解析与路由用例 3 个 |

## 图流程

```
START → retrieve → generate → write → validate → assess
                        ↑                              │
                        └──────── fix ←────────────────┘（失败且未达上限）
                                        └── 失败且达上限 → finalize → END
```

- 生成器要求按 `===FILE: 相对路径=== … ===END===` 输出，解析按行进行，兼容代码围栏与尾标记。
- 写盘前校验路径（拒绝绝对路径、`..`、非 `.txt`）。
- 校验节点把完整 stdout 写入 `workspace/runs/<run_id>/mission_stdout.log`，状态里只留摘要。
- 验收节点对照用户需求输出 `{passed, missing, fix_hint}`。

## 验证结果

- `pytest`：5 passed（含两次真实 `mission.exe` 调用）。
- 索引：440 块 / 106 文件 / 13.8 秒；`data/chroma`。
- 检索示例：
  - “SAM 火控雷达 跟踪 照射” → `blocks/radar_sam.txt`（cosine 距离 0.399）
  - “Unknown command 报错 修复” → `afsim-debug.md`、`refs/errors-ref.md`（0.316）
- 端到端冒烟：需求“蓝方一架战斗机沿航线飞行 60 秒”，一次生成通过；`mission.ok=true`，验收 passed，产物为 `main.txt`。
- 检查点：2 个 `thread_id`，16 条 checkpoint，可回放。

## 已知边界

- 检索只做单轮 top-8，未按 kind/skill 过滤，也未把 mission 报错映射到具体教训条目。
- 修复是整份工程重新生成，没有做最小 diff。
- 验收复用同一个 7B 模型，复杂需求的判定可靠性有限。
- FastAPI/SSE 入口尚未搭建。
- `afsim-tools/`（技能语料）与 `afsim2.9en/` 未纳入 git，克隆后需自行放置并重建索引。
