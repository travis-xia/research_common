#!/usr/bin/env python3
"""research —— 序列决策式 post-training 研究编排器。

对 PostTrainBench harness 的契约：cwd = $JOB_TASK，结束时产出 ./final_model。
除此之外本文件不依赖 harness 的任何内部实现，也不修改 harness 的任何文件。

流程（对齐 流程记录.md）：
    Step0 Golden Init（三路并行 specialist + 汇总 init agent 做初步决策）
      └─► loop: Step1 猜想×N(并行，坐标互斥/视角互异/步长下发) -> 硬规则前筛 -> Judge 两两对比
                -> (若全否则触发 Step2 Measurement 升级诊断并回流重新生成猜想)
                -> Step3 Experiment 端到端实验落地（规划方案+运行前代码自查+占卡训练与评测）
                -> Step5 记录 -> (平台期重新点火 / 常态探索 / 收尾保护)
    finalize: best -> final_model

每个节点是一个独立的 CLI 进程（`codex exec` 或 `claude -p`，由 RESEARCH_CLI 选定），
上下文由本编排器投喂，产物走文件契约——所以换 CLI 不影响流程本身。
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

# ---------------------------------------------------------------- 0. 配置
TASK_DIR = Path.cwd()
RES = TASK_DIR / "research"
NODES = RES / "nodes"
AGENT_DIR = Path(os.environ["RESEARCH_AGENT_DIR"]).resolve()
PROMPTS = AGENT_DIR / "prompts"
ASSETS = AGENT_DIR / "assets"
TASKS_DIR = AGENT_DIR / "tasks"
TASK_TYPE = os.environ.get("RESEARCH_TASK_TYPE", "post_train")
SETTINGS = RES / "settings.json"
STATE_F = RES / "state.json"
JOURNAL = RES / "journal.md"
BANK_PATH = RES / "experience_bank.md"
PHASE_F = RES / ".phase"

CLAUDE = os.environ.get("RESEARCH_CLAUDE_BIN", "/usr/bin/claude")
CODEX = os.environ.get("RESEARCH_CODEX_BIN", "/usr/bin/codex")
CLI = os.environ.get("RESEARCH_CLI", "claude")     # 由 agents/{codex,claude}_research 选定
CLI_MODEL = os.environ.get("AGENT_CONFIG", "")
EFFORT = os.environ.get("RESEARCH_EFFORT", "max")              # claude: low..max
CODEX_EFFORT = os.environ.get("RESEARCH_CODEX_EFFORT", "high")  # codex: low/medium/high/xhigh
MODEL = os.environ.get("MODEL", "Qwen/Qwen3-4B-Base")
TASK = os.environ.get("TASK", "gsm8k")
REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/root/ComateProjects/chats/myptbench/PostTrainBench"))
DRY = os.environ.get("RESEARCH_DRY_RUN", "0") == "1"

# protocol 产物是 markdown 提醒清单而不是 JSON 契约（2026-09-10 用户决策）：读它的都是
# LLM 节点，字段化只会把篇幅撑大。下面几个小节标题是唯一的结构约定，缺了也只是提醒。
# 标题与 prompts/step0_protocol.md 的产物契约逐字对齐（2026-09-17 prompt 改版）。
PROTOCOL_MD = "protocol.md"
PROTOCOL_SECTIONS = ["## 1. 评测执行指令与参数", "## 2. 输入提示词与格式模板",
                     "## 3. 停机符与输出长度限制", "## 4. 答案抽取与判定逻辑"]

# 调度旋钮（都可用环境变量覆盖，便于做消融）
N_HYPO = int(os.environ.get("RESEARCH_N_HYPO", "3"))
MIN_GROUNDED = float(os.environ.get("RESEARCH_MIN_GROUNDED", "4"))
MIN_MECHANISTIC = float(os.environ.get("RESEARCH_MIN_MECHANISTIC", "3"))
PLATEAU_K = int(os.environ.get("RESEARCH_PLATEAU_K", "2"))
# 采纳阈值：单次评测分数上的0.005远小于采样噪声，会把噪声当成提升。
# 全量评测定死为IMPROVE_EPS；子集评测挂到噪声上max(IMPROVE_EPS, se(n))，见adopt_threshold()。
IMPROVE_EPS = float(os.environ.get("RESEARCH_IMPROVE_EPS", "0.01"))
KEEP_CKPT = int(os.environ.get("RESEARCH_KEEP_CKPT", "2"))
# Golden Init 占总预算比例（2026-09-09 用户决策：10h 下 = 80 分钟总窗口，即三路
# specialist 并行各 36 + synthesis 保底 44；内部窗口在 step0_golden_init 直接定值，
# 不再层层比例推导，LIT_MIN/BASELINE_FRAC 两个旋钮随之取消）
GOLDEN_FRAC = float(os.environ.get("RESEARCH_GOLDEN_FRAC", "0.1333"))
RESERVE_FRAC = float(os.environ.get("RESEARCH_RESERVE_FRAC", "0.08"))
# 单节点硬性 kill 默认关闭：节点超出自己的墙钟上限不再被 SIGKILL，让它自然跑完/自行收尾。
# 唯一保留的硬停是全局墙钟（605m timeout + _on_term 收尾保存 final_model）。
# 需要恢复单节点强杀时设 RESEARCH_NODE_HARD_KILL=1。
NODE_HARD_KILL = os.environ.get("RESEARCH_NODE_HARD_KILL", "0") == "1"
MAX_NODES = int(os.environ.get("RESEARCH_MAX_NODES", "40"))
JOURNAL_TAIL = int(os.environ.get("RESEARCH_JOURNAL_TAIL", "8"))
USE_AGENTS = os.environ.get("RESEARCH_USE_AGENTS", "1") == "1"
API_RETRIES = int(os.environ.get("RESEARCH_API_RETRIES", "3"))
API_BACKOFF_S = int(os.environ.get("RESEARCH_API_BACKOFF_S", "90"))
API_GIVEUP = int(os.environ.get("RESEARCH_API_GIVEUP", "6"))

# 统一尺子（ruler）：循环阶段所有评测都用官方 `evaluate.py` 本身（它自带 --limit /
# --max-tokens / --max-connections / --gpu-memory-utilization），不再自建 dev 评测器——
# 自建实现复现不了官方语义（取小行为、采样兜底）曾直接烧掉过一次完整 run。
# 尺子在 Golden Init 一次性定好（流程记录.md：能不切就不切，一般用全量；
# 全量太贵才退固定子集，且子集不能太小），全程不得更换，否则历史节点不可比。
RULER_N = int(os.environ.get("RESEARCH_RULER_N", "300"))
RULER_MIN_N = int(os.environ.get("RESEARCH_RULER_MIN_N", "200"))
# 官方 evaluate.py 的兜底抽样数：只在 Golden Init 的 baseline 窗口装不下全量时才允许退到
# 它（记 eval_mode=official_subset）。循环阶段的抽样大小由尺子统一决定，不用这个值。
OFFICIAL_CHECK_LIMIT = int(os.environ.get("RESEARCH_OFFICIAL_CHECK_LIMIT", "150"))
# Golden Run：Golden Init 之后**不进循环**，先直接执行 synthesis 写出的完整配方，
# 把主干建起来（流程记录.md「黄金的流程」：初步 SFT 建立稳定策略 → 才进入
# "评测 → bad case → 单机制修改 → 回滚/晋级"的循环）。
# 依据（1509 条轨迹）：① 10h 预算对 4B 是宽松的——控制 agent 身份后墙钟对分数几乎没有
# 解释力（Top-5 agent 内部 rho=+0.028, p=0.63），稀缺的是"知道该做什么"而不是时间；
# ② 11.5% 的 run 交不出可评测模型、9.7% 得 0 分，开局把配方与交付管道一次跑通的收益
# 远高于用机制测试探路。
# 打分用官方 evaluate.py **全量**（--limit -1），与 Golden Init 基线同刻度，Δ 直接可比。
# 失败不重试：如实记进 research/golden_run.json，注入后续所有节点当先验，然后照旧
# 落回 bootstrap regime 走大步（安全网）。
GOLDEN_RUN = os.environ.get("RESEARCH_GOLDEN_RUN", "1") == "1"
GOLDEN_RUN_FRAC = float(os.environ.get("RESEARCH_GOLDEN_RUN_FRAC", "0.35"))
# Golden Init 的失败分桶是粗桶（推理错/格式错/截断/抽取失败/复读），不足以支撑
# "并行候选各守一个坐标"。默认在第一轮猜想之前先做一次 Measurement 把分面铺开。
MEASURE_FIRST = os.environ.get("RESEARCH_MEASURE_FIRST", "1") == "1"
# 度量升级要重跑尺子评测，不便宜。全程最多做 MAX_MEASURE 次；除了 judge 明确把问题
# 归因到度量侧（measure_pending）以外，自动触发还要求距上次度量至少又跑了
# MEASURE_GAP 个实验，否则每轮都会重跑一次把预算吃光。
MAX_MEASURE = int(os.environ.get("RESEARCH_MAX_MEASURE", "4"))
MEASURE_GAP = int(os.environ.get("RESEARCH_MEASURE_GAP", "2"))

_DEADLINE = None          # 由 main() 按 timer.sh 设定
_FINALIZED = threading.Event()

# ---------------------------------------------------------------- 1. 基础设施
def log(msg: str) -> None:
    print(f"[orch {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


# ---- Markdown frontmatter 取值：节点产物现在都是 Markdown+YAML frontmatter（2026-09-17
# prompt 改版），check_contract 会把 frontmatter 解析进返回对象。下面几个小工具把
# frontmatter 里的单值折成下游逻辑沿用的类型/形状，容忍 LLM 偶尔把数字写成字符串。
def fm_float(obj, key) -> float | None:
    v = (obj or {}).get(key)
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+(?:\.\d+)?", v)
        if m:
            try:
                return float(m.group(0))
            except ValueError:
                return None
    return None


def fm_int(obj, key, default=None):
    f = fm_float(obj, key)
    return int(f) if f is not None else default


def fm_bool(obj, key, default=False) -> bool:
    v = (obj or {}).get(key)
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1", "是")
    return default


def metrics_from_frontmatter(obj) -> dict | None:
    """把 result/baseline 的 frontmatter（score/eval_mode/n）折成下游沿用的 metrics
    字典——score_of / eval_n_of / _scale_of / adopt_threshold 全吃这个形状。"""
    score = fm_float(obj, "score")
    if score is None:
        return None
    return {"accuracy": score, "eval_mode": (obj or {}).get("eval_mode"),
            "n": fm_int(obj, "n")}


def result_from_md(obj) -> dict:
    """把 Engineer_1 的 Markdown frontmatter（status/score/eval_mode/n/elapsed_h/
    model_path）折成 step5/record_golden_run 沿用的 result 形状。正文当作实测记述
    存进 what_actually_happened，供日志与后续节点回读。"""
    obj = obj or {}
    metrics = metrics_from_frontmatter(obj)
    elapsed_h = fm_float(obj, "elapsed_h")
    body = str(obj.get("body") or "").strip()
    return {"status": obj.get("status") or ("ok" if metrics else "failed"),
            "metrics_dev": metrics,
            "model_path": obj.get("model_path"),
            "elapsed_h": elapsed_h,
            "wall_min": int(elapsed_h * 60) if elapsed_h is not None else None,
            "what_actually_happened": body[:2000] or None,
            "body": body}


def set_phase(name: str) -> None:
    """守卫脚本按阶段决定是否放行占卡命令。"""
    PHASE_F.parent.mkdir(parents=True, exist_ok=True)
    PHASE_F.write_text(name, encoding="utf-8")


def timer_remaining_h() -> float:
    """解析 harness 的 timer.sh。拿不到就退回 NUM_HOURS，再不行给个保守值。"""
    t = TASK_DIR / "timer.sh"
    if t.is_file():
        try:
            out = subprocess.run(["bash", str(t)], capture_output=True, text=True,
                                 timeout=30).stdout
            if "expired" in out.lower():
                return 0.0
            m = re.search(r"(\d+):(\d{2})", out)
            if m:
                return int(m.group(1)) + int(m.group(2)) / 60.0
        except Exception as exc:
            log(f"WARN 读 timer.sh 失败: {exc!r}")
    try:
        return float(os.environ.get("NUM_HOURS", "10"))
    except ValueError:
        return 10.0


def remaining_h() -> float:
    if _DEADLINE is None:
        return timer_remaining_h()
    return max(0.0, (_DEADLINE - time.time()) / 3600.0)


def benchmark_name() -> str:
    f = REPO_ROOT / "src" / "eval" / "tasks" / TASK / "benchmark.txt"
    try:
        return f.read_text(encoding="utf-8").strip()
    except Exception:
        return TASK

# ---------------------------------------------------------------- 2. 状态
def default_state() -> dict:
    return {"created": now_iso(), "task": TASK, "model": MODEL, "cli": CLI,
            "cli_model": CLI_MODEL,
            "nodes": [], "best": {"node": None, "score": None, "model": None},
            "baseline": None, "no_improve_streak": 0, "reignite_count": 0,
            "round": 0, "rejected_rounds": 0, "consec_rejects": 0,
            # Step1 Measurement 的触发账本：pending 由 judge/实验结果置位，
            # anchor 记录上次做完度量时的实验节点数，避免同一状态反复触发。
            "measure_pending": MEASURE_FIRST,
            "measure_reason": ("Golden Init 的失败分桶是粗桶，先把分面铺开再提猜想"
                              if MEASURE_FIRST else None),
            "measure_anchor": -1}


def load_state() -> dict:
    s = read_json(STATE_F)
    return s if isinstance(s, dict) else default_state()


def save_state(s: dict) -> None:
    write_json(STATE_F, s)


def node_dir(nid: str) -> Path:
    d = NODES / nid
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_nid(state: dict, kind: str) -> str:
    """单调序号而不是节点数：被否掉的候选也要占号，否则它们的轨迹会被下一轮覆盖。"""
    state["seq"] = state.get("seq", 0) + 1
    save_state(state)
    return f"n{state['seq']:03d}-{kind}"


def journal_append(text: str) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n\n")


def journal_for_prompt(state: dict) -> str:
    """只注入最近 N 个节点的压缩记录，避免后期输入 token 爆炸。

    golden_run 一并注入：它是主干的来源，后续每一轮都需要知道"起点是怎么来的、
    配方哪部分已经验证过"，否则猜想会重复提已经在开局做过的事。
    """
    recs = [n for n in state["nodes"]
            if n.get("kind") in ("exp", "golden_run")][-JOURNAL_TAIL:]
    if not recs:
        return "（还没有实验记录。）"
    lines = [f"（完整日志见 `research/journal.md`；这里是最近 {len(recs)} 个实验节点）"]
    for n in recs:
        lines.append(
            f"- `{n['id']}` [{n.get('layer', '?')}/{n.get('regime', '?')}] "
            f"{n.get('target', '?')}｜{n.get('title', '')}｜"
            f"score {n.get('score')}（Δ{n.get('delta')}，采纳阈值 {n.get('adopt_threshold')}，"
            f"{n.get('eval_mode', '?')} n={n.get('eval_n')}）｜"
            f"{n.get('verdict', '?')}｜采纳={n.get('adopted')}"
            + (f"｜失败原因: {n.get('note')}" if n.get("note") else ""))
    return "\n".join(lines)


def tried_targets(state: dict) -> dict:
    """坐标 -> {"n": 次数, "adopted": 是否有过采纳}。用于坐标分配与"已试过"硬规则。"""
    out: dict[str, dict] = {}
    for n in state["nodes"]:
        if n.get("kind") != "exp" or not n.get("target"):
            continue
        rec = out.setdefault(n["target"], {"n": 0, "adopted": False})
        rec["n"] += 1
        rec["adopted"] = rec["adopted"] or bool(n.get("adopted"))
    return out


def inject_json(obj) -> str:
    """注入提示词用的紧凑 JSON（2026-09-09 用户决策）：比 indent=2 省 20-30% 输入
    token。只去掉结构缩进，字符串字段（尺子命令、配方步骤）逐字保留；
    落盘产物仍走 write_json 的 indent=2，保持人可读。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def best_summary(state: dict) -> str:
    return inject_json({"baseline": state.get("baseline"), "best": state.get("best"),
                        "no_improve_streak": state.get("no_improve_streak"),
                        "reignite_count": state.get("reignite_count"),
                        "remaining_hours": round(remaining_h(), 2)})

