#!/usr/bin/env python3
"""轻量研究编排器。

这个文件只负责三件事：

1. 为每个 agent 组装运行时上下文并渲染现有 prompt；
2. 按流程顺序启动 CLI agent，保存 prompt 和事件流；
3. 在整个 run 结束时请求一个 agent 选择交付模型，必要时回退基座模型。

研究状态、实验判断、Markdown 内容和失败解释都由 agent 自己负责。
本实现不读取或维护旧版 state.json/journal.md，不校验产物章节，不做
salvage、分数采纳、自动重试或 agent 内部命令 watchdog。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path


TASK_DIR = Path.cwd().resolve()
RES = TASK_DIR / "research"
NODES = RES / "nodes"
AGENT_DIR = Path(os.environ["RESEARCH_AGENT_DIR"]).resolve()
PROMPTS = AGENT_DIR / "prompts"
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

_DEADLINE: float | None = None
_ACTIVE_PROCS: set[subprocess.Popen] = set()
_ACTIVE_LOCK = threading.Lock()
_NODE_LOCK = threading.Lock()
_RESEARCH_STOP = threading.Event()


class ResearchTimeUp(Exception):
    pass


def log(message: str) -> None:
    print(f"[orch {datetime.now().strftime('%H:%M:%S')}] {message}",
          flush=True)


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


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, RecursionError):
        return default


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return default


def timer_remaining_h() -> float:
    timer = TASK_DIR / "timer.sh"
    if timer.is_file():
        try:
            output = subprocess.run(
                ["bash", str(timer)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            ).stdout
            if "expired" in output.lower():
                return 0.0
            match = re.search(r"(\d+):(\d{2})", output)
            if match:
                return int(match.group(1)) + int(match.group(2)) / 60.0
        except Exception as exc:
            log(f"WARN 读取 timer.sh 失败: {exc!r}")
    try:
        return max(0.0, float(os.environ.get("NUM_HOURS", "10")))
    except ValueError:
        return 10.0


def remaining_h() -> float:
    if _DEADLINE is None:
        return timer_remaining_h()
    return max(0.0, (_DEADLINE - time.time()) / 3600.0)


def research_remaining_h() -> float:
    """研究节点可用时间：总剩余时间扣掉固定留给 finalizer 的部分。"""
    return max(0.0, remaining_h() - FINALIZE_RESERVE_MIN / 60.0)


def benchmark_name() -> str:
    path = REPO_ROOT / "src" / "eval" / "tasks" / TASK / "benchmark.txt"
    text = read_text(path).strip()
    return text or TASK


def next_node_id(kind: str) -> str:
    """节点编号只用于避免覆盖文件，不承担研究状态含义。"""
    with _NODE_LOCK:
        maximum = -1
        if NODES.is_dir():
            for item in NODES.iterdir():
                match = re.match(r"^n(\d+)-", item.name)
                if match:
                    maximum = max(maximum, int(match.group(1)))
        return f"n{maximum + 1:03d}-{kind}"


def node_dir(kind: str) -> Path:
    with _NODE_LOCK:
        maximum = -1
        if NODES.is_dir():
            for item in NODES.iterdir():
                match = re.match(r"^n(\d+)-", item.name)
                if match:
                    maximum = max(maximum, int(match.group(1)))
        path = NODES / f"n{maximum + 1:03d}-{kind}"
        path.mkdir(parents=True, exist_ok=False)
        return path


def render(text: str, context: dict) -> str:
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
    if deny_tools:
        command += ["--disallowedTools", ",".join(deny_tools)]
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
        "step5_archive.md",
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
        "step5_archive.md",
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


FINALIZE_PROMPT = """你是研究流水线的最终交付 agent。
剩余时间约 {{REMAINING_MIN}} 分钟，到点会被强制终止。

当前工作目录：{{TASK_DIR}}
研究目录：{{RESEARCH_DIR}}
基座模型：{{MODEL}}
最终交付目录：{{FINAL_MODEL_PATH}}
最终报告：{{CONTRACT_PATH}}

只做下面三件事，不要训练、不要评测，不要修改 prompts、编排器源码或原始实验报告：

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


def on_term(signum, _frame) -> None:
    log(f"收到信号 {signum}，停止当前 agent 并尝试交付 final_model")
    stop_active_processes()
    fallback_final_model()
    os._exit(128 + signum)


def main() -> int:
    global _DEADLINE
    if CLI not in {"claude", "codex"}:
        log(f"ERROR RESEARCH_CLI 只能是 claude 或 codex，当前为 {CLI}")
        return 2

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    total_h = timer_remaining_h()
    _DEADLINE = time.time() + total_h * 3600
    bootstrap()
    log(f"轻量编排器启动：预算 {total_h:.2f}h，cli={CLI}，model={CLI_MODEL or 'default'}")

    threading.Thread(
        target=research_watchdog,
        args=(_DEADLINE - FINALIZE_RESERVE_MIN * 60,),
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
