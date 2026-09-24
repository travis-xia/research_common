"""Offline regression tests for deadline and JSON recovery interfaces."""

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


AGENT_DIR = Path(__file__).resolve().parents[1]


class OrchestratorRecoveryTests(unittest.TestCase):
    """Keep every artifact in a fresh temporary task, without real CLI calls."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            prefix=".orch-recovery-", dir=Path(__file__).parent
        )
        self.addCleanup(self.tmp.cleanup)
        self.task = Path(self.tmp.name)
        spec = importlib.util.spec_from_file_location(
            "orch_recovery_test", AGENT_DIR / "orchestrator.py"
        )
        self.orch = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {
            "RESEARCH_AGENT_DIR": str(AGENT_DIR),
            "RESEARCH_DRY_RUN": "1",
        }):
            spec.loader.exec_module(self.orch)
        self.orch.TASK_DIR = self.task
        self.orch.RES = self.task / "research"
        self.orch.NODES = self.orch.RES / "nodes"
        for name, filename in {
            "STATE_F": "state.json",
            "SETTINGS": "settings.json",
            "JOURNAL": "journal.md",
            "BANK_PATH": "experience_bank.md",
            "PHASE_F": ".phase",
        }.items():
            setattr(self.orch, name, self.orch.RES / filename)
        self.orch.RES.mkdir()
        self.orch.NODES.mkdir()
        self.log = patch.object(self.orch, "log").start()
        self.addCleanup(patch.stopall)

    def make_state(self):
        state = self.orch.default_state()
        state["round"] = 3
        state["seq"] = 12
        state["best"] = {
            "node": "n010-hyp-c0",
            "score": 0.7,
            "model": str(self.orch.NODES / "n010-hyp-c0" / "model"),
        }
        state["nodes"] = [{"id": "n010-hyp-c0", "kind": "exp"}]
        return state

    def test_missing_optional_json_remains_quiet(self):
        self.assertIsNone(self.orch.read_json(self.task / "missing.json"))
        self.log.assert_not_called()

    def test_malformed_optional_json_is_logged(self):
        artifact = self.task / "bad.json"
        artifact.write_text("{bad", encoding="utf-8")
        self.assertIsNone(self.orch.read_json(artifact))
        self.assertIn(str(artifact), str(self.log.call_args))
        self.assertEqual(artifact.read_text(encoding="utf-8"), "{bad")

    def test_permission_error_is_logged(self):
        with patch.object(Path, "read_text", side_effect=PermissionError):
            self.assertIsNone(self.orch.read_json(self.orch.STATE_F))
        self.log.assert_called()

    def test_non_utf8_json_is_logged_without_overwriting(self):
        artifact = self.task / "bad-encoding.json"
        artifact.write_bytes(b"\xff")
        self.assertIsNone(self.orch.read_json(artifact))
        self.assertEqual(artifact.read_bytes(), b"\xff")
        self.log.assert_called()

    def test_deeply_nested_json_is_logged_without_aborting(self):
        artifact = self.task / "too-deep.json"
        depth = 10000
        raw = "[" * depth + "0" + "]" * depth
        artifact.write_text(raw, encoding="utf-8")
        self.assertIsNone(self.orch.read_json(artifact))
        self.assertEqual(artifact.read_text(encoding="utf-8"), raw)
        self.log.assert_called()

    def test_bad_state_recovers_last_saved_snapshot_after_restart(self):
        state = self.make_state()
        self.orch.save_state(state)
        self.orch._LAST_GOOD_STATE = None
        self.orch.STATE_F.write_text("{broken", encoding="utf-8")
        recovered = self.orch.load_state()
        self.assertEqual(recovered["best"], state["best"])
        self.assertEqual(recovered["round"], 3)
        evidence = list(self.orch.RES.glob("state.json.corrupt-*"))
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].read_text(encoding="utf-8"), "{broken")
        self.orch.save_state(recovered)
        self.assertEqual(self.orch.load_state()["best"], state["best"])

    def test_memory_snapshot_is_not_mutated_by_caller(self):
        state = self.make_state()
        self.orch.save_state(state)
        loaded = self.orch.load_state()
        loaded["best"]["score"] = 0.1
        self.orch.STATE_F.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.orch.load_state()["best"]["score"], 0.7)

    def test_missing_primary_can_recover_backup(self):
        state = self.make_state()
        self.orch.save_state(state)
        self.orch._LAST_GOOD_STATE = None
        self.orch.STATE_F.unlink()
        self.assertEqual(self.orch.load_state()["best"], state["best"])

    def test_valid_json_non_object_state_recovers_backup(self):
        state = self.make_state()
        self.orch.save_state(state)
        self.orch._LAST_GOOD_STATE = None
        self.orch.STATE_F.write_text("[]", encoding="utf-8")
        self.assertEqual(self.orch.load_state()["best"], state["best"])
        evidence = list(self.orch.RES.glob("state.json.corrupt-*"))
        self.assertEqual(evidence[0].read_text(encoding="utf-8"), "[]")

    def test_bad_state_containers_cannot_poison_snapshot(self):
        for key, value in (("best", None), ("nodes", {})):
            with self.subTest(key=key):
                state = self.make_state()
                self.orch.save_state(state)
                broken = {**state, key: value}
                self.orch.write_json(self.orch.STATE_F, broken)
                loaded = self.orch.load_state()
                self.assertEqual(loaded["best"], state["best"])
                self.assertEqual(loaded["nodes"], state["nodes"])
                self.orch.STATE_F.write_text("{broken", encoding="utf-8")
                self.assertEqual(self.orch.load_state()["best"], state["best"])
                backup = self.orch.read_json(
                    self.orch.STATE_F.with_suffix(".json.bak")
                )
                self.assertEqual(backup["best"], state["best"])

    def test_transient_backup_read_failure_preserves_backup_for_retry(self):
        state = self.make_state()
        self.orch.save_state(state)
        self.orch._LAST_GOOD_STATE = None
        self.orch.STATE_F.write_text("{broken", encoding="utf-8")
        backup = self.orch.STATE_F.with_suffix(".json.bak")
        original = backup.read_bytes()
        read_text = Path.read_text

        def fail_backup(path, *args, **kwargs):
            if path == backup:
                raise OSError("transient read error")
            return read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", new=fail_backup):
            self.orch.load_state()
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(self.orch.load_state()["best"], state["best"])

    def test_save_during_backup_read_failure_does_not_destroy_old_backup(self):
        state = self.make_state()
        self.orch.save_state(state)
        self.orch._LAST_GOOD_STATE = None
        self.orch.STATE_F.write_text("{broken", encoding="utf-8")
        backup = self.orch.STATE_F.with_suffix(".json.bak")
        original = backup.read_bytes()
        read_text = Path.read_text

        def fail_backup(path, *args, **kwargs):
            if path == backup:
                raise OSError("transient read error")
            return read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", new=fail_backup):
            loaded = self.orch.load_state()
            loaded["seq"] = 123
            self.orch.save_state(loaded)
        self.assertEqual(backup.read_bytes(), original)
        recovered = self.orch.load_state()
        self.assertEqual(recovered["best"], state["best"])
        self.assertGreaterEqual(recovered["seq"], 123)
        self.assertNotIn("_recovery_backup_pending", recovered)
        evidence = list(self.orch.RES.glob("state.json.recovery-*"))
        self.assertEqual(len(evidence), 1)
        self.assertEqual(self.orch.read_json(evidence[0])["seq"], 123)
        self.assertFalse(self.orch._STATE_BACKUP_PROTECTED)

    def test_pending_recovery_survives_a_fresh_process_import(self):
        state = self.make_state()
        self.orch.save_state(state)
        self.orch._LAST_GOOD_STATE = None
        self.orch.STATE_F.write_text("{broken", encoding="utf-8")
        backup = self.orch.STATE_F.with_suffix(".json.bak")
        read_text = Path.read_text

        def fail_backup(path, *args, **kwargs):
            if path == backup:
                raise OSError("transient read error")
            return read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", new=fail_backup):
            self.orch.save_state(self.orch.load_state())
        spec = importlib.util.spec_from_file_location(
            "orch_recovery_restarted", AGENT_DIR / "orchestrator.py"
        )
        restarted = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"RESEARCH_AGENT_DIR": str(AGENT_DIR)}):
            spec.loader.exec_module(restarted)
        for name in ("TASK_DIR", "RES", "NODES", "STATE_F"):
            setattr(restarted, name, getattr(self.orch, name))
        restarted.log = MagicMock()
        self.assertFalse(restarted._STATE_BACKUP_PROTECTED)
        recovered = restarted.load_state()
        self.assertEqual(recovered["best"], state["best"])
        self.assertNotIn("_recovery_backup_pending", recovered)
        restarted.save_state(recovered)
        self.assertEqual(restarted.read_json(backup)["best"], state["best"])
        self.assertFalse(restarted._STATE_BACKUP_PROTECTED)

    def test_stale_backup_does_not_reuse_newer_node_ids(self):
        state = self.make_state()
        self.orch.save_state(state)
        self.orch._LAST_GOOD_STATE = None
        self.orch.STATE_F.write_text("{broken", encoding="utf-8")
        (self.orch.NODES / "n099-hyp-c0").mkdir()
        recovered = self.orch.load_state()
        self.assertEqual(recovered["best"], state["best"])
        self.assertEqual(self.orch.new_nid(recovered, "hyp"), "n100-hyp")

    def test_legacy_state_preserves_fields_and_recovers_sequence(self):
        state = self.make_state()
        del state["seq"]
        del state["measure_anchor"]
        self.orch.write_json(self.orch.STATE_F, state)
        loaded = self.orch.load_state()
        self.assertEqual(loaded["best"], state["best"])
        self.assertEqual(loaded["round"], 3)
        self.assertEqual(loaded["seq"], 10)
        self.assertEqual(loaded["measure_anchor"], -1)

    def test_no_snapshot_preserves_bad_state_and_existing_node_ids(self):
        self.orch.STATE_F.write_text("{broken", encoding="utf-8")
        (self.orch.NODES / "n042-hyp-c0").mkdir()
        state = self.orch.load_state()
        self.assertGreaterEqual(state["seq"], 42)
        self.assertTrue(list(self.orch.RES.glob("state.json.corrupt-*")))
        self.assertEqual(self.orch.new_nid(state, "hyp"), "n043-hyp")

    def test_backup_write_failure_does_not_abort_primary_save(self):
        state = self.make_state()
        write_json = self.orch.write_json

        def write_with_failed_backup(path, obj):
            if path != self.orch.STATE_F:
                raise OSError("backup is unavailable")
            return write_json(path, obj)

        with patch.object(
            self.orch, "write_json", side_effect=write_with_failed_backup
        ):
            self.orch.save_state(state)
            self.log.assert_called()
            # 主文件再次损坏时，备份失败也不能污染最近的内存快照。
            state["best"]["score"] = 0.1
            self.orch.STATE_F.write_text("{broken", encoding="utf-8")
            self.assertEqual(self.orch.load_state()["best"]["score"], 0.7)
        self.assertEqual(self.orch.load_state()["best"]["score"], 0.7)

    def test_good_primary_does_not_depend_on_bad_backup(self):
        state = self.make_state()
        self.orch.save_state(state)
        self.orch._LAST_GOOD_STATE = None
        self.orch.STATE_F.with_suffix(".json.bak").write_text(
            "{broken", encoding="utf-8"
        )
        self.assertEqual(self.orch.load_state()["best"], state["best"])

    def test_expired_budget_finishes_without_starting_nodes(self):
        for hours in (0.0, -1.0):
            with self.subTest(hours=hours):
                self.orch._FINALIZED.clear()
                state = self.make_state()
                with (
                    patch.object(self.orch.signal, "signal"),
                    patch.object(self.orch, "timer_remaining_h",
                                 return_value=hours),
                    patch.object(self.orch, "bootstrap"),
                    patch.object(self.orch, "load_state", return_value=state),
                    patch.object(self.orch, "step0_golden_init") as init,
                    patch.object(self.orch, "step0_golden_run") as golden,
                    patch.object(self.orch, "step1_hypotheses") as hypo,
                    patch.object(self.orch, "finalize") as finalize,
                ):
                    self.assertEqual(self.orch.main(), 0)
                    init.assert_not_called()
                    golden.assert_not_called()
                    hypo.assert_not_called()
                    finalize.assert_called_once_with(state)

    def test_expired_budget_delivers_best_from_recovered_snapshot(self):
        state = self.make_state()
        model = Path(state["best"]["model"])
        model.mkdir(parents=True)
        (model / "config.json").write_text('{"recovered": true}', encoding="utf-8")
        self.orch.save_state(state)
        self.orch._LAST_GOOD_STATE = None
        self.orch.STATE_F.write_text("{broken", encoding="utf-8")
        with (
            patch.object(self.orch.signal, "signal"),
            patch.object(self.orch, "timer_remaining_h", return_value=0),
            patch.object(self.orch, "run_cli",
                         side_effect=AssertionError("expired task launched CLI")),
        ):
            self.assertEqual(self.orch.main(), 0)
        self.assertEqual(
            json.loads((self.task / "final_model" / "config.json").read_text()),
            {"recovered": True},
        )
        summary = self.orch.read_json(self.orch.RES / "summary.json")
        self.assertEqual(summary["best"], state["best"])

    def test_expired_budget_can_restore_existing_golden_model(self):
        golden_model = self.orch.NODES / "n000-golden-run" / "model"
        golden_model.mkdir(parents=True)
        (golden_model / "config.json").write_text('{"golden": true}', encoding="utf-8")
        self.orch.write_json(self.orch.RES / "golden_run.json", {
            "id": "n000-golden-run", "adopted": True, "score": 0.55,
        })
        base = self.task / "base"
        base.mkdir()
        (base / "config.json").write_text('{"base": true}', encoding="utf-8")
        with (
            patch.object(self.orch.signal, "signal"),
            patch.object(self.orch, "timer_remaining_h", return_value=0),
            patch.object(self.orch, "resolve_base_model", return_value=base),
            patch.object(self.orch, "run_cli",
                         side_effect=AssertionError("expired task launched CLI")),
        ):
            self.assertEqual(self.orch.main(), 0)
        self.assertEqual(
            json.loads((self.task / "final_model" / "config.json").read_text()),
            {"golden": True},
        )

    def run_fake_cli(self, lines):
        node = self.orch.NODES / "event-test"
        node.mkdir(exist_ok=True)
        proc = MagicMock()
        proc.stdout = io.StringIO("".join(lines))
        proc.wait.return_value = 1
        with (
            patch.object(self.orch, "DRY", False),
            patch.object(self.orch, "NODE_HARD_KILL", False),
            patch.object(self.orch, "cli_cmd", return_value=(["fake"], {})),
            patch.object(self.orch.subprocess, "Popen", return_value=proc),
        ):
            result = self.orch.run_cli(
                "test", node, "test", 1, "hypothesis"
            )
        stream = node / "test.stream.jsonl"
        self.assertEqual(stream.read_text(encoding="utf-8"), "".join(lines))
        return result

    def test_malformed_failure_event_uses_retry_classification(self):
        rc, kind = self.run_fake_cli(['{"type": "turn.failed", broken\n'])
        self.assertEqual((rc, kind), (1, "api"))
        self.assertIn("JSON", str(self.log.call_args_list))

    def test_deeply_nested_event_does_not_abort_stream(self):
        depth = 10000
        nested = "[" * depth + "0" + "]" * depth
        rc, kind = self.run_fake_cli([
            '{"type": "turn.failed", "error": ' + nested + '}\n',
            '{"type": "turn.completed"}\n',
        ])
        self.assertEqual((rc, kind), (1, "api"))
        self.assertIn("JSON", str(self.log.call_args_list))

    def test_stream_continues_after_bad_event(self):
        rc, kind = self.run_fake_cli([
            '{"type":"result", broken\n',
            json.dumps({
                "type": "result", "is_error": True,
                "api_error_status": 503,
            }) + "\n",
        ])
        self.assertEqual((rc, kind), (1, "api"))

    def test_spaced_error_event_is_not_lost_by_later_completion(self):
        rc, kind = self.run_fake_cli([
            '{"type" : "error", "message": "unavailable"}\n',
            '{"type": "turn.completed"}\n',
        ])
        self.assertEqual((rc, kind), (1, "api"))

    def test_malformed_claude_api_failure_uses_retry_classification(self):
        rc, kind = self.run_fake_cli([
            '{"type": "result", "is_error": true, "api_error_status": 503,\n',
        ])
        self.assertEqual((rc, kind), (1, "api"))

    def test_malformed_failure_fields_can_be_reordered(self):
        for line in (
            '{"message": "unavailable", "type": "error", broken\n',
            '{"id": "abc", "type": "turn.failed", broken\n',
            '{"is_error": true, "api_error_status": 503, "type": "result", broken\n',
        ):
            with self.subTest(line=line):
                self.assertEqual(self.run_fake_cli([line]), (1, "api"))

    def test_malformed_nested_error_is_not_a_terminal_event(self):
        rc, kind = self.run_fake_cli([
            '{"type": "item.completed", "item": {"type": "error"}, broken\n',
        ])
        self.assertEqual((rc, kind), (1, None))

    def test_nested_error_is_not_a_terminal_event(self):
        rc, kind = self.run_fake_cli([
            '{"type": "item.completed", "item": {"type": "error"}}\n',
            '{"type": "turn.completed"}\n',
        ])
        self.assertEqual((rc, kind), (1, None))

    def test_bad_optional_usage_does_not_abort_terminal_parsing(self):
        rc, kind = self.run_fake_cli([
            '{"type": "turn.completed", "usage": [1, 2]}\n',
        ])
        self.assertEqual((rc, kind), (1, None))

    def test_bad_completed_event_is_logged_without_becoming_api_failure(self):
        rc, kind = self.run_fake_cli([
            '{"type":"turn.completed", broken\n',
            '{"type":"turn.completed","usage":{"input_tokens":1}}\n',
        ])
        self.assertEqual((rc, kind), (1, None))
        self.assertIn("JSON", str(self.log.call_args_list))

    def test_bad_failure_event_retries_node_and_accepts_next_artifact(self):
        node = self.orch.NODES / "retry-test"
        node.mkdir()
        contract = node / "result.json"
        failed = MagicMock()
        failed.stdout = io.StringIO('{"type": "turn.failed", broken\n')
        failed.wait.return_value = 1
        succeeded = MagicMock()
        succeeded.stdout = io.StringIO('{"type": "turn.completed"}\n')

        def finish():
            contract.write_text('{"status": "ok"}', encoding="utf-8")
            return 0

        succeeded.wait.side_effect = finish
        with (
            patch.object(self.orch, "DRY", False),
            patch.object(self.orch, "NODE_HARD_KILL", False),
            patch.object(self.orch, "API_RETRIES", 1),
            patch.object(self.orch, "API_GIVEUP", 6),
            patch.object(self.orch, "build_prompt", return_value="test"),
            patch.object(self.orch, "cli_cmd", return_value=(["fake"], {})),
            patch.object(self.orch.time, "sleep"),
            patch.object(self.orch.subprocess, "Popen",
                         side_effect=[failed, succeeded]) as popen,
        ):
            result = self.orch.run_node(
                "unused.md", node, contract, "probe", "hypothesis",
                ["status"], self.make_state(), retries=0,
            )
        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(popen.call_count, 2)
        self.assertTrue((node / "probe.api1.stream.jsonl").is_file())
        self.assertEqual(
            (node / "probe.stream.jsonl").read_text(),
            '{"type": "turn.failed", broken\n',
        )

    def test_dry_pipeline_restart_and_expired_delivery(self):
        """Run the real main in new processes, using only the built-in dry stubs."""
        for cli in ("claude", "codex"):
            with self.subTest(cli=cli), tempfile.TemporaryDirectory(
                prefix=f"{cli}-", dir=self.task
            ) as task_name:
                task = Path(task_name)
                base_model = task / "base-model"
                base_model.mkdir()
                (base_model / "config.json").write_text(
                    '{"architectures": ["Qwen3ForCausalLM"]}', encoding="utf-8"
                )
                env = {
                    **os.environ,
                    "RESEARCH_AGENT_DIR": str(AGENT_DIR),
                    "RESEARCH_DRY_RUN": "1",
                    "RESEARCH_CLI": cli,
                    "RESEARCH_MAX_NODES": "8",
                    "RESEARCH_N_HYPO": "3",
                    "RESEARCH_GOLDEN_RUN": "1",
                    "RESEARCH_DRY_GOLDEN_FAIL": "0",
                    "RESEARCH_MEASURE_FIRST": "1",
                    "REPO_ROOT": str(AGENT_DIR.parents[1]),
                    "NUM_HOURS": "10",
                    "MODEL": str(base_model),
                }

                def run():
                    result = subprocess.run(
                        [sys.executable, "-B", str(AGENT_DIR / "orchestrator.py")],
                        cwd=task, env=env, capture_output=True, text=True,
                        timeout=30,
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertTrue((task / "final_model" / "config.json").is_file())
                    return result.stdout

                run()
                research = task / "research"
                state_file = research / "state.json"
                saved = json.loads(state_file.read_text(encoding="utf-8"))
                self.assertIsNotNone(saved["best"]["node"])
                summary = json.loads(
                    (research / "summary.json").read_text(encoding="utf-8")
                )
                self.assertGreater(summary["n_exp_nodes"], 0)
                golden = (research / "golden_init.json").read_bytes()

                # 模拟进程重启前主状态损坏，磁盘还留有编号更新的孤立节点。
                state_file.write_text("{broken smoke", encoding="utf-8")
                (research / "nodes" / "n099-hyp-c0").mkdir()
                env["RESEARCH_MAX_NODES"] = "14"
                output = run()
                self.assertIn("恢复 state", output)
                resumed = json.loads(state_file.read_text(encoding="utf-8"))
                self.assertEqual(resumed["best"], saved["best"])
                self.assertGreater(resumed["seq"], 99)
                self.assertGreater(resumed["round"], saved["round"])
                old_ids = {node["id"] for node in saved["nodes"]}
                self.assertTrue(old_ids.issubset(
                    {node["id"] for node in resumed["nodes"]}
                ))
                self.assertEqual((research / "golden_init.json").read_bytes(), golden)
                evidence = list(research.glob("state.json.corrupt-*"))
                self.assertEqual(len(evidence), 1)
                self.assertEqual(
                    evidence[0].read_text(encoding="utf-8"), "{broken smoke"
                )

                # 已到期的启动只交付已有 best，不再增加节点或轮次。
                env["NUM_HOURS"] = "0"
                output = run()
                self.assertIn("总预算已到期", output)
                self.assertEqual(
                    json.loads(state_file.read_text(encoding="utf-8")), resumed
                )
                summary = json.loads(
                    (research / "summary.json").read_text(encoding="utf-8")
                )
                self.assertEqual(summary["best"], resumed["best"])


if __name__ == "__main__":
    unittest.main()