# ---------------------------------------------------------------- 3. 节点调用
def render(text: str, ctx: dict) -> str:
    for k, v in ctx.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def cur_phase() -> str:
    try:
        return PHASE_F.read_text(encoding="utf-8").strip()
    except Exception:
        return "unknown"


def ruler_of_run() -> dict | None:
    """本 run 定好的统一尺子。golden_init.json 是最终裁决（synthesis 消化
    protocol.levers + baseline 实测命令后定稿）。Golden Init 期间尺子还不存在，
    返回 None——那阶段的评测纪律只说定稿规则，不再把 protocol 的建议命令
    假装成已经冻结的尺子。"""
    obj = read_json(RES / "golden_init.json") or {}
    r = obj.get("ruler")
    if isinstance(r, dict) and r.get("mode") in ("full", "fixed"):
        return r
    return None


def eval_policy_text() -> str:
    """评测纪律。所有节点共享同一段文字，避免各节点各自发明 --limit。"""
    ruler = ruler_of_run()
    if ruler is None:
        # Golden Init 三路并行期间：尺子尚未定稿，只能说定稿规则
        return (
            f"- Golden Init 阶段：baseline 直接用官方 `evaluate.py --limit -1` **全量**跑，\n"
            f"  与交付评测同刻度；只有时间窗口装不下时才退 `--limit {OFFICIAL_CHECK_LIMIT}`\n"
            f"  （记 `eval_mode: \"official_subset\"`）。不要自己写评测脚本。\n"
            f"- 循环阶段的**统一尺子**由 synthesis 在 Golden Init 收尾时定好并写进\n"
            f"  `golden_init.json`：全量优先；全量太贵才退固定子集，且 n ≥ {RULER_MIN_N}。\n"
            f"- 尺子就是官方 `evaluate.py` 加固定参数，不存在自建 dev 评测器。\n"
            f"- 每份 metrics 都必须带 `n`（实际评了多少条）与 `eval_mode`。\n"
        )
    mode, n, cmd = ruler.get("mode"), ruler.get("n"), ruler.get("cmd") or ""
    scale = (f"official_full" if mode == "full" else f"official_subset（n 固定为 {n}）")
    return (
            f"- **本 run 循环阶段的统一尺子（Golden Init 定好，全程不得更换）**：\n"
            f"  `{cmd}`\n"
            f"  每次评测**逐字照抄**这条命令，只换 `--model-path` 与 `--json-output-file`。\n"
            f"  `--limit` / `--max-tokens` / `--max-connections` / `--gpu-memory-utilization`\n"
            f"  （以及 cmd 里出现的其它 flag）一个都不许临场改——速度参数已经按\n"
            f"  protocol.levers + baseline 实测写进尺子，再摸就是换尺子。\n"
            f"- 尺子刻度：{scale}。metrics 必须带 `n` 与 `eval_mode`（`official_full` |\n"
            f"  `official_subset`），且 `n` 必须等于尺子的 n——**不许临场自选一个数**。\n"
            f"  全量与子集是两把刻度不同的尺子，Δ 只能和同刻度的历史节点比。\n"
            f"- 逐样本结果在官方日志 `logs/`（取**最新一份**做分析）；分面指标用\n"
            f"  `research/scripts/diagnose.py`（Step1 Measurement 构建）在日志上计算，\n"
            f"  不要为此再跑新评测。\n"
            f"- 例外：**Golden Run 主干节点**用官方 `evaluate.py --limit -1` **全量**打分\n"
            f"  （与 Golden Init 基线同刻度），速度参数仍照抄上面这条 cmd 里的取值。\n"
        )


def protocol_for_prompt() -> str:
    """协议事实注入。产物是 markdown（见 PROTOCOL_SECTIONS），原样贴进 prompt。"""
    f = RES / PROTOCOL_MD
    if f.is_file() and f.stat().st_size:
        return f.read_text(encoding="utf-8", errors="replace").strip()
    return ("（`research/protocol.md` 还不存在或为空：协议 specialist 没产出。"
            "需要协议事实时自己读 `evaluate.py` / `templates/` 确认，不要凭印象假设。）")


def build_prompt(step_file: str, node_d: Path, contract: Path, extra: dict,
                 state: dict, timeout_min: float, phase: str | None = None) -> str:
    # 并行节点不能依赖共享的 .phase 推断自己的阶段；调用方直接传入才没有竞态。
    phase = phase or cur_phase()
    # benchmark 相关约定（评测命令形状、协议源码位置）从 assets/benchmark_profile.json 读，
    # 不再写死在 prompt 里——切 benchmark/harness 时只改那一份。缺文件时退回 PostTrainBench 默认。
    bp = read_json(RES / "benchmark_profile.json") or {}
    eval_cmd_hint = str(bp.get("eval_cmd_hint")
                        or "python evaluate.py --model-path {{MODEL}} ...").replace("{{MODEL}}", str(MODEL))
    protocol_sources = "、".join(bp.get("protocol_sources")
                                 or ["evaluate.py", "templates/", "Scorer 实现", "相关配置文件"])
    base = {
        "TASK_DIR": TASK_DIR, "NODE_DIR": node_d, "MODEL": MODEL,
        "BENCHMARK": benchmark_name(), "NODE_TIMEOUT_MIN": round(timeout_min, 2),
        "REMAINING_H": round(remaining_h(), 2),
        # 子进程直接继承宿主环境变量（不设 GPU 租约，任何阶段都可以用自己的卡）
        "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
        "PROTOCOL": protocol_for_prompt(),
        "BEST_SUMMARY": best_summary(state),
        "JOURNAL": journal_for_prompt(state),
        "ACTION_SPACE": inject_json(
            json.loads((RES / "action_space.json").read_text(encoding="utf-8"))),
        "STEP_POLICY": inject_json(
            json.loads((RES / "step_policy.json").read_text(encoding="utf-8"))),
        "EVAL_POLICY": eval_policy_text(),
        "EVAL_CMD_HINT": eval_cmd_hint, "PROTOCOL_SOURCES": protocol_sources,
        "RULER_N": RULER_N, "RULER_MIN_N": RULER_MIN_N,
        "OFFICIAL_CHECK_LIMIT": OFFICIAL_CHECK_LIMIT,
        # 本机操作手册目录（skills/）：需要的节点按触发条件给单行指针
        "SKILLS_DIR": AGENT_DIR / "skills",
        "CONTRACT_PATH": contract,
        "WORKFLOW_OVERVIEW_PATH": "research/WORKFLOW_OVERVIEW.md",
    }

    # 每个节点的 prompt 都是自足的（2026-09-10 用户决策：删掉 common_context.md）——
    # 只注入本节点真的用得上的占位符，用不到的键渲染时自然不出现。
    body = (PROMPTS / step_file).read_text(encoding="utf-8")
    ctx = dict(base)
    ctx.update(extra)
    return render(body, ctx)


def child_env(node_d: Path, phase: str, timeout_min: float) -> dict:
    """节点子进程环境。不设 GPU 租约：阶段只决定调度，不摘卡（2026-09-11 用户决策）。"""
    env = dict(os.environ)
    env["RESEARCH_DIR"] = str(RES)
    # guard 优先读取进程自己的阶段。共享 .phase 只保留作人工审计和旧版本兜底；
    # Golden Init specialist 并行时不能让最后一次写 .phase 的节点替其它节点做权限决定。
    env["RESEARCH_PHASE"] = phase
    env["IS_SANDBOX"] = "1"
    # 节点内单条 Bash 命令的超时与节点墙钟对齐（2026-09-07 用户决策：删掉 240 分钟
    # 的硬上限——4h+ 的训练命令曾会被腰斩；官方各 agent 的 solve.sh 也都设 10h）。
    env["BASH_MAX_TIMEOUT_MS"] = str(int(timeout_min * 60 * 1000))
    return env


def cli_cmd(node_d: Path, phase: str, deny_tools: list[str] | None,
            with_agents: bool) -> tuple[list[str], dict]:
    """按 RESEARCH_CLI 组装命令。两个后端都用 JSONL 事件流，产物契约走文件，与 CLI 无关。"""
    web_ok = not (deny_tools and "WebSearch" in deny_tools)
    if CLI == "codex":
        # 与 agents/codex/solve.sh 对齐：--search exec --json --skip-git-repo-check --yolo
        cmd = [CODEX]
        if web_ok:
            cmd += ["--search"]
        cmd += ["exec", "--json", "--skip-git-repo-check", "--yolo",
                "-c", "model_reasoning_summary=detailed",
                "-c", f"model_reasoning_effort={CODEX_EFFORT}"]
        if CLI_MODEL:
            cmd += ["--model", CLI_MODEL]
        return cmd, {}
    cmd = [CLAUDE, "--print", "--verbose", "--output-format", "stream-json",
           "--effort", EFFORT, "--dangerously-skip-permissions",
           "--settings", str(SETTINGS), "--session-id", str(uuid.uuid4())]
    if CLI_MODEL:
        cmd += ["--model", CLI_MODEL]
    if deny_tools:
        cmd += ["--disallowedTools", ",".join(deny_tools)]
    if with_agents and USE_AGENTS:
        cmd += ["--agents", (ASSETS / "subagents.json").read_text(encoding="utf-8")]
    return cmd, {"CLAUDE_CONFIG_DIR": str(node_d / ".cc")}


