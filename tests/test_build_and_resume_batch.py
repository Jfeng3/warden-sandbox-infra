from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_and_resume_batch.py"
SPEC = importlib.util.spec_from_file_location("build_and_resume_batch", SCRIPT_PATH)
assert SPEC and SPEC.loader
build_and_resume_batch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = build_and_resume_batch
SPEC.loader.exec_module(build_and_resume_batch)


BATCH_RUN_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TASK_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class BuildAndResumeBatchTests(unittest.TestCase):
    def test_parse_adopt_entry_uses_task_identity(self) -> None:
        entry = build_and_resume_batch.parse_adopt_entry(
            f"{TASK_ID}|post-one|INK-147"
        )
        self.assertEqual(entry.task_id, TASK_ID)
        self.assertEqual(entry.post_id, "post-one")
        self.assertEqual(entry.superseded_issue_identifier, "INK-147")

    def test_batch_ticket_uses_one_batch_run_id_and_lists_post_task_mapping(self) -> None:
        calls: list[tuple[str, dict]] = []

        def graphql(query: str, variables: dict) -> dict:
            calls.append((query, variables))
            if "projects(" in query:
                return {
                    "projects": {
                        "nodes": [
                            {
                                "id": "project-id",
                                "name": "blog_e2b_run",
                                "teams": {"nodes": [{"id": "team-id"}]},
                            }
                        ]
                    }
                }
            return {
                "issueCreate": {
                    "success": True,
                    "issue": {
                        "id": BATCH_RUN_ID,
                        "identifier": "INK-177",
                        "url": "https://linear.app/issue/INK-177",
                        "title": "Batch",
                    },
                }
            }

        entry = build_and_resume_batch.BatchEntry(TASK_ID, "post-one", "INK-147")
        with patch.object(build_and_resume_batch, "linear_graphql", side_effect=graphql):
            issue = build_and_resume_batch.create_batch_ticket(
                batch_run_id=BATCH_RUN_ID,
                project_name="blog_e2b_run",
                branch="main",
                commit="13ca4b10" * 5,
                template="template-v2",
                worker_id="e2b:worker",
                job="buying-guide-pipeline",
                from_step="freeze-coverage-contract-step",
                snapshot_sources=[],
                artifact_source=None,
                entries=[entry],
                started_at="2026-08-25T00:00:00+00:00",
            )

        self.assertEqual(issue["id"], BATCH_RUN_ID)
        create_input = calls[1][1]["input"]
        self.assertEqual(create_input["id"], BATCH_RUN_ID)
        description = create_input["description"]
        self.assertIn(f"Run ID: `{BATCH_RUN_ID}`", description)
        self.assertIn(f"| post-one | `{TASK_ID}` | INK-147 |", description)
        self.assertIn("Warden task ID", description)
        self.assertNotIn("Warden run ID", description)

    def test_marks_each_old_ticket_duplicate_of_batch_ticket(self) -> None:
        entries = [
            build_and_resume_batch.BatchEntry(TASK_ID, "post-one", "INK-147"),
            build_and_resume_batch.BatchEntry(
                "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                "post-two",
                "INK-148",
            ),
        ]
        calls: list[dict] = []

        def graphql(_query: str, variables: dict) -> dict:
            calls.append(variables)
            return {
                "issueRelationCreate": {
                    "success": True,
                    "issueRelation": {"id": "relation-id", "type": "duplicate"},
                }
            }

        with patch.object(build_and_resume_batch, "linear_graphql", side_effect=graphql):
            build_and_resume_batch.mark_superseded_issues_duplicate("INK-177", entries)

        self.assertEqual(
            [call["input"] for call in calls],
            [
                {
                    "issueId": "INK-147",
                    "relatedIssueId": "INK-177",
                    "type": "duplicate",
                },
                {
                    "issueId": "INK-148",
                    "relatedIssueId": "INK-177",
                    "type": "duplicate",
                },
            ],
        )

    def test_launch_starts_parallel_controller_for_worker(self) -> None:
        class Controller:
            pid = 12345

        with patch.dict(
            build_and_resume_batch.os.environ,
            {"WARDEN_MAX_CONCURRENT_TASKS": "7", "WARDEN_CODEX_AUTH_PATH": "/tmp/jupiter-auth.json"},
            clear=True,
        ), patch.object(
            build_and_resume_batch,
            "build_controller_env",
            side_effect=lambda source, _infra_env, _warden_env: dict(source),
        ), patch.object(build_and_resume_batch.subprocess, "Popen", return_value=Controller()) as popen:
            build_and_resume_batch.launch_tasks(
                [TASK_ID, "cccccccc-cccc-4ccc-8ccc-cccccccccccc"],
                "template-v2",
                "e2b:worker",
                Path("/tmp/warden"),
            )

        command = popen.call_args.args[0]
        env = popen.call_args.kwargs["env"]
        self.assertEqual(command, [build_and_resume_batch.sys.executable, "-m", "warden_sandbox_infra", "run"])
        self.assertEqual(env["E2B_TEMPLATE"], "template-v2")
        self.assertEqual(env["WARDEN_WORKER_ID"], "e2b:worker")
        self.assertEqual(env["WARDEN_SANDBOX_RUNTIME"], "e2b")
        self.assertEqual(env["WARDEN_WORKER_CWD"], "/workspace/warden")
        self.assertEqual(env["WARDEN_WORKER_COMMAND"], 'npm run warden -- worker-task --task-id "$WARDEN_TASK_ID"')
        self.assertEqual(env["WARDEN_MAX_CONCURRENT_TASKS"], "7")
        self.assertEqual(env["WARDEN_CODEX_AUTH_PATH"], "/tmp/jupiter-auth.json")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_launch_defaults_parallel_controller_to_twenty_tasks(self) -> None:
        class Controller:
            pid = 12345

        with patch.dict(
            build_and_resume_batch.os.environ,
            {},
            clear=True,
        ), patch.object(
            build_and_resume_batch,
            "build_controller_env",
            side_effect=lambda source, _infra_env, _warden_env: dict(source),
        ), patch.object(build_and_resume_batch.subprocess, "Popen", return_value=Controller()) as popen:
            build_and_resume_batch.launch_tasks([TASK_ID], "template-v2", "e2b:worker", Path("/tmp/warden"))

        self.assertEqual(popen.call_args.kwargs["env"]["WARDEN_MAX_CONCURRENT_TASKS"], "20")


if __name__ == "__main__":
    unittest.main()
