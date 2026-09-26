"""隔离验证：用真实 CLI 跑一轮 Step1 批量猜想 + judge。
复用已有的 Golden Init 产物，不做训练。"""
import importlib.util
import json
import os

spec = importlib.util.spec_from_file_location(
    "orch", os.environ["RESEARCH_AGENT_DIR"] + "/orchestrator.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

m.bootstrap()
st = m.load_state()
m.adopt_golden_into_state(st)
st = m.load_state()

sched = m.schedule(st, 1.0, 8.0)
m.log(f"regime={sched['mode']} layers={sched['layers']} cap={sched['cost_cap_h']}h")

batch = m.step1_hypotheses(st, sched, 14)
m.log(f"批量猜想产物={batch.get('_hypotheses_file') if batch else None}")

if batch:
    chosen, why, kind = m.step1_judge(st, batch, sched, 14)
    m.log(f"judge 选中={chosen.get('_target_key') if chosen else None}｜否决={str(why)[:200]}｜归因={kind}")
    if chosen:
        print("    verification:", json.dumps(chosen.get("_review"), ensure_ascii=False)[:400])