def run_cli(prompt: str, node_d: Path, label: str, timeout_min: float, phase: str,
            deny_tools: list[str] | None = None,
            with_agents: bool = False, timeout_s: int | None = None,
            dry_extra: dict | None = None) -> tuple[int, str | None]:
    """跑一个节点进程。事件流全量落盘，作为轨迹采集的原始数据。
    返回 (rc, err_kind)；err_kind == "api" 表示网关侧错误，值得重试而不是判节点失败。"""
    stream = node_d / f"{label}.stream.jsonl"
    (node_d / f"{label}.prompt.md").write_text(prompt, encoding="utf-8")
    if DRY:
        return dry_stub(label, node_d, prompt, dry_extra or {}), None

    cmd, extra_env = cli_cmd(node_d, phase, deny_tools, with_agents)
    env = child_env(node_d, phase, timeout_min)
    env.update(extra_env)
    if "CLAUDE_CONFIG_DIR" in env:
        Path(env["CLAUDE_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    n_ev = 0
    last_result = None
    saw_api_err = False
    with stream.open("w", encoding="utf-8") as sf:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, cwd=str(TASK_DIR), env=env,
                                text=True, bufsize=1, start_new_session=True)

        def kill_process_group() -> None:
            """超时后杀掉 CLI 及它启动的 shell/下载器，避免后台进程突破节点预算。"""
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        # 硬性节点 kill 已默认取消（见 NODE_HARD_KILL）：不再在节点墙钟上限 SIGKILL 进程组，
        # 让节点自然跑完 / 自行收尾；唯一硬停是全局墙钟与 _on_term 收尾保存 final_model。
        killer = None
        if NODE_HARD_KILL:
            killer = threading.Timer(timeout_s or timeout_min * 60, kill_process_group)
            killer.daemon = True
            killer.start()
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
            for line in proc.stdout:
                sf.write(line)
                n_ev += 1
                # 只认像"事件级错误"的行；agent 自己命令输出里出现同样字样不算
                if ('"type":"error"' in line
                        or "overloaded_error" in line
                        or ("all nodes exhausted" in line and '"error"' in line)):
                    saw_api_err = True
                # claude: {"type":"result",...}；codex: {"type":"turn.completed"|"turn.failed",...}
                if '"type":"result"' in line or '"type":"turn.' in line or '"type":"error"' in line:
                    try:
                        last_result = json.loads(line)
                    except Exception:
                        pass
            rc = proc.wait()
        finally:
            if killer is not None:
                killer.cancel()

    err_kind = "api" if saw_api_err else None
    cost = None
    if isinstance(last_result, dict):
        cost = last_result.get("total_cost_usd")
        if last_result.get("type") in ("turn.failed", "error"):
            err_kind = "api"          # codex 侧的 turn 失败基本都是网关/模型侧问题
        usage = last_result.get("usage") or {}
        if usage.get("input_tokens") is not None and not cost:
            cost = (f"in {usage.get('input_tokens')}/out {usage.get('output_tokens')} tok")
        if last_result.get("is_error") and (last_result.get("api_error_status")
                                            or "API Error" in str(last_result.get("result", ""))):
            err_kind = "api"
    if n_ev == 0 and rc != 0:
        err_kind = err_kind or "api"     # 连事件都没有，基本是网关/启动层问题
    log(f"  {label}: rc={rc} events={n_ev} {int(time.time() - t0)}s"
        + (f" usage={cost}" if cost else "") + (f" ERR={err_kind}" if err_kind else "")
        + f" -> {stream.name}")
    return rc, err_kind


def dry_stub(label: str, node_d: Path, prompt: str, extra: dict) -> int:
    """RESEARCH_DRY_RUN=1：不调 API，按新版 Markdown+frontmatter 契约造最小合法产物，
    用来验证编排逻辑本身。"""
    def _md(fm: dict, body: str) -> str:
        head = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items())
        return f"---\n{head}\n---\n\n{body.strip()}\n"

    ruler_cmd = ("python evaluate.py --model-path <model> --limit -1 "
                 "--max-connections 64 --gpu-memory-utilization 0.85 --json-output-file <out>")

    if label == "golden_protocol":
        body = "\n\n".join([f"# 官方评测协议备忘录（dry / {TASK}）"]
                           + [f"{h}\n- dry stub（evaluate.py:1）" for h in PROTOCOL_SECTIONS])
        (RES / PROTOCOL_MD).write_text(body + "\n", encoding="utf-8")
        return 0

    if label == "golden_baseline":
        fm = {"status": "ok", "score": 0.30, "eval_mode": "official_full",
              "n": 1319, "elapsed_min": 12.0}
        body = ("# 零样本基线评测报告\n\n## 1. 评测执行与得分\n"
                f"- 执行命令: `python evaluate.py --model-path {MODEL} --limit -1 "
                "--max-connections 64 --gpu-memory-utilization 0.85`\n"
                "- 基线得分: 0.30 (n=1319)\n\n"
                "## 2. 失败原因分桶与占比\n- 格式错误: ~30%\n- 推理错误: ~40%\n\n"
                "## 3. 代表性 Bad Cases\n- dry：答案已算出但未落入提取框。\n")
        (RES / "baseline.md").write_text(_md(fm, body), encoding="utf-8")
        return 0

    if label == "golden_literature":
        body = ("# 文献与数据调研报告\n\n## 1. 核心候选数据集\n- `org/dry-math`：dry。\n\n"
                "## 2. 训练范式与方法参考\n- dry：全参 SFT 冷启动。\n\n"
                "## 3. 其他参考建议\n- dry。\n")
        (RES / "literature.md").write_text(body, encoding="utf-8")
        return 0
    if label == "golden_synthesis":
        fm = {"status": "ok", "n": -1, "ruler_cmd": ruler_cmd}
        body = ("# Golden Recipe 与初始化主干规划\n\n## 1. 综合诊断与决策依据\n"
                "- dry：主要瓶颈是格式对齐。\n\n## 2. 统一评测尺子 (Ruler)\n"
                f"- 评测指令: `{ruler_cmd}`\n\n## 3. 数据方案\n- dry：~30000 条，去污染。\n\n"
                "## 4. 训练与解码配置\n- 全参 SFT，lr=1e-5，epochs=2；四键解码写全。\n\n"
                "## 5. 完整执行工单步骤 (Steps)\n```bash\n# 1. 准备数据\n# 2. 训练\n"
                f"# 3. 评测\n{ruler_cmd}\n```\n")
        (node_d / "golden_synthesis.md").write_text(_md(fm, body), encoding="utf-8")
        return 0

    if label == "measurement":
        fm = {"status": "ok", "focus_dimension": "format_error",
              "suggested_targets": ["Inference.Hyperparams", "Training.Data"]}
        body = ("# 深度行为诊断与度量报告\n\n## 1. 现象细分与量化表现\n"
                "- dry：短序列 50% vs 长序列截断 65%。\n\n## 2. 瓶颈归因与机制洞察\n"
                "- dry：多跳推导提前收尾、未输出终止符。\n\n"
                "## 3. 对下一轮猜想的坐标与靶点建议\n- 建议 `Inference.Hyperparams`。\n")
        (node_d / "measurement.md").write_text(_md(fm, body), encoding="utf-8")
        (RES / "scripts").mkdir(parents=True, exist_ok=True)
        (RES / "scripts" / "diagnose.py").write_text(
            "#!/usr/bin/env python3\n# dry facet script\n", encoding="utf-8")
        return 0

    if label == "experiment":
        fm = {"status": "ok", "score": 0.55, "eval_mode": "official_full",
              "n": 1319, "elapsed_h": 0.05, "model_path": str(node_d / "model"),
              "preflight_passed": True}
        body = ("# 实验落地与综合实测报告\n\n## 1. 实验方案与控制变量\n- dry：单变量控制。\n\n"
                "## 2. 运行前工程与配置自查 (Preflight)\n- 语法与双 EOS: 正常。\n\n"
                "## 3. 评测指标与结果分析\n- 官方全量 0.55 (n=1319)。\n\n"
                "## 4. 意外发现与现象记录 (Surprises)\n- dry。\n")
        (node_d / "model").mkdir(exist_ok=True)
        write_json(node_d / "model" / "config.json", {"architectures": ["Qwen3ForCausalLM"]})
        (node_d / "experiment.md").write_text(_md(fm, body), encoding="utf-8")
        return 0
    if label.startswith("hypo"):
        idx = int(label.split("-")[-1])
        allowed = str(extra.get("ALLOWED_LAYERS", "exec/strategy"))
        exec_t = [{"domain": "Inference", "module": "Decoding"},
                  {"domain": "Inference", "module": "Hyperparams"},
                  {"domain": "Training", "module": "Hyperparams"},
                  {"domain": "Harness", "module": "Prompt"}]
        strat_t = [{"domain": "Training", "module": "Data"},
                   {"domain": "Training", "module": "Method"},
                   {"domain": "Model", "module": "ExternalModule"}]
        if "strategy" in allowed and "exec" not in allowed:
            pool = strat_t
        elif "exec" in allowed and "strategy" not in allowed:
            pool = exec_t
        else:
            pool = exec_t + strat_t
        target = pool[idx % len(pool)]
        fm = {"status": "ok", "targets": [target],
              "abstain": False, "cost_estimate_h": round(0.2 + 0.1 * idx, 2)}
        body = ("# 科学假说与干预设计\n\n## 1. 现象观察与支撑证据\n"
                "- dry：最新评测里 32/100 条判为 format_error。\n\n"
                f"## 2. 机制假说\n- dry：{target['domain']}.{target['module']} 上的干预降低格式错误率。\n\n"
                "## 3. 干预变量与受控设计\n- 干预: dry；受控: 其他超参不变。\n\n"
                "## 4. 证伪条件与预期指标\n- 证伪: 统一尺子得分未提升。\n")
        (node_d / "hypothesis.md").write_text(_md(fm, body), encoding="utf-8")
        return 0

    if label == "judge":
        fm = {"status": "ok", "winner_index": 0, "winner_file": None,
              "insufficient_reason": None}
        body = ("# 猜想裁决与比选报告\n\n## 1. 候选方案证据核验\n- Candidate 0：证据真实。\n\n"
                "## 2. 方案对比 (Head-to-Head Comparison)\n- 选 Candidate 0。\n\n"
                "## 3. 最终裁决\n- 选定 Candidate 0。\n")
        (node_d / "judge.md").write_text(_md(fm, body), encoding="utf-8")
        return 0

    if label == "archive":
        fm = {"status": "ok", "node_id": extra.get("NODE_ID"),
              "target": extra.get("TARGET_KEY"), "adopted": True,
              "score": 0.63, "delta": 0.08}
        body = ("# 猜想-实验对归档卡片\n\n## 1. 猜想与机制假说\n- dry：格式对齐假说。\n\n"
                "## 2. 实验方案概述\n- dry：单坐标干预。\n\n"
                "## 3. 实验结果与验证判据\n- dry：得分 0.63（Δ+0.08），采纳。\n")
        (node_d / "archive_card.md").write_text(_md(fm, body), encoding="utf-8")
        return 0
    if label == "golden_run":
        (node_d / "model").mkdir(exist_ok=True)
        write_json(node_d / "model" / "config.json", {"architectures": ["Qwen3ForCausalLM"]})
        model_path = str(node_d / "model")
        if os.environ.get("RESEARCH_DRY_GOLDEN_FAIL") == "1":
            shutil.rmtree(node_d / "model", ignore_errors=True)
            fm = {"status": "failed", "score": None, "eval_mode": "official_full",
                  "n": 0, "elapsed_h": 0.02, "model_path": None}
            body = ("# 实验执行与实测打分报告\n\n## 1. 实验落地概况\n- dry：SFT 在 step 40 OOM。\n\n"
                    "## 2. 评测指标与结果\n- 未产出模型。\n\n"
                    "## 3. 意外发现与现象记录 (Surprises)\n- dry：max_seq_len 4096 装不下。\n\n"
                    "## 4. 防污染与合规自查\n- 未执行。\n")
            (node_d / "result.md").write_text(_md(fm, body), encoding="utf-8")
            return 0
        acc, n = 0.55, 1319
        fm = {"status": "ok", "score": acc, "eval_mode": "official_full", "n": n,
              "elapsed_h": 0.02, "model_path": model_path}
        body = ("# 实验执行与实测打分报告\n\n## 1. 实验落地概况\n- dry：训练收敛。\n\n"
                f"## 2. 评测指标与结果\n- 官方全量 {acc} (n={n})。\n\n"
                "## 3. 意外发现与现象记录 (Surprises)\n- dry。\n\n"
                "## 4. 防污染与合规自查\n- 通过。\n")
        (node_d / "result.md").write_text(_md(fm, body), encoding="utf-8")
        return 0

    return 0

class GatewayDown(RuntimeError):
    """网关连续不可用。继续跑只会空转烧时间，直接收尾更好。"""


def parse_markdown_with_frontmatter(text: str) -> tuple[dict, str]:
    """解析 Markdown 文件中的 YAML Frontmatter 和正文。"""
    meta = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            raw_meta = parts[1].strip()
            body = parts[2].strip()
            try:
                import yaml
                loaded = yaml.safe_load(raw_meta)
                if isinstance(loaded, dict):
                    meta = loaded
            except Exception:
                # 简单键值回退
                for line in raw_meta.splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        meta[k.strip()] = v.strip().strip("\"'")
    return meta, body


def check_contract(contract: Path, required: list[str], text_contract: bool = False):
    """产物文件是唯一的成功判据。

    支持 Markdown 契约（带 YAML Frontmatter 或纯小节）与 JSON 契约。
    编排器是辅助不是监管：返回值只决定是否带着反馈再试一次。
    `text_contract=True` 或后缀为 `.md` 时解析为 Markdown 契约，`required` 为必须出现的标题/锚点。
    """
    is_md = text_contract or contract.suffix.lower() == ".md"
    if is_md:
        if not contract.is_file() or contract.stat().st_size == 0:
            return None, f"产物文件 `{contract}` 不存在或为空。"
        text = contract.read_text(encoding="utf-8", errors="replace")
        missing = [h for h in required if h not in text]
        if missing:
            return None, f"产物缺少关键小节锚点: {missing}。"
        meta, body = parse_markdown_with_frontmatter(text)
        obj = {
            "status": meta.get("status", "ok"),
            "path": str(contract),
            "text": text,
            "body": body,
            **meta
        }
        return obj, None

    obj = read_json(contract)
    if obj is None:
        return None, f"产物文件 `{contract}` 不存在或不是合法 JSON。"
    missing = [k for k in required if k not in obj]
    if missing:
        return None, f"产物文件缺少必需字段: {missing}。"
    return obj, None


def run_node(step_file: str, node_d: Path, contract: Path, label: str, phase: str,
             required: list[str], state: dict, extra: dict | None = None,
             timeout_min: int = 60, deny_tools: list[str] | None = None,
             with_agents: bool = False, retries: int = 1,
             total_budget_min: int | None = None, text_contract: bool = False):
    """跑一个节点并校验文件契约。

    两种重试是分开的：
      - 网关错误（err=="api"）**且**没拿到产物 -> 原样退避重试，不计入 contract 重试；
      - 拿到产物就立刻成功返回，哪怕过程中出现过网关报错（CLI 自己会重试，
        而且 agent 的命令输出里也可能出现"all nodes exhausted"这类字符串）；
      - 产物不合法 -> 带着失败原因重开一次（计入 retries）。

    重试用完仍不合法时**不判死**（2026-09-10 用户决策）：盘上只要有个能解析的产物，
    就带 `_contract_warning` 原样返回，缺口留给下游节点自己看着办。只有盘上什么都没有、
    或时间/网关真的不允许继续时才返回 None。
    """
    extra = dict(extra or {})
    api_failures = 0
    salvage: dict | None = None
    budget_deadline = (
        time.monotonic() + total_budget_min * 60
        if total_budget_min is not None else None
    )
    feedback = ""
    for attempt in range(retries + 1):
        for api_try in range(API_RETRIES + 1):
            attempt_timeout_s = max(1, int(timeout_min * 60))
            if budget_deadline is not None:
                left_s = budget_deadline - time.monotonic()
                if left_s <= 0:
                    log(f"  {label}: 总时间预算 {total_budget_min} 分钟已耗尽")
                    return None
                attempt_timeout_s = max(1, min(attempt_timeout_s, int(left_s)))
            attempt_timeout = max(1 / 60, attempt_timeout_s / 60)
            set_phase(phase)
            if contract.exists():
                contract.unlink()
            extra["RETRY_FEEDBACK"] = feedback
            prompt = build_prompt(step_file, node_d, contract, extra, state, attempt_timeout,
                                  phase=phase)
            if feedback:
                prompt += ("\n\n---\n\n# 上一次尝试失败\n\n" + feedback +
                           "\n\n这次务必先把产物文件写出来再收尾。")
            tag = label + (f".retry{attempt}" if attempt else "") + (f".api{api_try}" if api_try else "")
            _rc, err = run_cli(prompt, node_d, tag, attempt_timeout, phase,
                               deny_tools=deny_tools, with_agents=with_agents,
                               timeout_s=attempt_timeout_s, dry_extra=extra)
            obj, why = check_contract(contract, required, text_contract)
            if obj is not None:
                return obj
            if contract.is_file() and contract.stat().st_size > 0:
                # 不合法但盘上有东西：先留着，重试都失败就用它继续
                if text_contract:
                    raw = contract.read_text(encoding="utf-8", errors="replace")
                    meta, body = parse_markdown_with_frontmatter(raw)
                    # 把 frontmatter 单值也带上：即便小节锚点缺失，下游仍能读到 score/targets 等
                    salvage = {"status": "partial", "path": str(contract),
                               "text": raw, "body": body, **meta}
                else:
                    parsed = read_json(contract)
                    if isinstance(parsed, dict):
                        salvage = parsed
            if err != "api":
                break                     # 是节点没做好，交给外层带反馈重开
            api_failures += 1
            if api_failures >= API_GIVEUP:
                raise GatewayDown(f"节点 {label} 连续 {api_failures} 次网关错误且拿不到产物")
            if api_try < API_RETRIES:
                backoff_s = API_BACKOFF_S
                if budget_deadline is not None:
                    remaining_s = max(0, int(budget_deadline - time.monotonic()))
                    # 至少给重试本身留一分钟；否则把剩余预算全睡掉没有意义。
                    if remaining_s <= 60:
                        log(f"  {label}: 剩余预算不足一分钟，不再做网关重试")
                        return None
                    backoff_s = min(backoff_s, remaining_s - 60)
                log(f"  网关错误且无产物，{backoff_s}s 后原样重试（{api_try + 1}/{API_RETRIES}）")
                time.sleep(backoff_s)
        feedback = why
        log(f"  契约校验失败({label}, attempt={attempt}): {feedback}")
    if salvage is not None:
        salvage.setdefault("status", "partial")
        salvage["_contract_warning"] = feedback
        # 后续 attempt 开头会 unlink 契约文件，把留存的这版写回盘上，下游注入才读得到
        if not contract.is_file():
            if text_contract:
                contract.write_text(salvage.get("text") or "", encoding="utf-8")
            else:
                write_json(contract, salvage)
        log(f"  {label}: 重试用完仍不合法，按盘上这一版继续（缺口交给下游）：{feedback}")
        return salvage
    return None


