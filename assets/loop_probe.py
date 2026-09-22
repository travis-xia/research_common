"""隔离验证：用真实 CLI 跑一轮 Step1 并行猜想（坐标互斥 + 步长下发）+ 硬规则 + judge。
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

plans = m.assign_plans(st, sched)
for p in plans:
    print(f"    cand-{p['idx']}: layers={p['layers']}")

cands = m.step1_hypotheses(st, sched, plans, 14)
m.log(f"产出候选 {len(cands)}")
for c in cands:
    tgt = c.get("_target_key") or c.get("target")
    print("   ", c["_id"], "|", c.get("layer"), "|",
          tgt, "|", str(c.get("title"))[:60], "| cost", c.get("cost_estimate_h"),
          "| min_viable", c.get("min_viable_h"))

cands, rejects = m.prefilter_candidates(cands, sched, st, sched["cost_cap_h"])
for r in rejects:
    print("    判废:", r["id"], "-", r["reason"])

if cands:
    chosen, why, kind = m.step1_judge(st, cands, sched, 14)
    m.log(f"judge 选中={chosen['_id'] if chosen else None}｜否决={str(why)[:200]}｜归因={kind}")
    if chosen:
        print("    verification:", json.dumps(chosen.get("_review"), ensure_ascii=False)[:400])
