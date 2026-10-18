"""收尾交付：finalizer agent 与基座模型兜底。"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .budget import remaining_h
from .cli_runner import log_artifact, run_cli
from .common import log, node_dir
from .config import MODEL, RES, TASK_DIR
from .prompting import render


FINALIZE_PROMPT = """你是研究流水线的最终交付 agent。
剩余时间约 {{REMAINING_MIN}} 分钟，到点会被强制终止。

当前工作目录：{{TASK_DIR}}
研究目录：{{RESEARCH_DIR}}
基座模型：{{MODEL}}
最终交付目录：{{FINAL_MODEL_PATH}}
最终报告：{{CONTRACT_PATH}}

只做下面三件事，不要训练、不要评测，不要修改节点提示词、编排器源码或原始实验报告：

1. 选择：读取 research/ 下的 Markdown、各节点实验报告中已有的评测结果和模型路径，
   判断哪个模型最值得交付。不要依赖 state.json，也不要假设最后一个节点最好。
   没有可用的改进模型时，选择基座模型或其本地快照。
2. 拷贝：先把选中的完整模型目录复制到 `{{FINAL_MODEL_PATH}}.tmp`，
   确认 config.json、tokenizer 和权重文件齐全后，再 `mv` 为 `{{FINAL_MODEL_PATH}}`。
3. 记录：把选择依据、来源模型路径和最终状态写入 `{{CONTRACT_PATH}}`。
"""


def run_finalizer() -> None:
    node_path = node_dir("finalize")
    contract = node_path / "finalize.md"
    prompt = render(FINALIZE_PROMPT, {
        "TASK_DIR": str(TASK_DIR),
        "RESEARCH_DIR": str(RES),
        "MODEL": MODEL,
        "FINAL_MODEL_PATH": str(TASK_DIR / "final_model"),
        "CONTRACT_PATH": str(contract),
        "REMAINING_MIN": round(remaining_h() * 60),
    })
    run_cli(prompt, node_path, "finalize", "finalize", with_agents=False,
            stoppable=False)
    log_artifact("finalize", contract, node_path=node_path)


def resolve_base_model() -> Path | None:
    model = Path(MODEL)
    if model.is_dir():
        return model
    if "/" in MODEL:
        org, name = MODEL.split("/", 1)
        hf_home = Path(os.environ.get("HF_HOME", ""))
        candidates = [
            hf_home / org / name,
            hf_home / "hub" / f"models--{org}--{name}" / "snapshots" / "local",
        ]
        for candidate in candidates:
            if candidate.is_dir() and (candidate / "config.json").is_file():
                return candidate
    return None


def fallback_final_model() -> None:
    destination = TASK_DIR / "final_model"
    if destination.exists():
        return
    source = resolve_base_model()
    if not source:
        log("WARN 无法定位基座模型，未生成 final_model")
        return
    try:
        shutil.copytree(source, destination, symlinks=False)
        log(f"final_model 回退为基座模型: {source}")
    except Exception as exc:
        log(f"WARN 生成基座 final_model 失败: {exc!r}")