# ---------------------------------------------------------------- 4. 各步骤
def validate_literature_contract(obj: dict) -> str | None:
    """文献产物现在是纯 Markdown 报告（prompts/step0_literature_review.md，无 frontmatter）。
    小节锚点已由 check_contract 校验，这里只兜底看正文非空——合规纪律写在 prompt 里，
    产物不再字段化，编排器也就不再机器判合规。"""
    text = str(obj.get("text") or obj.get("body") or "")
    if not text.strip():
        return "literature 报告为空"
    return None


def validate_protocol_md(obj: dict) -> str | None:
    """协议产物的轻检查：只看小节在不在、有没有源码位置引用。

    发现问题也不阻断 run（调用方只把它记成降级项）——协议读不全时下游节点自己会去读
    `evaluate.py`，那比让整个 run 收尾便宜得多。
    """
    text = str(obj.get("text") or "")
    if not text.strip():
        return "protocol.md 为空"
    missing = [h for h in PROTOCOL_SECTIONS if h not in text]
    if missing:
        return f"protocol.md 缺小节 {missing}"
    if not re.search(r"[\w./\\-]+\.(py|jinja|json|txt|yaml|yml):\d+", text):
        return "protocol.md 里没有任何 `文件:行号` 形式的源码位置引用"
    return None


def validate_baseline_contract(obj: dict) -> str | None:
    """baseline 现在是 Markdown+frontmatter（prompts/step0_baseline.md）：单值元数据
    只有 status/score/eval_mode/n/elapsed_min，失败分桶与 bad case 写在正文里交给
    synthesis 阅读，不再字段化。这里只要求主分数可读——它是采纳阈值的锚点来源。
    """
    status = obj.get("status")
    if status not in {"ok", "partial"}:
        return f"baseline.status 必须是 ok/partial，当前为 {status!r}"
    if fm_float(obj, "score") is None:
        return "baseline.frontmatter 缺少可比较的主分数 score"
    return None


def extract_eval_cmd(text: str) -> str:
    """从 Markdown 正文里抓一条完整的 `... evaluate.py ...` 命令（baseline 报告里的
    「执行命令」、synthesis 的工单里都会出现），用于兜底修尺子。抓不到返回空串。"""
    for line in (text or "").splitlines():
        s = line.strip().strip("`").strip()
        if "evaluate.py" in s and s.lower().startswith(("python", "torchrun", "cd ", "./", "python3")):
            return s
    m = re.search(r"([^\n`]*evaluate\.py[^\n`]*)", text or "")
    return m.group(1).strip() if m else ""


def validate_recipe_contract(text: str) -> str | None:
    """配方现在是 synthesis 的 Markdown 正文（prompts/step0_synthesis.md 的「5. 完整
    执行工单步骤」等小节），不再是结构化 JSON。小节锚点由 check_contract 校验，这里只
    兜底确认正文里确实给出了可执行工单（出现代码块或 evaluate.py），否则 Golden Run
    无从执行。跨基准先验（全参 SFT / 显式四键解码 / epochs≤3 等）已写进 prompt 与
    skills/golden-recipe.md，不再由编排器逐字段判。"""
    text = (text or "").strip()
    if not text:
        return "synthesis 正文为空，没有可执行的 Golden Recipe"
    if "```" not in text and "evaluate.py" not in text and "## 5" not in text:
        return "synthesis 正文缺少可执行的工单步骤（## 5 完整执行工单步骤 / 代码块）"
    return None


def validate_ruler(obj) -> str | None:
    """统一尺子的 fail-closed 校验。尺子一旦定下全程不变，所以宁可在这里挡住坏方案。
    刻度以 frontmatter 的 mode/n 为准（synthesis 只给 n 与 ruler_cmd，mode 由编排器
    从 n 推出），cmd 只要求基于官方 evaluate.py——占位符由 repair_ruler 归一化。"""
    if not isinstance(obj, dict):
        return "ruler 必须是对象"
    for key in ("mode", "n", "cmd"):
        if obj.get(key) in (None, ""):
            return f"ruler 缺少 `{key}`"
    mode = str(obj.get("mode"))
    n = obj.get("n")
    if mode not in ("full", "fixed"):
        return f"ruler.mode 必须是 full | fixed，当前 {mode!r}"
    cmd = str(obj.get("cmd"))
    if "evaluate.py" not in cmd:
        return "ruler.cmd 必须基于官方 evaluate.py"
    if mode == "full":
        if not (n == -1 or (isinstance(n, int) and n > 0)):
            return f"mode=full 时 n 必须是 -1 或全量条数，当前 {n!r}"
    else:
        if not isinstance(n, int) or n < RULER_MIN_N:
            return f"固定子集的 n={n!r} 不得小于 {RULER_MIN_N}（子集太小会让噪声淹没信号）"
    return None


def build_ruler(synth: dict, baseline: dict) -> dict:
    """从 synthesis frontmatter（n + ruler_cmd）折出统一尺子 {mode,n,cmd,why}。
    mode 由 n 推出：-1=full，否则 fixed。cmd 缺失时退回 baseline 实测命令。"""
    n = fm_int(synth, "n")
    cmd = str((synth or {}).get("ruler_cmd") or "").strip().strip("`").strip()
    if not cmd:
        cmd = extract_eval_cmd(str((baseline or {}).get("_body") or ""))
    mode = "full" if (n is None or n == -1) else "fixed"
    if n is None:
        n = -1
    return {"mode": mode, "n": n, "cmd": cmd,
            "why": "synthesis frontmatter（n + ruler_cmd）定稿"}


def repair_ruler(obj, protocol_text: str, baseline: dict) -> dict:
    """尺子不合法时补成一把能用的，而不是判废整个 Golden Init（2026-09-10 用户决策）。

    优先沿用 baseline 实测过的完整命令（它跑通过、速度参数也验证过），否则退到官方
    全量的最小写法。刻度一律 full——与 Golden Init 基线同刻度是唯一不能让的。
    """
    obj = obj if isinstance(obj, dict) else {}
    cmd = str(obj.get("cmd") or "").strip()
    if "evaluate.py" not in cmd:
        used = str(((baseline.get("official") or {})).get("eval_cmd_used")
                   or extract_eval_cmd(str(baseline.get("_body") or "")) or "")
        cmd = used if "evaluate.py" in used else (
            "python evaluate.py --model-path <model> --limit -1 "
            "--json-output-file <out>")
    if "--model-path" not in cmd:
        cmd = cmd + " --model-path <model>"
    else:
        cmd = re.sub(r"--model-path\s+\S+", "--model-path <model>", cmd)
    if "--json-output-file" in cmd:
        cmd = re.sub(r"--json-output-file\s+\S+", "--json-output-file <out>", cmd)
    if "--limit -1" not in cmd:
        cmd = re.sub(r"--limit\s+\S+", "--limit -1", cmd) if "--limit" in cmd \
            else cmd + " --limit -1"
    return {"mode": "full", "n": -1, "cmd": cmd,
            "why": (obj.get("why") or "编排器兜底：synthesis 没给出合法尺子，"
                    "沿用 baseline 实测命令的全量刻度")}


def repair_recipe(text: str, ruler: dict, baseline: dict) -> str:
    """synthesis 没给出可执行配方时，兜底出一份最小可跑的 Markdown 工单，而不是让整个
    Golden Init 重来。默认值取自 skills/golden-recipe.md（跨 7 个 bench 成立的先验）。
    兜底配方不见得好，但"跑一次有缺口的主干"永远好过"开局直接收尾"。"""
    text = (text or "").strip()
    base_score = score_of(((baseline or {}).get("official") or {}))
    ruler_cmd = (ruler or {}).get("cmd") or "python evaluate.py --model-path <model> --limit -1"
    steps_block = ("## 5. 完整执行工单步骤 (Steps)\n```bash\n"
                   "# 1. 构造并筛选训练数据（正确性可验证、去污染、长度对齐生成上限，目标 ~30000 条）\n"
                   "# 2. 全参 bf16 SFT：lr=1e-5, cosine, warmup_ratio=0.03, epochs=2, "
                   "effective_bs≈32, max_seq_len=2048, loss 只算 completion\n"
                   "# 3. 写全 generation_config 四键：eos_token_id(数组)/temperature=0.0/"
                   "top_p=1.0/repetition_penalty=1.0\n"
                   "# 4. 交付验证：目录完整性 + vllm serve 起服冒烟\n"
                   "# 5. 官方全量评测（与基线同刻度）\n"
                   f"{ruler_cmd}\n```\n")
    if not text:
        return (f"# Golden Recipe（编排器兜底：synthesis 未给出完整配方）\n\n"
                f"## 1. 综合诊断与决策依据\n- synthesis 正文缺失或不可执行，按 "
                f"skills/golden-recipe.md 的跨基准默认值兜底。\n- 基线主分数：{base_score}\n\n"
                f"## 2. 统一评测尺子 (Ruler)\n- 评测指令：`{ruler_cmd}`\n\n"
                f"## 3. 数据方案\n- 按 research/protocol.md「答案抽取与判分」对齐答案格式。\n\n"
                f"## 4. 训练与解码配置\n- 全参 bf16 SFT，显式写全 generation_config 四键。\n\n"
                f"{steps_block}")
    if "```" not in text and "## 5" not in text:
        return text + "\n\n" + steps_block
    return text


