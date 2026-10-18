"""prompt 渲染与运行时上下文组装。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .budget import research_remaining_h
from .common import read_json, read_text
from .config import (
    AGENT_DIR, MODEL, OFFICIAL_CHECK_LIMIT, PROMPTS, REPO_ROOT, RES,
    RULER_MIN_N, RULER_N, TASK, TASK_DIR,
)


def benchmark_name() -> str:
    path = REPO_ROOT / "src" / "eval" / "tasks" / TASK / "benchmark.txt"
    text = read_text(path).strip()
    return text or TASK


def render(text: str, context: dict) -> str:
    # {{INCLUDE:<相对 skills/ 的路径>}} 先展开成对应文件内容，再统一替换占位符。
    text = re.sub(
        r"\{\{INCLUDE:([\w./-]+)\}\}",
        lambda m: read_text(AGENT_DIR / "skills" / m.group(1),
                            f"（缺失注入文件 skills/{m.group(1)}）").strip(),
        text,
    )
    for key, value in context.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def inject_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def action_space() -> dict:
    return read_json(RES / "action_space.json", {"domains": {}})


def ruler_of_run() -> dict | None:
    golden = read_json(RES / "golden_init.json", {}) or {}
    ruler = golden.get("ruler")
    return ruler if isinstance(ruler, dict) else None


def eval_policy_text() -> str:
    ruler = ruler_of_run()
    if not ruler:
        return (
            "- Golden Init 阶段优先使用官方 `evaluate.py --limit -1` 全量评测；\n"
            f"- 只有窗口装不下时才退到 `--limit {OFFICIAL_CHECK_LIMIT}`，并记录 "
            "`eval_mode: official_subset`；\n"
            f"- 固定子集时样本数不得低于 {RULER_MIN_N}；\n"
            "- 不要自行编写与官方语义不同的评测器。\n"
        )
    mode = ruler.get("mode", "full")
    n = ruler.get("n")
    command = ruler.get("cmd", "")
    scale = "official_full" if mode == "full" else f"official_subset (n={n})"
    return (
        f"- 本 run 的统一评测命令：`{command}`\n"
        "- 后续评测只替换模型路径和输出路径，不要临时修改其它参数。\n"
        f"- 当前统一刻度：{scale}；metrics 必须包含 n 和 eval_mode。\n"
    )


def protocol_sources() -> str:
    profile = read_json(RES / "benchmark_profile.json", {}) or {}
    return "、".join(profile.get("protocol_sources") or [
        "evaluate.py", "templates/", "Scorer 实现", "相关配置文件",
    ])


def eval_cmd_hint() -> str:
    profile = read_json(RES / "benchmark_profile.json", {}) or {}
    command = profile.get(
        "eval_cmd_hint",
        "python evaluate.py --model-path {{MODEL}} ...",
    )
    return str(command).replace("{{MODEL}}", str(MODEL))


def prior_context_summary() -> str:
    """只生成文件指针，不替 agent 解释历史研究结果。"""
    files = []
    for path in sorted(RES.rglob("*.md")):
        if path.name.endswith(".prompt.md"):
            continue
        files.append(str(path.relative_to(TASK_DIR)))
    if not files:
        return "（目前还没有前置 Markdown 产物，请按当前任务自行建立事实。）"
    return "（前置 Markdown 产物请按需读取：\n- " + "\n- ".join(files) + "\n）"


def build_prompt(
    step_file: str,
    node_path: Path,
    contract: Path,
    extra: dict | None = None,
    timeout_min: float | None = None,
) -> str:
    body = (PROMPTS / step_file).read_text(encoding="utf-8")
    profile = read_json(RES / "benchmark_profile.json", {}) or {}
    context = {
        "TASK_DIR": str(TASK_DIR),
        "NODE_DIR": str(node_path),
        "MODEL": MODEL,
        "BENCHMARK": benchmark_name(),
        "NODE_TIMEOUT_MIN": "" if step_file == "step0_golden_run.md"
        else round(timeout_min if timeout_min is not None else research_remaining_h() * 60, 2),
        "REMAINING_H": round(research_remaining_h(), 2),
        "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
        "PROTOCOL": read_text(RES / "protocol.md").strip(),
        "BEST_SUMMARY": prior_context_summary(),
        "JOURNAL": prior_context_summary(),
        "ACTION_SPACE": inject_json(action_space()),
        "EVAL_POLICY": eval_policy_text(),
        "EVAL_CMD_HINT": eval_cmd_hint(),
        "PROTOCOL_SOURCES": protocol_sources(),
        "RULER_N": RULER_N,
        "RULER_MIN_N": RULER_MIN_N,
        "OFFICIAL_CHECK_LIMIT": OFFICIAL_CHECK_LIMIT,
        "SKILLS_DIR": str(AGENT_DIR / "skills"),
        "CONTRACT_PATH": str(contract),
        "WORKFLOW_OVERVIEW_PATH": "research/WORKFLOW_OVERVIEW.md",
        "BANK_PATH": str(RES / "experience_bank.md"),
    }
    context.update(extra or {})
    return render(body, context)
