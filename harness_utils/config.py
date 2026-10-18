"""路径与环境变量常量。"""
from __future__ import annotations

import os
from pathlib import Path


TASK_DIR = Path.cwd().resolve()
RES = TASK_DIR / "research"
NODES = RES / "nodes"
AGENT_DIR = Path(os.environ["RESEARCH_AGENT_DIR"]).resolve()
PROMPTS = AGENT_DIR / "agents"
ASSETS = AGENT_DIR / "assets"
TASKS_DIR = AGENT_DIR / "tasks"

CLI = os.environ.get("RESEARCH_CLI", "claude")
CLAUDE = os.environ.get("RESEARCH_CLAUDE_BIN", "/usr/bin/claude")
CODEX = os.environ.get("RESEARCH_CODEX_BIN", "/usr/bin/codex")
CLI_MODEL = os.environ.get("AGENT_CONFIG", "")
EFFORT = os.environ.get("RESEARCH_EFFORT", "max")
CODEX_EFFORT = os.environ.get("RESEARCH_CODEX_EFFORT", "high")
USE_AGENTS = os.environ.get("RESEARCH_USE_AGENTS", "1") == "1"
DRY = os.environ.get("RESEARCH_DRY_RUN", "0") == "1"
# --print 下 Monitor 的完成通知送不回来，agent 会空等。每个 Claude 节点都禁。
ALWAYS_DENY = ["Monitor"]

MODEL = os.environ.get("MODEL", "Qwen/Qwen3-4B-Base")
TASK = os.environ.get("TASK", "gsm8k")
REPO_ROOT = Path(os.environ.get(
    "REPO_ROOT", "/root/paddlejob/rl-public/xiasheng01/xs_workdir/myptbench/PostTrainBench"))
TASK_TYPE = os.environ.get("RESEARCH_TASK_TYPE", "post_train")

MAX_ROUNDS = int(os.environ.get(
    "RESEARCH_MAX_ROUNDS", os.environ.get("RESEARCH_MAX_NODES", "40")))
N_HYPO = max(1, int(os.environ.get("RESEARCH_N_HYPO", "3")))
MEASURE_FIRST = os.environ.get("RESEARCH_MEASURE_FIRST", "1") == "1"
FINALIZE_RESERVE_MIN = float(os.environ.get(
    "RESEARCH_FINALIZE_RESERVE_MIN", "20"))
RULER_N = int(os.environ.get("RESEARCH_RULER_N", "300"))
RULER_MIN_N = int(os.environ.get("RESEARCH_RULER_MIN_N", "200"))
OFFICIAL_CHECK_LIMIT = int(os.environ.get(
    "RESEARCH_OFFICIAL_CHECK_LIMIT", "150"))

PROTOCOL_SECTIONS = [
    "## 1. 评测执行指令与参数",
    "## 2. 输入提示词与格式模板",
    "## 3. 停机符与输出长度限制",
    "## 4. 答案抽取与判定逻辑",
]