def step0_golden_init(state: dict, budget_min: int) -> bool:
    """并行建立协议、基线表征和外部知识先验，再由编排器合成 Golden Init。

    三个 specialist 写互不重叠的文件。文献节点有独立硬超时，不会因为 baseline
    评测耗时而退化成顺手 curl 几个数据集页面。
    """
    total_min = max(1, budget_min)
    golden_deadline = time.monotonic() + total_min * 60
    # 窗口直接按 10h 总预算定值（2026-09-09 用户决策：不再 20%/50%/2/3 层层比例推导）：
    # 三路 specialist 并行、各 36 分钟硬窗口（并行段墙钟 36），synthesis 拿 deadline
    # 剩余、保底 80-36=44 分钟。总预算偏离 10h 时各窗口随总窗口按 36/80 同比缩放。
    # 36 分钟对三路都够：baseline 官方全量实测 ~12-15 分钟 + 逐样本分析（live 曾
    # 12-20 分钟完成评测段），protocol/literature 历史上从未用满 30 分钟。
    specialist_min = max(1, round(total_min * 36 / 80))
    protocol_min = baseline_min = literature_min = specialist_min
    synthesis_reserve_min = max(1, total_min - specialist_min)
    log("==== Step0 Golden Init（三路并行）"
        f"｜总窗口 {total_min} 分钟｜protocol {protocol_min}｜"
        f"baseline {baseline_min}｜literature {literature_min}｜"
        f"synthesis reserve {synthesis_reserve_min} ====")

    protocol_d = node_dir("n000-golden-protocol")
    baseline_d = node_dir("n000-golden-baseline")
    literature_d = node_dir("n000-golden-literature")

    jobs = [
        lambda: run_node(
            "step0_protocol.md", protocol_d, RES / PROTOCOL_MD,
            "golden_protocol", "golden_protocol",
            PROTOCOL_SECTIONS,
            state, timeout_min=protocol_min,
            deny_tools=["WebSearch", "WebFetch"], retries=1,
            total_budget_min=protocol_min, text_contract=True),
        lambda: run_node(
            "step0_baseline.md", baseline_d, RES / "baseline.md",
            "golden_baseline", "golden_baseline",
            ["## 1. 评测执行与得分", "## 2. 失败原因分桶与占比"],
            state, timeout_min=baseline_min,
            deny_tools=["WebSearch", "WebFetch"], with_agents=True, retries=1,
            total_budget_min=baseline_min, text_contract=True),
        lambda: run_node(
            "step0_literature_review.md", literature_d, RES / "literature.md",
            "golden_literature", "golden_literature",
            ["## 1. 核心候选数据集", "## 2. 训练范式与方法参考"],
            state, timeout_min=literature_min, retries=1,
            total_budget_min=literature_min, text_contract=True),
    ]
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = [ex.submit(job) for job in jobs]
        protocol, baseline, literature = [f.result() for f in futures]

    # 2026-09-10 用户决策：编排器是辅助不是监管。Golden Init 里**没有一处**因为"缺了
    # 什么"而收尾——缺的东西记成降级项注入后续节点，让它们自己补；唯一的硬边界是时间。
    degradations: list[str] = []

    def degrade(msg: str):
        log(f"Golden Init 降级：{msg}")
        degradations.append(msg)

    if not protocol:
        degrade("protocol specialist 无产出——下游节点自己读 evaluate.py 对齐协议")
        protocol = {"status": "missing", "text": ""}
    if not baseline:
        degrade("baseline specialist 无产出——没有零样本基线，采纳阈值只能靠后续节点自建锚点")
        baseline = {"status": "missing"}
    if not literature:
        degrade("literature specialist 无产出——没有外部数据/范式证据，配方只能靠本地现象")
        literature = {"status": "missing"}

    for name, component, validator in (
        ("protocol", protocol, validate_protocol_md),
        ("baseline", baseline, validate_baseline_contract),
        ("literature", literature, validate_literature_contract),
    ):
        if component.get("status") == "missing":
            continue
        error = validator(component)
        if error:
            degrade(f"{name} 产物有缺口：{error}")

    # baseline 折成下游沿用的内部形状：official 主分数是采纳阈值的锚点，_body 供修尺子抓命令。
    if baseline.get("status") == "missing":
        baseline_dict = {"status": "missing", "official": {}}
    else:
        b_body = str(baseline.get("text") or baseline.get("body") or "")
        baseline_dict = {
            "status": baseline.get("status", "partial"),
            "official": {"accuracy": fm_float(baseline, "score"),
                         "n": fm_int(baseline, "n"),
                         "eval_mode": baseline.get("eval_mode"),
                         "eval_cmd_used": extract_eval_cmd(b_body)},
            "_body": b_body,
        }

    # synthesis 的 prompt 读 research/literature.md；若历史遗留有对 .json 的引用，做一次容错兜底
    lit_md = RES / "literature.md"
    if lit_md.is_file() and not (RES / "literature.json").is_file():
        try:
            shutil.copy2(lit_md, RES / "literature.json")
        except Exception:
            pass

    synthesis_left_s = int(golden_deadline - time.monotonic())
    synthesis = None
    if synthesis_left_s <= 0:
        degrade("三路 specialist 用尽总窗口，没有剩余预算做汇合——配方与尺子走兜底")
    else:
        synthesis_min = max(1 / 60, synthesis_left_s / 60)
        synthesis_d = node_dir("n000-golden-synthesis")
        synthesis = run_node(
            "step0_synthesis.md", synthesis_d, synthesis_d / "golden_synthesis.md",
            "golden_synthesis", "golden_synthesis",
            ["## 1. 综合诊断与决策依据", "## 2. 统一评测尺子 (Ruler)",
             "## 5. 完整执行工单步骤 (Steps)"],
            state, timeout_min=synthesis_min,
            deny_tools=["WebSearch", "WebFetch"], retries=0,
            total_budget_min=synthesis_left_s / 60, text_contract=True,
        )
    if not synthesis:
        degrade("synthesis 无产出——配方与尺子走兜底")
        synthesis = {}
    elif synthesis.get("status") not in (None, "ok"):
        degrade(f"synthesis status={synthesis.get('status')!r}（非 ok）——正文仍照用，缺口交给下游")

    # 尺子从 frontmatter（n + ruler_cmd）折出；配方就是 synthesis 正文。任一不合法就兜底，
    # 而不是判废整个 Golden Init。
    ruler = build_ruler(synthesis, baseline_dict)
    ruler_error = validate_ruler(ruler)
    if ruler_error:
        ruler = repair_ruler(ruler, str(protocol.get("text") or ""), baseline_dict)
        degrade(f"尺子不合法（{ruler_error}）——编排器补成 {ruler['cmd']}")
    recipe = str(synthesis.get("body") or synthesis.get("text") or "")
    recipe_error = validate_recipe_contract(recipe)
    if recipe_error:
        recipe = repair_recipe(recipe, ruler, baseline_dict)
        degrade(f"配方有缺口（{recipe_error}）——按 golden-recipe.md 默认值兜底，"
                "Golden Run 照兜底版本跑")

    component_status = {
        "protocol": protocol.get("status", "ok"),
        "baseline": baseline.get("status", "partial"),
        "literature": literature.get("status", "ok"),
        "synthesis": "ok" if synthesis and not synthesis.get("_contract_warning") else "partial",
    }
    status = "ok" if all(v == "ok" for v in component_status.values()) else "partial"
    # 起步方案不再字段化：配方正文本身就是主干规划，这里只留一行指路供注入。
    base_plan = "见 research/golden_recipe.md（synthesis 定稿的主干配方与统一尺子）"

    # 供编排器状态恢复使用的精简 baseline 字典（移除庞大的 _body 正文，仅保留核心量化指标）
    clean_baseline = {
        "status": baseline_dict.get("status", "partial"),
        "official": baseline_dict.get("official") or {},
    }

    artifacts = list(dict.fromkeys(
        [f"research/{PROTOCOL_MD}", "research/baseline.md",
         "research/literature.md", "research/golden_recipe.md",
         "research/nodes/n000-golden-synthesis/golden_synthesis.md"]))
    obj = {
        "schema_version": 4,
        "status": status,
        "component_status": component_status,
        "baseline": clean_baseline,
        "data_research": "（外部数据与范式证据详见 research/literature.md）",
        "base_plan": base_plan,
        "golden_recipe": "见 research/golden_recipe.md",
        "ruler": ruler,
        "first_targets": [],
        "artifacts": artifacts,
    }
    if degradations:
        # 注入后续所有节点：开局缺了什么，它们自己心里有数（不是让 run 停下来的理由）
        obj["degradations"] = degradations
    write_json(RES / "golden_init.json", obj)
    # 配方单独落一份 Markdown：Golden Run 直接读它，续跑也不依赖 golden_init.json 的结构
    (RES / "golden_recipe.md").write_text(recipe, encoding="utf-8")

    state["baseline"] = clean_baseline
    state["golden_status"] = status
    state["nodes"].append({"id": "n000-golden", "kind": "golden",
                           "status": status, "components": component_status, "ts": now_iso()})
    save_state(state)
    recipe_head = "\n".join(recipe.splitlines()[:6])
    journal_append(f"## n000-golden [{now_iso()}] Golden Init\n"
                   f"- status: {status} / {json.dumps(component_status, ensure_ascii=False)}\n"
                   f"- baseline: {json.dumps(baseline_dict.get('official'), ensure_ascii=False)}\n"
                   f"- 起步方案: {base_plan}\n"
                   + (f"- 降级项: {json.dumps(degradations, ensure_ascii=False)}\n"
                      if degradations else "")
                   + f"- 尺子: {json.dumps(ruler, ensure_ascii=False)}\n"
                   f"- 配方（前几行）:\n{recipe_head}")
    return True

def step0_golden_run(state: dict, budget_min: int) -> bool:
    """Golden Run：直接执行 Golden Init 写出的配方，把主干建起来。

    单节点自跑到底——不走 Step2/Step3，也不拆单独的 Engineer_2 开工前检查（2026-09-17
    用户决策）：配方已由 Golden Init 三路调研 + synthesis 审计过，工程细节由本节点自己在
    启动前照 skills/engineering/preflight_engineering_check.md 自查即可，不再单设一个检查节点。

    失败不重试（2026-09-07 用户决策）：如实写进 research/golden_run.json，注入后续所有
    节点作为先验，然后照旧落回 bootstrap regime 走大步。
    """
    recipe = ""
    rp = RES / "golden_recipe.md"
    if rp.is_file():
        recipe = rp.read_text(encoding="utf-8", errors="replace").strip()
    if not recipe:
        log("没有 golden_recipe.md，跳过 Golden Run")
        return False
    nid = "n000-golden-run"
    d = node_dir(nid)
    log(f"==== Golden Run {nid}｜墙钟上限 {budget_min} 分钟｜配方 {len(recipe)} 字 ====")

    obj = run_node(
        "step0_golden_run.md", d, d / "result.md", "golden_run", "experiment",
        ["## 1. 实验落地概况", "## 2. 评测指标与结果"], state,
        extra={"HYPOTHESIS": recipe},
        timeout_min=budget_min, with_agents=True, retries=0, text_contract=True)

    result = result_from_md(obj) if obj else {
        "status": "failed", "metrics_dev": None,
        "what_actually_happened": "Golden Run 未产出合法 result.md（见 golden_run.stream.jsonl）"}
    return record_golden_run(state, recipe, result)


def record_golden_run(state: dict, recipe: str, result: dict) -> bool:
    """登记主干节点。成功就写入 best；失败也要留下可被后续节点读到的记录。"""
    nid = "n000-golden-run"
    metrics = result.get("metrics_dev")
    score = score_of(metrics)
    base = score_of((state.get("baseline") or {}).get("official"))
    delta = None if (score is None or base is None) else round(score - base, 4)
    model_ok = Path(str(result.get("model_path", ""))).is_dir()
    # 主干的门槛是"配方跑通且没有退步"，不是"提升超过采纳阈值"。
    # 用采纳阈值当门槛会重现旧问题：主干被一个刚好过噪声线的单坐标实验随机定下来。
    adopted = bool(result.get("status") == "ok" and model_ok
                   and score is not None and delta is not None and delta >= 0)
    if adopted:
        state["best"] = {"node": nid, "score": score, "model": result["model_path"],
                         "metrics": metrics if isinstance(metrics, dict) else {}}
        state["no_improve_streak"] = 0

    record = {
        "id": nid, "ts": now_iso(), "adopted": adopted,
        "status": result.get("status"),
        "score": score, "baseline_official": base, "delta": delta,
        "eval_mode": (metrics or {}).get("eval_mode"), "eval_n": eval_n_of(metrics),
        "metrics": metrics if isinstance(metrics, dict) else {},
        "recipe": recipe,
        "what_actually_happened": result.get("what_actually_happened"),
        "verdict_reason": result.get("verdict_reason"),
        "surprises": result.get("surprises") or [],
        # 给后续节点的直接指示：主干没建起来时，第一轮猜想要接着回答什么问题
        "lesson_for_next_rounds": (
            "主干已建立，后续实验在它之上叠加；配方里已经验证过的部分不要重复提。"
            if adopted else
            "开局配方未能成为主干，编排器已落回 bootstrap 大步模式。"
            "提猜想前先读本记录的 what_actually_happened 与 surprises："
            "配方在哪一环断掉，就是第一轮最该问的机制问题，不要从零重猜。"),
    }
    write_json(RES / "golden_run.json", record)

    state["nodes"].append({
        "id": nid, "kind": "golden_run", "ts": now_iso(),
        "title": "Golden Run：执行 Golden Init 配方建立主干",
        "layer": "strategy", "target": "multi", "regime": "golden_init",
        "score": score, "delta": delta, "adopt_threshold": 0.0,
        "eval_mode": (metrics or {}).get("eval_mode"), "eval_n": eval_n_of(metrics),
        "adopted": adopted, "status": result.get("status"),
        "verdict": result.get("hypothesis_verdict"),
        "wall_min": result.get("wall_min"),
    })
    save_state(state)

    recipe_head = "\n".join(str(recipe).splitlines()[:8])
    journal_append(
        f"## {nid} [{now_iso()}] Golden Run（主干）\n"
        f"- 配方（前几行）:\n{recipe_head}\n"
        f"- 结果: 官方全量 {score}（基线 {base}，Δ{delta}，"
        f"{(metrics or {}).get('eval_mode')} n={eval_n_of(metrics)}）"
        f" status={result.get('status')}\n"
        f"- 实际发生: {result.get('what_actually_happened')}\n"
        f"- 成为主干: {adopted}\n"
        f"- 给后续轮次: {record['lesson_for_next_rounds']}")
    if adopted:
        log(f"Golden Run 成为主干：官方全量 {score}（基线 {base}，Δ{delta}）")
    else:
        log(f"Golden Run 未能成为主干（status={result.get('status')} score={score} "
            f"Δ={delta}）：不重试，记录已写入 research/golden_run.json，"
            f"后续照旧走 bootstrap 大步")
    gc_checkpoints(state)
    return adopted


def latest_measurement_text(state: dict) -> str:
    """获取最近一次有效 Measurement 产出的精简摘要与指引，避免 20KB+ 全文塞爆猜想 prompt。"""
    measures = [n for n in state.get("nodes", []) if n.get("kind") == "measure"]
    if not measures:
        return ""
    last = measures[-1]
    nid = last.get("id")
    p = node_dir(nid) / "measurement.md"
    if p.is_file() and p.stat().st_size > 0:
        raw = p.read_text(encoding="utf-8", errors="replace").strip()
        fm = {}
        body = raw
        if raw.startswith("---"):
            parts = raw.split("---", 2)
            if len(parts) >= 3:
                for line in parts[1].splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        fm[k.strip()] = v.strip()
                body = parts[2].strip()

        # 提取核心要点：只取前两节的标题和摘要，忽略冗长 case 和附带脚本
        lines = []
        for line in body.splitlines():
            if line.startswith("## 3. 对下一轮猜想的坐标与靶点建议"):
                break
            lines.append(line)
        summary_body = "\n".join(lines[:25]).strip()

        focus = fm.get("focus_dimension", "未知")
        targets = fm.get("suggested_targets", "[]")
        return (f"## 最新度量诊断要点（来自 {nid}）\n"
                f"- **主要瓶颈维度**: `{focus}`\n"
                f"- **建议优先干预靶点**: `{targets}`\n"
                f"- **诊断要点精选**:\n{summary_body}\n\n"
                f"*(详细统计分面、典型 Case 与诊断脚本见: `{p}`，可按需查阅)*")
    # 若无 md 则退回 json/state 摘要
    if last.get("finding") or last.get("decision"):
        return (f"## 最新度量诊断要点（来自 {nid}）\n"
                f"- 诊断结论: {last.get('finding') or '（无）'}\n"
                f"- 建议方向: {last.get('decision') or '（无）'}")
    return ""


