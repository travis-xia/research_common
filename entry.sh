#!/bin/bash
# research 编排器的共用入口。由 agents/{codex,claude}_research/solve.sh 调用，
# 后者只负责选定 RESEARCH_CLI（驱动节点的 CLI），保持仓库里"一个 CLI 一个 agent 目录"的惯例。
#
# 对 PostTrainBench 的契约与其它 agent 完全一致：
#   cwd = $JOB_TASK，读环境里的 $PROMPT（本 agent 不使用它，自己组装每个节点的 prompt），
#   在硬超时前于 cwd 下留下 ./final_model。
# run_in_container.sh 只把 solve.sh 拷成 $WORK/agent_solve.sh，因此源码要靠 $REPO_ROOT 定位。
set -uo pipefail

unset GEMINI_API_KEY
export BASH_MAX_TIMEOUT_MS="36000000"

RESEARCH_CLI="${RESEARCH_CLI:-claude}"
export RESEARCH_CLI

REPO_ROOT="${REPO_ROOT:-/root/ComateProjects/chats/myptbench/PostTrainBench}"
COMMON_SRC="$REPO_ROOT/agents/research_common"
if [ ! -d "$COMMON_SRC" ]; then
    echo "ERROR: 找不到编排器源码 $COMMON_SRC（检查 REPO_ROOT）" >&2
    exit 1
fi

# 按 CLI 收敛鉴权，与仓库里 codex/solve.sh、claude/solve.sh 的做法对齐
case "$RESEARCH_CLI" in
    codex)
        unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
        export CODEX_API_KEY="${CODEX_API_KEY:-${OPENAI_API_KEY:-}}"
        # codexhigh/codex_xhigh 靠改 /home/ben/.codex/config.toml 设推理档位（同事镜像遗留
        # 路径，本机 $HOME=/root 时无效）。这里改用 -c model_reasoning_effort=，由编排器传。
        ;;
    claude)
        unset CODEX_API_KEY
        # ANTHROPIC_* 由 run_in_container.sh 的 claude* 分支设好；单跑时靠 host.env
        ;;
    *)
        echo "ERROR: RESEARCH_CLI 只能是 codex 或 claude（当前 $RESEARCH_CLI）" >&2
        exit 1
        ;;
esac

# 把编排器源码复制进任务目录：这样归档能带走，反作弊 judge 也能审到流程本身。
mkdir -p research
rm -rf research/harness_src
cp -r "$COMMON_SRC" research/harness_src

export RESEARCH_AGENT_DIR="$PWD/research/harness_src"
export RESEARCH_DIR="$PWD/research"
# 每个节点自带 CLI 配置目录（由编排器设到节点目录下），不碰本机共享状态
unset CLAUDE_CONFIG_DIR

PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
    echo "ERROR: 找不到 python" >&2
    exit 1
fi

echo "==== research orchestrator ===="
echo "cwd=$PWD"
echo "cli=$RESEARCH_CLI  agent_src=$RESEARCH_AGENT_DIR"
case "$RESEARCH_CLI" in
    codex)  echo "codex=$(/usr/bin/codex --version 2>/dev/null | head -1)" ;;
    claude) echo "claude=$(/usr/bin/claude --version 2>/dev/null | head -1)" ;;
esac
echo "cli_model=${AGENT_CONFIG:-unset} base_model=${MODEL:-unset} task=${TASK:-unset}"
echo "=============================="

exec "$PY" "$RESEARCH_AGENT_DIR/orchestrator.py"
