"""CLI agent 进程的启动、登记与终止。"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .budget import remaining_h
from .common import log
from .config import (
    ALWAYS_DENY, ASSETS, CLAUDE, CLI, CLI_MODEL, CODEX, CODEX_EFFORT, DRY,
    EFFORT, FINALIZE_RESERVE_MIN, PROTOCOL_SECTIONS, RES, TASK_DIR, USE_AGENTS,
)


_ACTIVE_PROCS: set[subprocess.Popen] = set()
_ACTIVE_LOCK = threading.Lock()
_RESEARCH_STOP = threading.Event()


def log_artifact(label: str, *paths, node_path=None) -> None:
    """节点结束后打印产物路径和 CLI 用量摘要。失败不影响编排。"""
    try:
        node = Path(node_path) if node_path else next((Path(p).parent for p in paths if p), None)
        stream_path = node / f"{label}.stream.jsonl" if node else None
        bits = [node.name if node else "", f"left={remaining_h():.2f}h"]
        result = None
        if stream_path:
            with stream_path.open("rb") as handle:
                handle.seek(0, 2)
                handle.seek(max(0, handle.tell() - 131072))
                tail = handle.read().decode("utf-8", errors="replace")
            for line in reversed(tail.splitlines()):
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict) and obj.get("type") == "result":
                    result = obj
                    break
        if result:
            usage = result.get("usage") or {}
            mu = result.get("modelUsage") or {}
            search = sum((v or {}).get("webSearchRequests") or 0
                         for v in mu.values() if isinstance(v, dict))
            bits += [
                result.get("subtype"),
                f"turns={result.get('num_turns')} ${result.get('total_cost_usd')} "
                f"in={usage.get('input_tokens')} out={usage.get('output_tokens')} "
                f"search={search} stop={result.get('stop_reason') or result.get('terminal_reason')}",
            ]
        log(f"{label} 结束: {' '.join(str(x) for x in bits if x)}")
        log(f"{label} 产物: {' '.join(str(p) for p in paths if p)}")
        log(f"{label} stream: {stream_path or ''}")
    except Exception:
        pass


def child_env(phase: str) -> dict:
    env = dict(os.environ)
    env["RESEARCH_DIR"] = str(RES)
    env["RESEARCH_PHASE"] = phase
    env["IS_SANDBOX"] = "1"
    # 允许训练和评测自然完成；不在编排器里给单条 Bash 命令另设短上限。
    env["BASH_MAX_TIMEOUT_MS"] = os.environ.get(
        "BASH_MAX_TIMEOUT_MS", "36000000")
    # 无头 --print 模式下回合结束即进程退出，后台任务的完成通知永远送不到；
    # 关掉后台任务并放宽单条命令默认超时，让 agent 在前台守完长任务。
    env["BASH_DEFAULT_TIMEOUT_MS"] = os.environ.get(
        "BASH_DEFAULT_TIMEOUT_MS", "3600000")
    env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = os.environ.get(
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "1")
    return env


def cli_command(node_path: Path, deny_tools: list[str] | None,
                with_agents: bool) -> tuple[list[str], dict]:
    if CLI == "codex":
        command = [CODEX, "exec", "--json", "--skip-git-repo-check", "--yolo",
                   "-c", "model_reasoning_summary=detailed",
                   "-c", f"model_reasoning_effort={CODEX_EFFORT}"]
        if CLI_MODEL:
            command += ["--model", CLI_MODEL]
        return command, {}

    command = [
        CLAUDE, "--print", "--verbose", "--output-format", "stream-json",
        "--effort", EFFORT, "--dangerously-skip-permissions",
        "--settings", str(RES / "settings.json"),
        "--session-id", str(uuid.uuid4()),
    ]
    if CLI_MODEL:
        command += ["--model", CLI_MODEL]
    denied = list(ALWAYS_DENY)
    for tool in deny_tools or []:
        if tool not in denied:
            denied.append(tool)
    command += ["--disallowedTools", ",".join(denied)]
    if with_agents and USE_AGENTS:
        subagents = ASSETS / "subagents.json"
        if subagents.is_file():
            command += ["--agents", subagents.read_text(encoding="utf-8")]
    return command, {"CLAUDE_CONFIG_DIR": str(node_path / ".cc")}


def _register_process(process: subprocess.Popen, stoppable: bool) -> bool:
    """登记进程；返回 True 表示研究阶段已结束，调用方应立即终止它。"""
    with _ACTIVE_LOCK:
        _ACTIVE_PROCS.add(process)
        return stoppable and _RESEARCH_STOP.is_set()


def _unregister_process(process: subprocess.Popen) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_PROCS.discard(process)


def run_cli(
    prompt: str,
    node_path: Path,
    label: str,
    phase: str,
    deny_tools: list[str] | None = None,
    with_agents: bool = False,
    stoppable: bool = True,
) -> int:
    node_path.mkdir(parents=True, exist_ok=True)
    (node_path / f"{label}.prompt.md").write_text(prompt, encoding="utf-8")
    stream_path = node_path / f"{label}.stream.jsonl"
    if DRY:
        stream_path.write_text(
            json.dumps({"type": "dry_run", "label": label}) + "\n",
            encoding="utf-8",
        )
        return dry_output(label, node_path, prompt)

    command, extra_env = cli_command(node_path, deny_tools, with_agents)
    env = child_env(phase)
    env.update(extra_env)
    if "CLAUDE_CONFIG_DIR" in env:
        Path(env["CLAUDE_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)

    log(f"启动 {label}: {' '.join(command[:3])}")
    started = time.time()
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(TASK_DIR),
        env=env,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    if _register_process(process, stoppable):
        kill_process_groups([process])
    events = 0
    try:
        with stream_path.open("w", encoding="utf-8") as stream:
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(prompt)
            process.stdin.close()
            for line in process.stdout:
                stream.write(line)
                events += 1
        return process.wait()
    finally:
        _unregister_process(process)
        log(f"{label}: rc={process.returncode} events={events} "
            f"elapsed={int(time.time() - started)}s")


def dry_output(label: str, node_path: Path, prompt: str) -> int:
    """仅用于检查调度和 prompt 注入，不做产物契约校验。"""
    if label == "golden_protocol":
        (RES / "protocol.md").write_text(
            "# Dry protocol\n\n" + "\n\n".join(PROTOCOL_SECTIONS) + "\n",
            encoding="utf-8",
        )
    elif label == "golden_baseline":
        (RES / "baseline.md").write_text(
            "---\nstatus: ok\nscore: 0.0\neval_mode: official_full\nn: 0\n---\n\n"
            "# Dry baseline\n",
            encoding="utf-8",
        )
    elif label == "golden_literature":
        (RES / "literature.md").write_text(
            "# Dry literature\n\n## 1. 核心候选数据集\n- dry\n",
            encoding="utf-8",
        )
    elif label == "golden_synthesis":
        (RES / "golden_recipe.md").write_text(
            "# Dry Golden Recipe\n\n## 1. 综合诊断与决策依据\n- dry\n",
            encoding="utf-8",
        )
    else:
        contract = node_path / {
            "golden_run": "result.md",
            "measurement": "measurement.md",
            "hypotheses": "hypotheses.md",
            "judge": "judge.md",
            "experiment": "experiment.md",
            "archive": "archive_card.md",
            "finalize": "finalize.md",
        }.get(label, f"{label}.md")
        contract.write_text(
            f"# Dry output: {label}\n\n"
            "这是调度链路的 dry-run 占位输出，不代表真实研究结果。\n",
            encoding="utf-8",
        )
    return 0


def stop_active_processes() -> None:
    with _ACTIVE_LOCK:
        processes = list(_ACTIVE_PROCS)
    kill_process_groups(processes)


def kill_process_groups(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def research_watchdog(stop_at: float) -> None:
    """到 finalizer 预留时间点时终止所有研究 agent，不等当前步骤结束。"""
    if _RESEARCH_STOP.wait(max(0.0, stop_at - time.time())):
        return
    with _ACTIVE_LOCK:
        if _RESEARCH_STOP.is_set():
            return
        _RESEARCH_STOP.set()
        processes = list(_ACTIVE_PROCS)
    log(f"距截止只剩 {FINALIZE_RESERVE_MIN:g} 分钟，终止研究 agent，转入 finalizer")
    kill_process_groups(processes)


def end_research() -> None:
    with _ACTIVE_LOCK:
        _RESEARCH_STOP.set()