def should_trigger_measurement(state: dict) -> tuple[bool, str]:
    """当猜想被硬规则或 Judge 全否时，判断是否应升级进入 Measurement。
    
    原则：
    1. 达到全局上限 MAX_MEASURE 则不再触发；
    2. 若针对当前的实验输出/基线状态尚未做过 Measurement，触发深入诊断；
    3. 若针对同一份评测结果已经做过诊断，不再重复触发，防止死循环。
    """
    measures = [n for n in state.get("nodes", []) if n.get("kind") == "measure"]
    if len(measures) >= MAX_MEASURE:
        return False, f"已达 Measurement 次数上限 ({MAX_MEASURE})"
    exps = [n for n in state.get("nodes", []) if n.get("kind") == "exp"]
    current_exp_count = len(exps)
    last_anchor = state.get("measure_anchor", -1)
    if current_exp_count <= last_anchor:
        return False, "当前实验产物已做过深度诊断，跳过避免重复"
    return True, f"连续猜想全被否决（第 {state.get('consec_rejects', 0)} 轮），进入行为诊断升级"


def step2_measurement(state: dict, reason: str, budget_min: int) -> dict:
    nid = new_nid(state, "measure")
    d = node_dir(nid)
    log(f"---- Step2 Measurement {nid}：{reason}")
    best = state.get("best") or {}
    best_path = best.get("model") or MODEL
    obj = run_node("step2_measurement.md", d, d / "measurement.md", "measurement",
                   "measurement", ["## 1. 现象细分与量化表现", "## 2. 瓶颈归因与机制洞察"], state,
                   extra={"MEASUREMENT_REASON": reason,
                          "BEST_MODEL_PATH": best_path},
                   timeout_min=budget_min, retries=0, text_contract=True)
    state["nodes"].append({"id": nid, "kind": "measure", "ts": now_iso(),
                           "trigger": reason,
                           "focus_dimension": (obj or {}).get("focus_dimension"),
                           "suggested_targets": [t for t in ((obj or {}).get(
                               "suggested_targets") or []) if isinstance(t, (dict, str))],
                           "finding": (obj or {}).get("body") or (obj or {}).get("text")})
    state["measure_pending"] = False
    state["measure_reason"] = None
    state["measure_anchor"] = len([n for n in state["nodes"] if n.get("kind") == "exp"])
    # 注：新版 Measurement 只读官方日志做细分诊断、不再重跑评测，因此不产出可当尺子锚点的
    # rerun 指标（旧的 best.ruler_metrics 回填链路随之取消）。跨刻度比较由 same_scale_anchor
    # 兜底：全量尺子时主干与循环实验同刻度；固定子集时退到基线锚点并打 WARN。
    save_state(state)
    if obj:
        targets = (obj.get("suggested_targets") or [])
        journal_append(f"## {nid} [{now_iso()}] Step2 Measurement\n- 触发: {reason}\n"
                       f"- 主瓶颈维度: {obj.get('focus_dimension')}\n"
                       f"- 建议靶点: {json.dumps(targets, ensure_ascii=False)}\n"
                       f"- 诊断正文见 research/nodes/{nid}/measurement.md")
    return obj or {}


ACTION_SPACE = None
STEP_POLICY = None

def step_policy() -> dict:
    global STEP_POLICY
    if STEP_POLICY is None:
        STEP_POLICY = read_json(RES / "step_policy.json") or {"regimes": {}}
    return STEP_POLICY


def action_space() -> dict:
    global ACTION_SPACE
    if ACTION_SPACE is None:
        ACTION_SPACE = read_json(RES / "action_space.json") or {"domains": {}}
    return ACTION_SPACE


def valid_target(t: dict) -> tuple[bool, str]:
    """靶点必须落在操作空间的合法坐标上——这是"防止空想"的硬约束。"""
    dom = action_space()["domains"].get(str(t.get("domain")))
    if not dom:
        return False, f"domain `{t.get('domain')}` 不在操作空间内"
    mod = dom.get("modules", {}).get(str(t.get("module")))
    if not mod:
        return False, f"module `{t.get('module')}` 不在 domain `{t.get('domain')}` 下"
    if mod.get("frozen"):
        return False, f"坐标被冻结：{mod.get('frozen_reason', 'frozen')}"
    return True, ""


def coord_layer(dom: dict, mod: dict) -> str:
    return str(mod.get("layer") or dom.get("layer") or "exec")


def allowed_layers(sched: dict) -> list[str]:
    """本轮允许的层级，由 regime 决定（polish 为 exec，explore/reignite 为 strategy+exec）。"""
    return list(sched.get("layers") or ["strategy", "exec"])


def assign_plans(state: dict, sched: dict) -> list[dict]:
    """给每个并行候选分配槽位。常态下全开 strategy/exec，不控制步伐大小。"""
    plans: list[dict] = []
    layers = allowed_layers(sched)
    n_cand = int(sched.get("n_cand") or N_HYPO)
    for i in range(n_cand):
        plans.append({"idx": i, "layers": layers})
    return plans


def step1_hypotheses(state: dict, sched: dict, plans: list[dict],
                     budget_min: int, repair_hint: str = "") -> list[dict]:
    """N 个猜想节点并行。纯 API 阶段，不占卡；守卫会拦下任何占卡命令。"""
    log(f"---- Step1 并行猜想 ×{len(plans)}（mode={sched['mode']}）")
    for p in plans:
        log(f"     cand-{p['idx']}: layers={p['layers']}")
    for p in plans:
        p["nid"] = new_nid(state, "hyp") + f"-c{p['idx']}"
        p["dir"] = node_dir(p["nid"])

    act_space_json = inject_json(action_space())

    def one(p: dict):
        best = state.get("best") or {}
        extra = {
            "CAND_INDEX": p["idx"] + 1, "N_CAND": len(plans), "N_OTHER": len(plans) - 1,
            "MODE": sched["mode"], "MODE_HINT": sched["hint"],
            "BEST_MODEL_PATH": best.get("model") or MODEL,
            "ACTION_SPACE": act_space_json,
            "ALLOWED_LAYERS": "/".join(p["layers"]),
            "COST_CAP_H": sched["cost_cap_h"],
            "REPAIR_HINT": repair_hint or "（无）",
            "MEASUREMENT_DIAGNOSIS": latest_measurement_text(state),
            # Step5 归档知识库：每轮实验后追加卡片，猜想节点开卷读，避免重复已证伪路线。
            # 首轮文件还不存在，prompt 里已说明"初始为空"属正常。
            "BANK_PATH": BANK_PATH,
        }
        return run_node("step1_hypothesis.md", p["dir"], p["dir"] / "hypothesis.md",
                        f"hypo-{p['idx']}", "hypothesis",
                        ["## 1. 现象观察与支撑证据", "## 2. 机制假说", "## 3. 干预变量与受控设计",
                         "## 4. 证伪条件与预期指标"], state,
                        extra=extra, timeout_min=budget_min,
                        deny_tools=["WebSearch", "WebFetch"], retries=1, text_contract=True)

    with ThreadPoolExecutor(max_workers=max(1, len(plans))) as ex:
        results = list(ex.map(one, plans))
    out = []
    for p, obj in zip(plans, results):
        if not obj:
            continue
        obj["_id"] = f"cand-{p['idx']}"
        obj["_nid"] = p["nid"]
        obj["_dir"] = str(p["dir"])
        obj["_plan"] = p
        out.append(obj)
    return out
    out = []
    for p, obj in zip(plans, results):
        if not obj:
            continue
        obj["_id"] = f"cand-{p['idx']}"
        obj["_nid"] = p["nid"]
        obj["_dir"] = str(p["dir"])
        obj["_plan"] = p
        out.append(obj)
    return out


def _nonempty(v) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, dict)):
        return bool(v)
    return True


def hypo_title_from_body(body: str, fallback: str) -> str:
    """从假说 Markdown 正文里取一句话当标题（新版 hypothesis frontmatter 不再有 title 字段）：
    优先「## 2. 机制假说」小节下第一条非空行，取不到就退回坐标组合。"""
    lines = (body or "").splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().startswith("## 2") and "机制假说" in ln:
            for nxt in lines[i + 1:]:
                s = nxt.strip().lstrip("-*").strip()
                if s:
                    return s[:80]
            break
    for ln in lines:
        s = ln.strip().lstrip("-*#").strip()
        if s:
            return s[:80]
    return fallback


def prefilter_candidates(cands: list[dict], sched: dict, state: dict,
                          budget_cap_h: float) -> tuple[list[dict], list[dict]]:
    """硬规则先筛（流程记录.md 猜想 judge 第①条）。

    这一层**不调 LLM**：非法坐标、越权层级、缺 targets、
    违反步长契约、墙钟装不下，都在这里判废（已废除死板的退火模块数量限制）。
    判重（与已归档尤其已证伪的猜想实质重复）交给 Judge——它读 experience_bank。
    留给 LLM 的是②：两两对比选机理潜力。
    """
    survivors, rejected = [], []

    def drop(c: dict, reason: str):
        log(f"  {c['_id']} 判废：{reason}")
        write_json(Path(c["_dir"]) / "rejected.json",
                   {"id": c["_id"], "reason": reason, "stage": "hard_rules"})
        rejected.append({"id": c["_id"], "reason": reason})

    seen_target_keys: set[str] = set()
    for c in cands:
        plan = c.get("_plan") or {}
        spec = size_spec(plan.get("size", ""))

        if c.get("abstain"):
            drop(c, f"候选主动弃权：{c.get('abstain_reason') or '未说明'}")
            continue

        # 解析自主选取的靶点列表（支持 targets 列表或单数 target）
        raw_targets = c.get("targets")
        if not raw_targets and c.get("target"):
            raw_targets = [c.get("target")] if isinstance(c.get("target"), dict) else []
        if not isinstance(raw_targets, list) or not raw_targets:
            drop(c, "未在 frontmatter 中指定有效的 targets 靶点列表")
            continue

        # 校验每个靶点的合法性与层级
        valid_keys = []
        target_invalid = False
        allowed_layers_set = set(plan.get("layers", ["exec", "strategy"]))
        for t in raw_targets:
            if not isinstance(t, dict):
                drop(c, f"靶点元素格式非法: {t}")
                target_invalid = True
                break
            ok, why = valid_target(t)
            if not ok:
                drop(c, f"靶点 {t.get('domain')}.{t.get('module')} 非法：{why}")
                target_invalid = True
                break
            dom = action_space()["domains"].get(str(t.get("domain")), {})
            mod = dom.get("modules", {}).get(str(t.get("module")), {})
            mod_layer = coord_layer(dom, mod)
            if mod_layer not in allowed_layers_set:
                drop(c, f"靶点 {t.get('domain')}.{t.get('module')} 的层级 `{mod_layer}` 不在本候选允许层级集 {plan.get('layers')}")
                target_invalid = True
                break
            valid_keys.append(f"{t.get('domain')}.{t.get('module')}")

        if target_invalid:
            continue

        target_combo_key = "+".join(sorted(valid_keys))
        if target_combo_key in seen_target_keys:
            drop(c, f"本轮已有另一个候选选取了完全相同的靶点组合 {target_combo_key}")
            continue

        # 赋回标准规范格式，供下游使用
        c["target"] = raw_targets[0] if len(raw_targets) == 1 else {"domain": "Multi", "module": target_combo_key}
        c["_target_key"] = target_combo_key
        # 记录/日志/直方图要用的派生字段（新版 frontmatter 不再带 layer/title）：
        # 层级取所选靶点里最高的一档（有 strategy 就算 strategy），标题从正文里取。
        layers_of = []
        for t in raw_targets:
            _dom = action_space()["domains"].get(str(t.get("domain")), {})
            _mod = _dom.get("modules", {}).get(str(t.get("module")), {})
            layers_of.append(coord_layer(_dom, _mod))
        c["layer"] = "strategy" if "strategy" in layers_of else "exec"
        c["title"] = hypo_title_from_body(c.get("body") or "", target_combo_key)

        # —— 可行性：估时装不下剩余墙钟就判废
        cap = budget_cap_h
        est = float(c.get("cost_estimate_h") or 0)
        if est > cap:
            drop(c, f"成本估计 {est}h 超过剩余墙钟 {cap:.2f}h")
            continue

        seen_target_keys.add(target_combo_key)
        c["_budget_cap_h"] = cap
        survivors.append(c)

    log(f"  硬规则通过 {len(survivors)}/{len(cands)}")
    return survivors, rejected


def step1_judge(state: dict, cands: list[dict], sched: dict, budget_min: int):
    """LLM judge 两两对比选一个 winner；硬规则已在 prefilter 筛过。

    新版契约（prompts/step1_judge.md）是 Markdown+frontmatter：`winner_index` 指向下面
    注入的候选清单里的第几个（0-based，对应 Candidate 0/1/2…），全否时为 null 并给出
    `insufficient_reason`（measurement=证据不足需度量升级 / quality=平庸可直接重试）。
    返回 (chosen, why, insufficient_reason)。
    """
    nid = new_nid(state, "judge")
    d = node_dir(nid)
    blocks = []
    for i, c in enumerate(cands):
        tgt_desc = c.get("_target_key") or "未定坐标"
        body = str(c.get("body") or c.get("text") or "").strip()
        blocks.append(
            f"### Candidate {i}（_id={c['_id']}｜靶点坐标: {tgt_desc}"
            f"｜预估 {c.get('cost_estimate_h')}h）\n\n{body}")
    obj = run_node("step1_judge.md", d, d / "judge.md", "judge", "judge",
                   ["## 1. 候选方案证据核验", "## 3. 最终裁决"], state,
                   extra={"N_CAND": len(cands), "CANDIDATES": "\n\n".join(blocks),
                          "CAND_IDS": ",".join(c["_id"] for c in cands),
                          "COST_CAP_H": sched["cost_cap_h"],
                          # judge 判重需要看归档知识库；首轮文件不存在，prompt 已说明"初始为空"
                          "BANK_PATH": BANK_PATH},
                   timeout_min=budget_min, deny_tools=["WebSearch", "WebFetch"],
                   retries=1, text_contract=True)
    reason_kind = (obj or {}).get("insufficient_reason")
    if isinstance(reason_kind, str) and reason_kind.strip().lower() in ("null", "none", ""):
        reason_kind = None
    win = fm_int(obj or {}, "winner_index")
    state["nodes"].append({"id": nid, "kind": "judge", "ts": now_iso(),
                           "winner_index": win, "insufficient_reason": reason_kind})
    save_state(state)
    if not obj:
        return None, "judge 节点未产出合法契约", None
    if win is None or not (0 <= win < len(cands)):
        return (None,
                f"judge 判本轮候选全部不达标（winner_index={(obj or {}).get('winner_index')!r}）",
                reason_kind)
    chosen = cands[win]
    chosen["_review"] = {"winner_index": win, "winner_file": obj.get("winner_file")}
    return chosen, None, None



