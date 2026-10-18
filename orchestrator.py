#!/usr/bin/env python3
"""轻量研究编排器。

这个文件只负责三件事：

1. 为每个 agent 组装运行时上下文并渲染现有 prompt；
2. 按流程顺序启动 CLI agent，保存 prompt 和事件流；
3. 在整个 run 结束时请求一个 agent 选择交付模型，必要时回退基座模型。

研究状态、实验判断、Markdown 内容和失败解释都由 agent 自己负责。
本实现不读取或维护旧版 state.json/journal.md，不校验产物章节，不做
salvage、分数采纳、自动重试或 agent 内部命令 watchdog。

本文件只保留流程骨架；配置、预算、prompt 渲染、CLI 进程与收尾逻辑在 harness_utils/。
"""
from __future__ import annotations

import os
import shutil
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from harness_utils import budget
from harness_utils.budget import research_remaining_h, timer_remaining_h
from harness_utils.cli_runner import (
    _RESEARCH_STOP, end_research, log_artifact, research_watchdog, run_cli,
    stop_active_processes,
)
from harness_utils.common import log, node_dir, read_text
from harness_utils.config import (
    ASSETS, CLI, CLI_MODEL, FINALIZE_RESERVE_MIN, MAX_ROUNDS, MEASURE_FIRST,
    N_HYPO, NODES, PROMPTS, RES, TASK_TYPE, TASKS_DIR, AGENT_DIR,
)
from harness_utils.finalize import fallback_final_model, run_finalizer
from harness_utils.prompting import build_prompt, render


class ResearchTimeUp(Exception):
    pass


def run_step(
    step_file: str,
    kind: str,
    label: str,
    contract_name: str,
    phase: str,
    extra: dict | None = None,
    deny_tools: list[str] | None = None,
    with_agents: bool = False,
) -> Path:
    if _RESEARCH_STOP.is_set():
        raise ResearchTimeUp(label)
    node_path = node_dir(kind)
    contract = node_path / contract_name
    prompt = build_prompt(
        step_file,
        node_path,
        contract,
        extra=extra,
        timeout_min=max(1.0, research_remaining_h() * 60),
    )
    run_cli(prompt, node_path, label, phase, deny_tools, with_agents)
    extra_path = (extra or {}).get("SELECTED_CONTRACT_PATH")
    log_artifact(label, contract, extra_path, node_path=node_path)
    return node_path


def bootstrap() -> None:
    RES.mkdir(parents=True, exist_ok=True)
    NODES.mkdir(parents=True, exist_ok=True)

    def copy_if_missing(source: Path, target: Path) -> None:
        if source.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    copy_if_missing(ASSETS / "guard.py", RES / "guard.py")
    copy_if_missing(ASSETS / "benchmark_profile.json",
                    RES / "benchmark_profile.json")
    action_source = TASKS_DIR / TASK_TYPE / "action_space.json"
    if not action_source.is_file():
        action_source = AGENT_DIR / "action_space.json"
    copy_if_missing(action_source, RES / "action_space.json")
    copy_if_missing(PROMPTS / "WORKFLOW_OVERVIEW.md",
                    RES / "WORKFLOW_OVERVIEW.md")

    settings_template = ASSETS / "hooks_settings.json"
    if settings_template.is_file() and not (RES / "settings.json").exists():
        settings = settings_template.read_text(encoding="utf-8")
        settings = render(settings, {
            "PYTHON": sys.executable,
            "GUARD": str(RES / "guard.py"),
        })
        (RES / "settings.json").write_text(settings, encoding="utf-8")


def step0_init() -> None:
    log("==== Step0 Golden Init：启动三路 specialist ====")
    jobs = [
        (
            "step0_protocol.md", "golden-protocol", "golden_protocol",
            "protocol.md", "golden_protocol", ["WebSearch", "WebFetch"], False,
        ),
        (
            "step0_baseline.md", "golden-baseline", "golden_baseline",
            "baseline.md", "golden_baseline", ["WebSearch", "WebFetch"], True,
        ),
        (
            "step0_literature_review.md", "golden-literature",
            "golden_literature", "literature.md", "golden_literature",
            None, False,
        ),
    ]

    def launch(item):
        return run_step(
            item[0], item[1], item[2], item[3], item[4],
            deny_tools=item[5], with_agents=item[6],
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(launch, jobs))

    run_step(
        "step0_synthesis.md",
        "golden-synthesis",
        "golden_synthesis",
        "golden_synthesis.md",
        "golden_synthesis",
        deny_tools=["WebSearch", "WebFetch"],
        with_agents=False,
    )

    recipe = read_text(RES / "golden_recipe.md").strip()
    golden_run_dir = run_step(
        "step0_golden_run.md",
        "golden-run",
        "golden_run",
        "result.md",
        "experiment",
        extra={"HYPOTHESIS": recipe or (
            "research/golden_recipe.md 尚未生成。请读取现有 Step0 产物，"
            "自行补齐并记录实际执行结果。"
        )},
        with_agents=True,
    )
    run_step(
        "step4_archive.md",
        "golden-archive",
        "archive",
        "archive_card.md",
        "archive",
        extra={
            "NODE_ID": golden_run_dir.name,
            "TARGET_KEY": "Golden Recipe",
            "ARCHIVE_SOURCE": str(golden_run_dir / "result.md"),
            "MODEL_PATH": str(golden_run_dir / "model"),
            "BANK_PATH": str(RES / "experience_bank.md"),
        },
        deny_tools=["WebSearch", "WebFetch"],
        with_agents=False,
    )


