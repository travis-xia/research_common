#!/usr/bin/env python3
"""回归测试：guard 只拦真正写入受保护路径的操作。"""

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "assets" / "guard.py"


def run(command: str) -> subprocess.CompletedProcess[str]:
    payload = json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": command},
    })
    env = {**os.environ, "RESEARCH_DIR": str(ROOT / ".test_guard_research")}
    return subprocess.run(
        [sys.executable, str(GUARD)],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
    )


def assert_allowed(command: str) -> None:
    result = run(command)
    assert result.returncode == 0, (command, result.stderr)


def assert_blocked(command: str) -> None:
    result = run(command)
    assert result.returncode == 2, (command, result.stderr)


# 这些命令来自历史 run 的 guard.log。旧版把同一条命令里出现的
# 受保护路径名和重定向符号当成写入；新版必须全部放行。
HISTORICAL_FALSE_POSITIVES = [
    "nvidia-smi 2>/dev/null | head -40; cat timer.sh",
    "bash timer.sh 2>/dev/null; echo exit=$?",
    "echo '=== templates ==='; ls -la templates/ 2>/dev/null",
    "grep --include=evaluate.py -n TODO .",
    "echo 'confirm evaluate.py routes qwen model_path -> qwen3.jinja'",
    "python3 -c \"print('CATEGORY -> baseline'); import os; print(os.listdir('templates/'))\"",
    "cat research/state.json 2>/dev/null; echo '=== journal ==='; head -60 research/journal.md",
    "head -50 research/action_space.json; cat research/step_policy.json",
    "cat research/settings.json; cat research/guard.py | head -60",
    "tail -20 research/guard.log",
    "find research/harness_src -type f | head -80",
    "ls -la research/harness_src/skills/",
    "sed -n '1,60p' research/protocol.md | grep -ni scorer",
    "cp templates/qwen3.jinja research/nodes/n001/",
    "mkdir -p logs && nohup python evaluate.py --model-path Qwen/Qwen3-4B-Base "
    "--limit -1 --json-output-file logs/baseline_metrics.json",
]


def main() -> None:
    for command in HISTORICAL_FALSE_POSITIVES:
        assert_allowed(command)

    # 真正写入/覆盖评测协议或编排账本仍必须拦截。
    assert_blocked("echo x > evaluate.py")
    assert_blocked("tee templates/qwen3.jinja")
    assert_blocked("cp foo evaluate.py")
    assert_blocked("sed -i 's/a/b/' timer.sh")
    assert_blocked("echo x >> research/state.json")
    assert_blocked("cp foo research/guard.py")
    assert_blocked("mv research/harness_src /tmp/harness_src")

    # 历史 run 中的临时产物清理属于 research/，不能放行。
    assert_blocked("rm -rf research/nodes/n001")
    assert_blocked("rm -f research/nodes/n001-measure/_facts.json")
    assert_blocked("rm -rf research/nodes/n000-golden-run/_scratch/probe_model")
    assert_blocked("rm -f final_model/config.json")
    assert_blocked("mv best_model backup_model")
    # 只删除 /tmp 的命令仍可执行。
    assert_allowed("rm -rf /tmp/qwen_smoke /tmp/smoke.jsonl")

    # 历史 run 多次尝试全局搜索；这三个根必须拦，普通子目录不拦。
    assert_blocked("find / -maxdepth 8 -type d -name skills")
    assert_blocked("find /root -maxdepth 3 -name contamination_check.py")
    assert_blocked("find /root/paddlejob/rl-public -maxdepth 4 -iname '*qwen3-4b*'")
    assert_allowed("find ./research -name recipe_guidelines.md")
    assert_allowed("find /root/paddlejob/rl-public/xiasheng01/models -name config.json")

    # 按名字杀进程会波及同机器的其他任务；数字 PID 是唯一允许形式。
    assert_blocked("pkill -f prepare_data.py")
    assert_blocked("pkill -f 'vllm.entrypoints.openai.api_server'")
    assert_blocked("killall python")
    assert_blocked("jobs -p | xargs -r kill")
    assert_blocked("kill $(pgrep -f vllm)")
    assert_blocked("kill `ps aux | grep vllm`")
    assert_allowed("kill 3959461")
    assert_allowed("kill -9 3959461 3957978")
    assert_allowed("pgrep -af 'vllm serve'")

    print("test_guard: ok")


if __name__ == "__main__":
    main()