def hypo_inject(hypo) -> str:
    """注入给下游（plan / engineer）的假说：新版契约是 Markdown，直接给正文；
    Golden Run 传的是配方文本（str）；只有极端兜底才退回 dict 的 JSON。"""
    if isinstance(hypo, str):
        return hypo
    body = (hypo or {}).get("body") or (hypo or {}).get("text")
    if body:
        return str(body)
    return inject_json({k: v for k, v in (hypo or {}).items() if not k.startswith("_")})


def step3_experiment(state: dict, hypo: dict, sched: dict, budget_min: int):
    """Step3 端到端实验落地：规划方案 -> 运行前工程与配置自查 (Preflight) -> 占卡训练与统一评测。
    三合一单卡串行执行，遵循边做边写的渐进式落盘纪律。
    产物是 Markdown+frontmatter（experiment.md），折成下游 result 形状后返回。"""
    d = Path(hypo["_dir"])
    log(f"---- Step3 端到端实验落地 {hypo['_nid']}（预算 {budget_min} 分钟）")
    obj = run_node("step3_experiment.md", d, d / "experiment.md", "experiment", "experiment",
                   ["## 1. 实验方案与控制变量", "## 2. 运行前工程与配置自查 (Preflight)", "## 3. 评测指标与结果分析"], state,
                   extra={"HYPOTHESIS_FILE": str(d / "hypothesis.md"),
                          "HYPOTHESIS": hypo_inject(hypo),
                          "COST_CAP_H": round(budget_min / 60.0, 2),
                          "BEST_MODEL_PATH": state["best"].get("model")},
                   timeout_min=budget_min, with_agents=True, retries=0, text_contract=True)
    if not obj:
        return None
    res = result_from_md(obj)
    res["preflight_passed"] = fm_bool(obj, "preflight_passed", True)
    return res


def score_of(metrics: dict | None) -> float | None:
    if not isinstance(metrics, dict):
        return None
    for k in ("accuracy", "acc", "mean", "score"):
        if isinstance(metrics.get(k), (int, float)):
            return float(metrics[k])
    return None


def eval_n_of(metrics: dict | None) -> int:
    """本次评了多少条。老产物可能只写 limit，兜底到尺子的 n。"""
    if isinstance(metrics, dict):
        for k in ("n", "limit", "num_samples"):
            v = metrics.get(k)
            if isinstance(v, int) and v > 0:
                return v
    return RULER_N


def adopt_threshold(metrics: dict | None) -> float:
    """采纳阈值：全量评测（official_full）一律定死为IMPROVE_EPS（0.01）。

    子集评测（official_subset抽样）本身更抖，仍挂到噪声上max(IMPROVE_EPS, se)，
    se是n条样本上准确率的标准误（n=300时se≈0.029）。
    """
    if (metrics or {}).get("eval_mode") in ("subset", "official_subset"):
        n = max(1, eval_n_of(metrics))
        se = (0.25 / n) ** 0.5
        return round(max(IMPROVE_EPS, se), 4)
    return IMPROVE_EPS


def _scale_of(metrics: dict | None) -> tuple[str, int]:
    """刻度 = (eval_mode, n)。official_full 与 official_subset 是两把尺子；
    同为 official_subset 但 n 不同也是两把尺子。"""
    m = metrics if isinstance(metrics, dict) else {}
    return (str(m.get("eval_mode") or ""), eval_n_of(m) if m else 0)


def same_scale_anchor(state: dict, metrics: dict | None) -> tuple[float | None, str]:
    """本次评测该跟谁比。Δ 只能在同刻度上算——跨尺子比会把刻度偏差当成提升。

    全程只有两种合法刻度：Golden Run/基线的 official_full，和循环阶段的统一尺子
    （official_full 或 official_subset@ruler_n）。尺子取全量时（常见默认），主干与循环
    实验同刻度，直接用 best.score：
      1. best 与本次同刻度 -> 直接用 best.score；
      2. 否则 -> best.ruler_metrics（历史遗留的同刻度锚点；新版 Measurement 不再重跑
         评测回填它，通常为空，仅作兼容/续跑兜底）；
      3. 都没有 -> 同刻度的基线锚点（baseline.official / baseline.ruler_metrics）。
    尺子取固定子集、且主干只有全量刻度时会落到基线锚点并打 WARN——那一轮 Δ 只说明相对
    基线的位置。
    """
    cur = _scale_of(metrics)
    best = state.get("best") or {}
    bl = state.get("baseline") or {}
    if best.get("node"):
        if _scale_of(best.get("metrics")) == cur \
                and isinstance(best.get("score"), (int, float)) and cur[0]:
            return float(best["score"]), f"best {best['node']}（同刻度）"
        s = score_of(best.get("ruler_metrics"))
        if s is not None and _scale_of(best.get("ruler_metrics")) == cur:
            return s, f"best {best['node']} 的尺子锚点"
        log(f"WARN 当前 best 是 {_scale_of(best.get('metrics'))} 刻度、本次是 {cur}，"
            f"且没有同刻度锚点，退到基线锚点比较——这一轮的 Δ 只说明相对基线的位置，"
            f"不说明相对主干")
    if _scale_of(bl.get("official")) == cur:
        return score_of(bl.get("official")), "baseline.official"
    s = score_of(bl.get("ruler_metrics"))
    if s is not None and _scale_of(bl.get("ruler_metrics")) == cur:
        return s, "baseline 的尺子锚点"
    return (score_of(bl.get("official")) or score_of(bl.get("ruler_metrics"))), \
        "baseline.official（刻度不完全一致）"


def step5_record(state: dict, hypo: dict, result: dict) -> None:
    """记录猜想-实验对，更新 best，做 checkpoint GC。"""
    nid = hypo["_nid"]
    plan_meta = hypo.get("_plan") or {}
    metrics = result.get("metrics_dev")
    score = score_of(metrics)
    base, base_from = same_scale_anchor(state, metrics)
    delta = None if (score is None or base is None) else round(score - base, 4)
    thr = adopt_threshold(metrics)
    preflight_passed = result.get("preflight_passed", True)

    adopted = bool(score is not None and delta is not None
                   and delta >= IMPROVE_EPS and delta > thr and result.get("status") == "ok"
                   and Path(str(result.get("model_path", ""))).is_dir())
    if adopted:
        # 不带 ruler_metrics：新 best 自己的分数就在统一尺子上，旧主干的尺子锚点已过期
        state["best"] = {"node": nid, "score": score, "model": result["model_path"],
                         "metrics": metrics if isinstance(metrics, dict) else {}}
        state["no_improve_streak"] = 0
    else:
        # 没有大于等于 0.01 的有效改进
        state["no_improve_streak"] = state.get("no_improve_streak", 0) + 1

    tgt_str = hypo.get("_target_key")
    if not tgt_str:
        tgt = hypo.get("target", {})
        tgt_str = f"{tgt.get('domain')}.{tgt.get('module')}"
    state["nodes"].append({
        "id": nid, "kind": "exp", "ts": now_iso(), "title": hypo.get("title"),
        "layer": hypo.get("layer"), "target": tgt_str,
        "regime": sched.get("mode"),
        "score": score, "delta": delta, "adopt_threshold": thr,
        "delta_vs": base_from,
        "eval_mode": (metrics or {}).get("eval_mode"), "eval_n": eval_n_of(metrics),
        "adopted": adopted,
        "verdict": result.get("hypothesis_verdict"), "status": result.get("status"),
        "preflight_passed": preflight_passed,
        "cost_estimate_h": hypo.get("cost_estimate_h"), "wall_min": result.get("wall_min"),
    })
    save_state(state)

    hypo_head = "\n".join(str(hypo.get("body") or "").splitlines()[:4])
    journal_append(
        f"## {nid} [{now_iso()}] {sched.get('mode')} / {hypo.get('layer')} / {tgt_str}\n"
        f"- 猜想: {hypo.get('title')}\n"
        f"- 靶点: {tgt_str}（预估 {hypo.get('cost_estimate_h')}h）\n"
        f"- 假说要点:\n{hypo_head}\n"
        f"- 结果: {score}（Δ{delta}，采纳阈值 {thr}，比较对象 {base_from}={base}，"
        f"{(metrics or {}).get('eval_mode')} n={eval_n_of(metrics)}）status={result.get('status')}\n"
        f"- 实际发生: {result.get('what_actually_happened')}\n"
        f"- Preflight 自查通过: {preflight_passed}\n"
        f"- 采纳: {adopted}")

    # 运行 Step5 Archive Agent：提炼研究卡片写入本节点契约，并由 Agent 自行 sync 进知识库。
    # 编排器不再代写 bank —— 知识库的追加完全交给 Step5 Agent（见 step5_archive.md 指令 3）。
    # 早先编排器会把 archive 正文整段 append 一次，和 Agent 自己提炼的条目重复记录同一轮，已移除。
    try:
        archive_d = node_dir(new_nid(state, "archive"))
        run_node(
            "step5_archive.md", archive_d, archive_d / "archive_card.md", "archive", "archive",
            ["## 1. 猜想与机制假说", "## 2. 实验方案概述", "## 3. 实验结果与验证判据"],
            state, extra={
                "NODE_ID": nid,
                "TARGET_KEY": tgt_str,
                "BANK_PATH": BANK_PATH,
            }, timeout_min=15, text_contract=True, retries=0
        )
    except Exception as exc:
        log(f"WARN Step5 Archive 归档记录异常: {exc!r}")

    gc_checkpoints(state)


def gc_checkpoints(state: dict) -> None:
    """4B bf16 一份 ~8GB。只保留 best 和最近 KEEP_CKPT 个节点的权重。"""
    keep = {str(state["best"].get("model") or "")}
    exp = [n["id"] for n in state["nodes"] if n.get("kind") == "exp"]
    for nid in exp[-KEEP_CKPT:]:
        keep.add(str(NODES / nid / "model"))
    freed = 0
    for nid in exp[:-KEEP_CKPT] if len(exp) > KEEP_CKPT else []:
        m = NODES / nid / "model"
        if m.is_dir() and str(m) not in keep:
            try:
                freed += sum(f.stat().st_size for f in m.rglob("*") if f.is_file())
                shutil.rmtree(m)
                (NODES / nid / "model.gc").write_text(
                    f"权重已被 orchestrator GC 回收 at {now_iso()}", encoding="utf-8")
            except Exception as exc:
                log(f"WARN GC {m} 失败: {exc!r}")
    if freed:
        log(f"  GC 回收 {freed / 2**30:.1f} GiB")


# ---------------------------------------------------------------- 5. 调度（常态探索 / 重新点火 / 收尾保护）


def schedule(state: dict, frac_left: float, left_h: float | None = None) -> dict:
    """极简三态调度：
    1. 平台期重新点火（reignite）：连续两次无提升（no_improve_streak >= 2）时触发。
       给出类似 bootstrap 的破局提示，踢掉最近采纳节点的坐标，鼓励跳出局部最优、尝试全新范式（如接 RL）。
    2. 收尾保护（polish）：剩余墙钟 <= 25% 时触发。
       提示避免发起耗时过长的大改动，只做低风险、稳健的收尾。
    3. 常态运行（explore）：其余所有情况。
       不限制步伐大小，不限制模块个数，自由探索与机理叠加。
    """
    if left_h is None:
        left_h = remaining_h()
    streak = state.get("no_improve_streak", 0)
    stuck = state.get("consec_rejects", 0) >= 2

    # 连续两次无提升即判定为平台期，启动重新点火
    if streak >= 2 and frac_left > 0.25 and not stuck:
        regime = "reignite"
        state["reignite_count"] = state.get("reignite_count", 0) + 1
        hint = (f"【重新点火】已连续 {streak} 个节点没有提升，判定进入平台期。\n"
                "请跳出当前的局部调优思维：允许并强烈建议尝试全新路线或进行范式跃迁"
                "（例如：若当前一直做 SFT，可基于当前 best 引入 RL/GRPO/DPO，或重塑数据构造与奖励机制）。\n"
                "编排器已把最近两个被采纳节点的坐标从坐标池中剔除，请探索全新机理。")
        layers = ["strategy", "exec"]
    elif frac_left <= 0.25:
        regime = "polish"
        hint = ("【收尾保护】实验剩余时间有限（<= 25%），请勿发起耗时过长或风险过高的大改动（如全新的长周期多阶段训练）。\n"
                "聚焦于低风险、稳健、可回滚的参数收敛与配置对齐，确保产物能安全落盘。")
        layers = ["exec"]
    else:
        regime = "explore"
        hint = ("【常态探索】围绕当前发现的瓶颈自由提出因果假说。不设人为步伐与模块数量限制，"
                "只要机理闭环、现象支撑充分，既可以在当前 best 上精细叠加，也可以开启多阶段扩展（如基于 SFT 接 RL）。")
        layers = ["strategy", "exec"]

    cost_cap_h = round(max(0.3, left_h), 2)
    sched = {
        "mode": regime,
        "layers": layers,
        "n_cand": N_HYPO,
        "cost_cap_h": cost_cap_h,
        "hint": hint,
    }

    if stuck:
        sched["mode"] += "+relaxed"
        sched["layers"] = ["strategy", "exec"]
        sched["hint"] += ("\n注意：前序候选连续被否或靶点非法，请优先保证现象支撑（judge 最看重 grounded）与可执行性。")

    return sched


# ---------------------------------------------------------------- 6. 收尾
def resolve_base_model() -> Path | None:
    """final_model 的兜底：没有任何节点胜过基线时，也必须交出一个可评测的模型目录。"""
    p = Path(MODEL)
    if p.is_dir():
        return p
    # MODEL 是 HF id（如 Qwen/Qwen3-4B-Base），本机快照由 prepare_hf_layout.sh 挂成本地
    # HF id。这里直接解析本地路径，不要走 snapshot_download(local_files_only)——
    # 那个布局是给 transformers 的（refs/main 里是 "local" 不是 commit hash、
    # snapshots/main 是相对坏链），huggingface_hub 的缓存解析不认它，实测会抛
    # LocalEntryNotFoundError，导致崩溃后连"回退基座"都交不出 final_model。
    if "/" in str(MODEL):
        org, name = str(MODEL).split("/", 1)
        hf = Path(os.environ.get("HF_HOME", ""))
        for cand in (hf / org / name,
                     hf / "hub" / f"models--{org}--{name}" / "snapshots" / "local"):
            if cand.is_dir() and (cand / "config.json").is_file():
                return cand
    try:
        from huggingface_hub import snapshot_download
        return Path(snapshot_download(MODEL, local_files_only=True))
    except Exception as exc:
        log(f"WARN 无法解析基座模型路径: {exc!r}")
        return None