def latest_measurement() -> str:
    paths = sorted(NODES.glob("n*-measurement/measurement.md"))
    return read_text(paths[-1]).strip() if paths else ""


def run_round(round_index: int) -> None:
    log(f"==== Research round {round_index} ====")
    if round_index == 1 and MEASURE_FIRST:
        run_step(
            "step2_measurement.md",
            "measurement",
            "measurement",
            "measurement.md",
            "measurement",
            extra={
                "MEASUREMENT_REASON": "首轮先建立行为分面；请读取已有协议、基线和 Golden Run 产物。",
            },
            deny_tools=["WebSearch", "WebFetch"],
            with_agents=True,
        )

    hypothesis_dir = run_step(
        "step1_hypothesis.md",
        "hypotheses",
        "hypotheses",
        "hypotheses.md",
        "hypothesis",
        extra={
            "N_HYPO": N_HYPO,
            "MODE": "explore",
            "MODE_HINT": (
                "请读取全部前置 Markdown，自主选择当前最有价值且未重复的研究方向。"
            ),
            "ALLOWED_LAYERS": "strategy/exec",
            "MEASUREMENT_DIAGNOSIS": latest_measurement() or "（暂无独立 Measurement 产物。）",
            "REPAIR_HINT": "（无；请根据已有产物自行判断。）",
            "BANK_PATH": str(RES / "experience_bank.md"),
        },
        deny_tools=["WebSearch", "WebFetch"],
        with_agents=False,
    )

    run_step(
        "step1_judge.md",
        "judge",
        "judge",
        "judge.md",
        "judge",
        extra={
            "N_HYPO": N_HYPO,
            "HYPOTHESES_FILE": str(hypothesis_dir / "hypotheses.md"),
            "SELECTED_CONTRACT_PATH": str(hypothesis_dir / "selected_hypothesis.md"),
            "BANK_PATH": str(RES / "experience_bank.md"),
            "ALLOWED_LAYERS": "strategy/exec",
            "MODE": "explore",
            "MODE_HINT": "读取候选和全部历史 Markdown 后自主裁决。",
            "COST_CAP_H": round(max(0.1, research_remaining_h()), 2),
        },
        deny_tools=["WebSearch", "WebFetch"],
        with_agents=False,
    )
    selected_contract = hypothesis_dir / "selected_hypothesis.md"

    experiment_dir = run_step(
        "step3_experiment.md",
        "experiment",
        "experiment",
        "experiment.md",
        "experiment",
        extra={
            "HYPOTHESIS_FILE": str(selected_contract),
            "COST_CAP_H": round(max(0.1, research_remaining_h()), 2),
        },
        with_agents=True,
    )

    run_step(
        "step4_archive.md",
        "archive",
        "archive",
        "archive_card.md",
        "archive",
        extra={
            "NODE_ID": experiment_dir.name,
            "TARGET_KEY": "从 selected_hypothesis.md 与 experiment.md 的干预包清单提取（允许 Multi: 坐标组合）",
            "ARCHIVE_SOURCE": str(experiment_dir / "experiment.md"),
            "MODEL_PATH": str(experiment_dir / "model"),
            "BANK_PATH": str(RES / "experience_bank.md"),
        },
        deny_tools=["WebSearch", "WebFetch"],
        with_agents=False,
    )

def on_term(signum, _frame) -> None:
    log(f"收到信号 {signum}，停止当前 agent 并尝试交付 final_model")
    stop_active_processes()
    fallback_final_model()
    os._exit(128 + signum)


def main() -> int:
    if CLI not in {"claude", "codex"}:
        log(f"ERROR RESEARCH_CLI 只能是 claude 或 codex，当前为 {CLI}")
        return 2

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    total_h = timer_remaining_h()
    budget._DEADLINE = time.time() + total_h * 3600
    bootstrap()
    log(f"轻量编排器启动：预算 {total_h:.2f}h，cli={CLI}，model={CLI_MODEL or 'default'}")

    threading.Thread(
        target=research_watchdog,
        args=(budget._DEADLINE - FINALIZE_RESERVE_MIN * 60,),
        daemon=True,
    ).start()

    try:
        try:
            step0_init()
            for round_index in range(1, MAX_ROUNDS + 1):
                run_round(round_index)
        except Exception:
            # 预留时间点到达后，被终止的 agent 可能以任意异常退出。
            if not _RESEARCH_STOP.is_set():
                raise
            log("研究阶段到达 finalizer 预留时间点")
        end_research()
        run_finalizer()
    finally:
        fallback_final_model()

    log("==== 轻量研究编排结束 ====")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        stop_active_processes()
        fallback_final_model()
        sys.exit(1)