def finalize(state: dict) -> None:
    """把 best 落成 ./final_model。harness 只认这个目录，所以它必须存在。"""
    if _FINALIZED.is_set():
        return
    _FINALIZED.set()
    set_phase("finalize")
    dst = TASK_DIR / "final_model"
    src = state["best"].get("model")
    if not (src and Path(src).is_dir()):
        log("没有胜过基线的节点，final_model 回退为基座模型（分数即基线）")
        src = resolve_base_model()
    if not (src and Path(src).is_dir()):
        log("ERROR 连基座模型都定位不到，final_model 无法生成")
        return
    try:
        if dst.exists():
            shutil.rmtree(dst) if dst.is_dir() and not dst.is_symlink() else dst.unlink()
        shutil.copytree(src, dst, symlinks=False)
        log(f"final_model <- {src}")
    except Exception as exc:
        log(f"ERROR 生成 final_model 失败: {exc!r}")
    golden_run = read_json(RES / "golden_run.json") or {}
    write_json(RES / "summary.json", {
        "finished": now_iso(), "cli": CLI, "cli_model": CLI_MODEL,
        "best": state["best"], "baseline": state.get("baseline"),
        "golden_run": {"adopted": golden_run.get("adopted"),
                       "status": golden_run.get("status"),
                       "score": golden_run.get("score"),
                       "delta": golden_run.get("delta")},
        "n_exp_nodes": len([n for n in state["nodes"] if n.get("kind") == "exp"]),
        "n_adopted": len([n for n in state["nodes"] if n.get("adopted")]),
        "n_measurement_nodes": len([n for n in state["nodes"] if n.get("kind") == "measure"]),
        "reignite_count": state.get("reignite_count", 0),
        "rejected_rounds": state.get("rejected_rounds", 0),
        "final_model_source": str(src),
        "target_histogram": _hist(state, "target"),
        "regime_histogram": _hist(state, "regime"),
        "layer_histogram": _hist(state, "layer")})
    journal_append(f"## 收尾 [{now_iso()}]\n- best: {json.dumps(state['best'], ensure_ascii=False)}\n"
                   f"- final_model 来源: {src}")


def _hist(state: dict, field: str) -> dict:
    """分布直方图：记录采纳或实验节点的属性分布。"""
    hist: dict[str, int] = {}
    for n in state["nodes"]:
        if n.get("kind") == "exp" and n.get(field):
            hist[str(n[field])] = hist.get(str(n[field]), 0) + 1
    return hist


def _on_term(signum, _frame):
    log(f"收到信号 {signum}，立刻收尾")
    try:
        finalize(load_state())
    finally:
        os._exit(128 + signum)

# ---------------------------------------------------------------- 7. 主流程
def bootstrap() -> None:
    """把守卫、操作空间、settings 物化到 research/ 下。
    放在 research/ 而不是 agent 目录，是为了让归档和反作弊 judge 能看到它们。"""
    RES.mkdir(parents=True, exist_ok=True)
    NODES.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ASSETS / "guard.py", RES / "guard.py")
    act_space_src = TASKS_DIR / TASK_TYPE / "action_space.json"
    if not act_space_src.is_file():
        act_space_src = AGENT_DIR / "action_space.json"
    shutil.copy2(act_space_src, RES / "action_space.json")
    shutil.copy2(ASSETS / "step_policy.json", RES / "step_policy.json")
    shutil.copy2(ASSETS / "benchmark_profile.json", RES / "benchmark_profile.json")
    if (PROMPTS / "WORKFLOW_OVERVIEW.md").is_file():
        shutil.copy2(PROMPTS / "WORKFLOW_OVERVIEW.md", RES / "WORKFLOW_OVERVIEW.md")
    tpl = (ASSETS / "hooks_settings.json").read_text(encoding="utf-8")
    SETTINGS.write_text(render(tpl, {"PYTHON": sys.executable,
                                     "GUARD": str(RES / "guard.py")}), encoding="utf-8")
    if not JOURNAL.exists():
        journal_append(f"# 研究日志\n\n- 开始: {now_iso()}\n- task: {TASK} / {benchmark_name()}\n"
                       f"- 基座: {MODEL}\n- 驱动 CLI: {CLI}\n- CLI 模型: {CLI_MODEL}\n"
                       f"- 编排器: research_common")


def adopt_golden_into_state(state: dict) -> None:
    """golden_init.json 在、但 state 里没有 baseline 时补齐（例如上次在 Golden Init 之后被杀）。
    没有 baseline 的话 delta 永远算不出来，任何节点都不会被采纳。
    Golden Run 已经成为主干时也一并恢复 best，否则续跑会把主干丢掉、退回基座重开。"""
    gi = read_json(RES / "golden_init.json") or {}
    if not state.get("baseline") and gi.get("baseline"):
        state["baseline"] = gi["baseline"]
        state["golden_status"] = gi.get("status")
        if not any(n.get("id") == "n000-golden" for n in state["nodes"]):
            state["nodes"].append({"id": "n000-golden", "kind": "golden",
                                   "status": gi.get("status"), "ts": now_iso()})
        save_state(state)
        log(f"从 golden_init.json 恢复 baseline: {json.dumps(gi['baseline'], ensure_ascii=False)}")

    gr = read_json(RES / "golden_run.json") or {}
    if gr.get("adopted") and not (state.get("best") or {}).get("node"):
        model = str(NODES / str(gr.get("id") or "n000-golden-run") / "model")
        if Path(model).is_dir():
            state["best"] = {"node": gr["id"], "score": gr.get("score"),
                             "model": model, "metrics": gr.get("metrics") or {}}
            if not any(n.get("id") == gr["id"] for n in state["nodes"]):
                state["nodes"].append({"id": gr["id"], "kind": "golden_run",
                                       "adopted": True, "score": gr.get("score"),
                                       "delta": gr.get("delta"), "ts": now_iso()})
            save_state(state)
            log(f"从 golden_run.json 恢复主干 best: {gr['id']} score={gr.get('score')}")
        else:
            log(f"WARN golden_run.json 记录已采纳，但模型目录不存在：{model}")


def main() -> int:
    global _DEADLINE
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    total_h = timer_remaining_h()
    _DEADLINE = time.time() + total_h * 3600
    reserve_h = total_h * RESERVE_FRAC
    bootstrap()
    state = load_state()
    log(f"总预算 {total_h:.2f}h（收尾保留 {reserve_h:.2f}h）cli={CLI} model={CLI_MODEL} dry_run={DRY}")

    if not (RES / "golden_init.json").is_file():
        # step0_golden_init 现在只在时间上让步：缺协议/缺基线/缺文献/配方不全都会降级
        # 继续（2026-09-10 用户决策：编排器是辅助不是监管），所以这里不再有"直接收尾"。
        if not step0_golden_init(state, round(total_h * GOLDEN_FRAC * 60)):
            log("Golden Init 没写出 golden_init.json（异常路径）——带着空开局进循环")
    state = load_state()
    adopt_golden_into_state(state)
    state = load_state()

    # Golden Run：Golden Init 之后先把配方跑完、建立主干，再进循环。
    # 流程记录.md「黄金的流程」：初步 SFT 建立稳定策略 -> 才做"评测 -> bad case ->
    # 单机制修改 -> 回滚/晋级"。成功后 recipe_stable() 成立，第一轮直接是 climb_wide；
    # 失败则 best 仍为空，第一轮照旧 bootstrap 走大步（bootstrap 从此是兜底路径）。
    if GOLDEN_RUN and not (RES / "golden_run.json").is_file():
        cap_h = min(total_h * GOLDEN_RUN_FRAC,
                    max(0.0, remaining_h() - reserve_h - 0.3))
        if cap_h >= 0.5:
            step0_golden_run(state, int(cap_h * 60))
            state = load_state()
        else:
            log(f"剩余预算不足（可用 {cap_h:.2f}h < 0.5h），跳过 Golden Run，直接进循环")

    repair_hint = ""
    while True:
        left_h = remaining_h() - reserve_h
        frac_left = max(0.0, remaining_h() / total_h)
        if left_h <= 0.3:
            log(f"剩余预算 {left_h:.2f}h 不足以再跑一个节点，收尾")
            break
        if len(state["nodes"]) >= MAX_NODES:
            log("节点数达上限，收尾")
            break

        if state.get("consec_rejects", 0) >= 3 and not state.get("measure_pending"):
            # 连续拿不到可执行猜想不是收尾的理由（2026-09-10 用户决策）：按流程记录.md
            # 的回流箭头先把度量做细，再继续猜。唯一的硬边界还是墙钟与节点数上限。
            log(f"连续 {state['consec_rejects']} 轮没拿到可执行猜想 —— 下一轮先回 Step1 "
                f"把度量做细，继续跑")
            state["measure_pending"] = True
            state["measure_reason"] = f"连续 {state['consec_rejects']} 轮猜想全否"
            state["measure_anchor"] = -1
            save_state(state)

        state["round"] = state.get("round", 0) + 1
        sched = schedule(state, frac_left, left_h)
        log(f"==== Round {state['round']}｜regime={sched['mode']}｜"
            f"步长 {'/'.join(sched['sizes'])}｜配方成立={sched['recipe_stable']}｜"
            f"剩余 {left_h:.2f}h（收尾保留外全部可用，时长由节点自决） ====")
        save_state(state)

        def wall_min() -> int:
            return int(max(1.0, remaining_h() - reserve_h) * 60)

        # measure_pending（MEASURE_FIRST 开局置位，或猜想连续被否后回流置位）：先做一次
        # 行为诊断再提猜想，诊断正文经 latest_measurement_text 注入本轮 hypotheses
        # （流程记录.md 的回流箭头）。should_trigger_measurement 负责 MAX_MEASURE / 去重把关。
        if state.get("measure_pending") and left_h > 1.0:
            do_measure, m_reason = should_trigger_measurement(state)
            if do_measure:
                step2_measurement(state, state.get("measure_reason") or m_reason, wall_min())
                state = load_state()
            else:
                state["measure_pending"] = False
                state["measure_reason"] = None
                save_state(state)

        hypo_min = wall_min()
        plans = assign_plans(state, sched)
        for p in plans:
            p["regime"] = sched["mode"]
        cands = step1_hypotheses(state, sched, plans, hypo_min, repair_hint)
        repair_hint = ""
        state = load_state()
        cands, hard_rejects = prefilter_candidates(cands, sched, state, sched["cost_cap_h"])
        if not cands:
            state["rejected_rounds"] = state.get("rejected_rounds", 0) + 1
            state["consec_rejects"] = state.get("consec_rejects", 0) + 1
            why = "；".join(f"{r['id']}: {r['reason']}" for r in hard_rejects) or "全部节点无产物"
            journal_append(f"## round {state['round']} 硬规则全否 [{now_iso()}]\n- {why}")
            log(f"本轮所有候选被硬规则判废：{why}")
            
            # 全否触发 Step2 Measurement 诊断升级
            should_measure, m_reason = should_trigger_measurement(state)
            if should_measure and left_h > 1.0:
                log(f"触发 Step2 度量升级与深度行为诊断：{m_reason}")
                meas_res = step2_measurement(state, f"硬规则全否：{why}", wall_min())
                state = load_state()
                repair_hint = f"上一轮候选全被硬规则否决。已升级度量诊断，请针对诊断出的瓶颈重新提出假设。"
            else:
                repair_hint = f"上一轮所有候选被硬规则判废：{why}"
            save_state(state)
            continue

        chosen, why, insufficient = step1_judge(state, cands, sched, wall_min())
        state = load_state()
        if chosen is None:
            state["rejected_rounds"] = state.get("rejected_rounds", 0) + 1
            state["consec_rejects"] = state.get("consec_rejects", 0) + 1
            journal_append(f"## round {state['round']} judge 全否 [{now_iso()}]\n"
                           f"- 理由: {why}\n- 归因: {insufficient or 'quality'}")
            log(f"judge 全否：{why}")

            # 全否触发 Step2 Measurement 诊断升级
            should_measure, m_reason = should_trigger_measurement(state)
            if should_measure and left_h > 1.0:
                log(f"候选全被 Judge 否决，进入 Step2 深度度量诊断：{m_reason}")
                meas_res = step2_measurement(state, f"Judge全否：{why}", wall_min())
                state = load_state()
                repair_hint = (f"上一轮所有猜想未被 Judge 采纳（{why}）。"
                               f"最新度量诊断已提取出深层弱点，请务必针对诊断中的具体事实制定假说。")
            else:
                repair_hint = str(why or "上一轮候选全未通过 Judge 评判")
            save_state(state)
            continue
        state["consec_rejects"] = 0
        save_state(state)

        # Step3: 端到端实验落地（方案规划 + 运行前代码与配置自查 + 占卡训练与统一评测）
        est_h = float(chosen.get("cost_estimate_h") or 1.0)
        cost_h = min(est_h, chosen["_budget_cap_h"],
                     max(0.4, remaining_h() - reserve_h - 0.3))

        result = step3_experiment(state, chosen, sched, int(cost_h * 60))
        if result is None:
            result = {"status": "failed", "metrics_dev": None,
                      "what_actually_happened": "Step3 Experiment 未产出合法 experiment.md"
                                                "（见 experiment.stream.jsonl）"}
        step5_record(state, chosen, result)
        state = load_state()

    finalize(state)
    b = state["best"]
    bl = state.get("baseline") or {}
    log(f"==== 结束｜best={b.get('node')} score={b.get('score')} "
        f"baseline={score_of(bl.get('official')) or score_of(bl.get('ruler_metrics'))} ====")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GatewayDown as exc:
        log(f"网关持续不可用（{exc}）：停止研究循环，用当前 best 收尾")
        finalize(load_state())
        sys.exit(4)
    except Exception:
        import traceback
        traceback.print_exc()
        try:
            finalize(load_state())     # 崩溃也要留下可评测的 final_model
        finally:
            sys.exit(1)
