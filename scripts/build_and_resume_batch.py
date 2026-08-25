#!/usr/bin/env python3
"""Build a branch-scoped E2B template and launch one batch run of Warden tasks.

The command is dry-run by default. ``--execute`` creates fresh Warden tasks;
``--launch`` additionally starts infra's parallel Warden controller for the
target worker. ``--artifact-run-id`` may provide durable artifact refs from
another task, but only when its client/post identity matches the snapshot source.

Required environment for execution: E2B_API_KEY, SUPABASE_URL,
SUPABASE_SERVICE_ROLE_KEY, and the Warden CLI's normal environment.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

try:
    from scripts.run_warden_task import build_controller_env
except ModuleNotFoundError:  # Direct execution places this file's directory on sys.path.
    from run_warden_task import build_controller_env


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WARDEN_REPO = REPO_ROOT.parent / "warden"
ARTIFACT_STATE_KEYS = (
    "artifact_manifest",
    "artifact_paths",
    "raw_input_path",
    "raw_input_material_path",
    "input_material_path",
    "input_figure_path",
    "normalized_article_contract_path",
    "figure_path",
    "content_brief_path",
    "post_json_object_draft_path",
    "post_json_object_final_path",
    "draft_path",
    "format_draft_path",
    "publish_path",
    "publish_inkwarden_manifest_path",
    "asset_path",
    "infograph_path",
    "infograph_png_path",
)
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
LINEAR_ISSUE_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")


@dataclass(frozen=True)
class Snapshot:
    task_id: str
    row: dict[str, Any]

    @property
    def state(self) -> dict[str, Any]:
        return {key: self.row.get(key) for key in ARTIFACT_STATE_KEYS if self.row.get(key) is not None}

    @property
    def identity(self) -> tuple[str, str]:
        state = self.row
        client = str(state.get("client_slug") or "")
        post = str(state.get("public_slug") or state.get("topic_slug") or "")
        return client, post


@dataclass(frozen=True)
class BatchEntry:
    task_id: str
    post_id: str
    superseded_issue_identifier: str = ""


def parse_adopt_entry(value: str) -> BatchEntry:
    parts = [part.strip() for part in value.split("|")]
    if len(parts) != 3 or not all(parts):
        raise SystemExit("--adopt-entry must be <task-id>|<post-id>|<superseded-Linear-issue>")
    task_id, post_id, issue_identifier = parts
    if not UUID_PATTERN.fullmatch(task_id):
        raise SystemExit(f"Invalid adopted task ID: {task_id}")
    if not LINEAR_ISSUE_PATTERN.fullmatch(issue_identifier):
        raise SystemExit(f"Invalid superseded Linear issue identifier: {issue_identifier}")
    return BatchEntry(task_id=task_id, post_id=post_id, superseded_issue_identifier=issue_identifier)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", action="append", default=[], help="Snapshot source task ID; repeatable")
    parser.add_argument("--fresh-post", action="append", default=[], help="Planned post ID for a brand-new Step-0 task; repeatable")
    parser.add_argument(
        "--adopt-entry",
        action="append",
        default=[],
        help="Existing <task-id>|<post-id>|<superseded-Linear-issue>; repeatable",
    )
    parser.add_argument("--batch-run-id", help="Batch run UUID; generated once per invocation by default")
    parser.add_argument("--from-step", required=True, help="Stable workflow step key, e.g. draft-step")
    parser.add_argument("--job", default="buying-guide-pipeline")
    parser.add_argument("--artifact-run-id", help="Optional run whose latest snapshot supplies artifact refs")
    parser.add_argument("--warden-repo", type=Path, default=DEFAULT_WARDEN_REPO)
    parser.add_argument("--template", help="Template name; default is generated from date/job/branch/commit")
    parser.add_argument("--template-commit", help="Actual Warden commit embedded in an adopted template")
    parser.add_argument("--worker-id", help="Exact target worker for created tasks")
    parser.add_argument("--linear-project", default="blog_e2b_run", help="Linear project for the batch ticket")
    parser.add_argument(
        "--existing-linear-batch-issue",
        help="Reuse an already-created Linear batch issue ID instead of creating another ticket",
    )
    parser.add_argument("--build", action="store_true", help="Build the generated/current template")
    parser.add_argument("--execute", action="store_true", help="Create fresh resume tasks")
    parser.add_argument("--launch", action="store_true", help="Launch created tasks with infra's parallel controller")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    if args.launch and not args.execute:
        parser.error("--launch requires --execute")
    modes = sum(bool(value) for value in (args.run_id, args.fresh_post, args.adopt_entry))
    if modes != 1:
        parser.error("provide exactly one of --run-id, --fresh-post, or --adopt-entry")
    if args.adopt_entry and (args.build or args.launch or args.artifact_run_id):
        parser.error("--adopt-entry cannot be combined with --build, --launch, or --artifact-run-id")
    if args.template_commit and not args.adopt_entry:
        parser.error("--template-commit is only valid with --adopt-entry")
    if args.existing_linear_batch_issue and not args.execute:
        parser.error("--existing-linear-batch-issue requires --execute")
    if args.existing_linear_batch_issue and not UUID_PATTERN.fullmatch(args.existing_linear_batch_issue):
        parser.error("--existing-linear-batch-issue must be a Linear issue UUID")
    source_repo = args.warden_repo.resolve()
    branch, current_commit = git_identity(source_repo)
    commit = args.template_commit or current_commit
    if not re.fullmatch(r"[0-9a-f]{40}", commit, re.I):
        parser.error("--template-commit must be a full 40-character git commit")
    template = args.template or generated_template(args.job, branch, commit)
    worker_id = args.worker_id or f"e2b:{args.job}-{short_slug(branch)}-{commit[:8]}"
    batch_run_id = args.batch_run_id or str(uuid4())
    if not UUID_PATTERN.fullmatch(batch_run_id):
        parser.error("--batch-run-id must be a UUID")

    artifact_state: dict[str, Any] = {}
    if args.artifact_run_id:
        artifact_snapshot = fetch_latest_snapshot(args.artifact_run_id)
        artifact_state = artifact_snapshot.state

    batch_started_at = datetime.now(timezone.utc).isoformat()
    print(json.dumps({
        "batch_started_at": batch_started_at,
        "batch_run_id": batch_run_id,
        "branch": branch,
        "commit": commit,
        "template": template,
        "worker_id": worker_id,
        "job": args.job,
        "from_step": args.from_step,
        "snapshot_sources": args.run_id,
        "fresh_posts": args.fresh_post,
        "artifact_source": args.artifact_run_id,
        "execute": args.execute,
        "launch": args.launch,
    }, sort_keys=True))

    adopted_entries = [parse_adopt_entry(value) for value in args.adopt_entry]
    if adopted_entries:
        if not args.execute:
            print(json.dumps({
                "adopted_entries": [entry.__dict__ for entry in adopted_entries],
                "count": len(adopted_entries),
            }, sort_keys=True))
            return 0
        batch_ticket = create_batch_ticket(
            batch_run_id=batch_run_id,
            project_name=args.linear_project,
            branch=branch,
            commit=commit,
            template=template,
            worker_id=worker_id,
            job=args.job,
            from_step=args.from_step,
            snapshot_sources=[],
            artifact_source=None,
            entries=adopted_entries,
            started_at=batch_started_at,
        )
        mark_superseded_issues_duplicate(batch_ticket["identifier"], adopted_entries)
        print(json.dumps({
            "batch_run_id": batch_run_id,
            "linear_batch_ticket": batch_ticket,
            "adopted_task_ids": [entry.task_id for entry in adopted_entries],
            "duplicate_issue_identifiers": [entry.superseded_issue_identifier for entry in adopted_entries],
            "count": len(adopted_entries),
        }, sort_keys=True))
        return 0

    if args.build:
        build_template(template, source_repo, no_cache=args.no_cache)

    batch_ticket: dict[str, str] | None = None
    if args.execute:
        batch_ticket = (
            get_linear_issue(args.existing_linear_batch_issue)
            if args.existing_linear_batch_issue
            else create_batch_ticket(
                batch_run_id=batch_run_id,
                project_name=args.linear_project,
                branch=branch,
                commit=commit,
                template=template,
                worker_id=worker_id,
                job=args.job,
                from_step=args.from_step,
                snapshot_sources=args.run_id,
                artifact_source=args.artifact_run_id,
                entries=[],
                started_at=batch_started_at,
            )
        )
        print(json.dumps({"linear_batch_ticket": batch_ticket}, sort_keys=True))

    task_ids: list[str] = []
    created_entries: list[BatchEntry] = []
    for post_id in args.fresh_post:
        task_id = create_fresh_task(
            post_id,
            args.job,
            worker_id,
            source_repo,
            batch_run_id=batch_run_id,
            batch_ticket=batch_ticket,
            execute=args.execute,
        )
        if task_id:
            task_ids.append(task_id)
            created_entries.append(BatchEntry(task_id=task_id, post_id=post_id))
    for source_id in args.run_id:
        source_snapshot = fetch_latest_snapshot(source_id)
        overrides = artifact_overrides(source_snapshot, artifact_state)
        task_id = create_resume_task(
            source_id,
            args.job,
            args.from_step,
            worker_id,
            source_repo,
            overrides,
            batch_run_id=batch_run_id,
            batch_ticket=batch_ticket,
            execute=args.execute,
        )
        if task_id:
            task_ids.append(task_id)
            created_entries.append(BatchEntry(
                task_id=task_id,
                post_id=source_snapshot.identity[1] or source_id,
            ))
        else:
            print(json.dumps({"source_task_id": source_id, "artifact_override_keys": sorted(overrides)}))

    if batch_ticket:
        update_batch_ticket(batch_ticket["id"], created_entries, args.linear_project)

    if args.launch:
        require_ydc_api_key(args.warden_repo)
        launch_tasks(task_ids, template, worker_id, args.warden_repo)
    print(json.dumps({
        "batch_run_id": batch_run_id,
        "created_task_ids": task_ids,
        "count": len(task_ids),
        "linear_batch_ticket": batch_ticket,
    }, sort_keys=True))
    return 0


def git_identity(repo: Path) -> tuple[str, str]:
    branch = run(["git", "branch", "--show-current"], cwd=repo).strip() or "detached"
    commit = run(["git", "rev-parse", "HEAD"], cwd=repo).strip()
    return branch, commit


def generated_template(job: str, branch: str, commit: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    return f"{stamp}-{short_slug(job)}-{short_slug(branch)}-{commit[:8]}"


def short_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "detached"


def build_template(name: str, repo: Path, *, no_cache: bool) -> None:
    env = os.environ.copy()
    env["E2B_TEMPLATE"] = name
    env["WARDEN_REPO_PATH"] = str(repo)
    command = ["make", "build-e2b-template"]
    if no_cache:
        command.append("NO_CACHE=1")
    subprocess.run(command, cwd=REPO_ROOT, env=env, check=True)


def fetch_latest_snapshot(task_id: str) -> Snapshot:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")
    endpoint = f"{url}/rest/v1/warden_workflow_snapshots"
    params = {"task_id": f"eq.{quote(task_id, safe='')}", "order": "step_index.desc", "limit": "1"}
    response = httpx.get(endpoint, params=params, headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=30)
    response.raise_for_status()
    rows = response.json()
    if not rows:
        raise SystemExit(f"No workflow snapshot found for run {task_id}")
    return Snapshot(task_id=task_id, row=rows[0])


def artifact_overrides(source: Snapshot, artifact_state: dict[str, Any]) -> dict[str, Any]:
    if not artifact_state:
        return {}
    source_identity = source.identity
    artifact_identity = (
        str(artifact_state.get("client_slug") or ""),
        str(artifact_state.get("public_slug") or artifact_state.get("topic_slug") or ""),
    )
    if source_identity != artifact_identity:
        raise SystemExit(
            "Artifact source identity mismatch: "
            f"snapshot={source_identity!r}, artifact={artifact_identity!r}"
        )
    return {key: value for key, value in artifact_state.items() if key in ARTIFACT_STATE_KEYS}


def create_resume_task(
    source_id: str,
    job: str,
    from_step: str,
    worker_id: str,
    warden_repo: Path,
    overrides: dict[str, Any],
    batch_run_id: str,
    batch_ticket: dict[str, str] | None,
    *,
    execute: bool,
) -> str | None:
    command = [
        "npm", "run", "-s", "warden", "--", "run",
        "--job", job, "--resume", source_id, "--from-step", from_step,
        "--target-worker", worker_id,
    ]
    for key, value in overrides.items():
        command.extend(["--set", f"{key}={json.dumps(value, separators=(',', ':'))}"])
    # ``--set`` uses a lightweight scalar/JSON coercer. UUIDs must be passed
    # as bare scalars; JSON-encoding the string leaves literal quote characters
    # in the value and Warden rejects it as an invalid UUID.
    command.extend(["--set", f"batch_run_id={batch_run_id}"])
    if batch_ticket:
        command.extend(["--set", f"linear_batch_issue={json.dumps(batch_ticket, separators=(',', ':'))}"])
    print(json.dumps({"command": command[:14], "source_task_id": source_id, "artifact_override_keys": sorted(overrides)}))
    if not execute:
        return None
    env = os.environ.copy()
    env.setdefault("WARDEN_CLIENT_RUNTIME_ROOT", str(warden_repo.parent / "project-delivery" / "runtime-config"))
    result = subprocess.run(command, cwd=warden_repo, env=env, check=True, text=True, capture_output=True)
    print(result.stdout, end="")
    match = re.search(r"→ task ([0-9a-f-]{36}) ·", result.stdout)
    if not match:
        raise RuntimeError(f"Could not parse created task ID for {source_id}")
    return match.group(1)


def create_fresh_task(
    post_id: str,
    job: str,
    worker_id: str,
    warden_repo: Path,
    *,
    batch_run_id: str,
    batch_ticket: dict[str, str] | None,
    execute: bool,
) -> str | None:
    command = [
        "npm", "run", "-s", "warden", "--", "run",
        "--job", job, "--planned-post", post_id,
        "--target-worker", worker_id,
        "--set", f"batch_run_id={batch_run_id}",
    ]
    if batch_ticket:
        command.extend(["--set", f"linear_batch_issue={json.dumps(batch_ticket, separators=(',', ':'))}"])
    print(json.dumps({"command": command, "fresh_post": post_id}))
    if not execute:
        return None
    env = os.environ.copy()
    env.setdefault("WARDEN_CLIENT_RUNTIME_ROOT", str(warden_repo.parent / "project-delivery" / "runtime-config"))
    result = subprocess.run(command, cwd=warden_repo, env=env, check=True, text=True, capture_output=True)
    print(result.stdout, end="")
    match = re.search(r"→ task ([0-9a-f-]{36}) ·", result.stdout)
    if not match:
        raise RuntimeError(f"Could not parse created task ID for fresh post {post_id}")
    return match.group(1)


def launch_tasks(task_ids: list[str], template: str, worker_id: str, warden_repo: Path) -> None:
    """Start infra's parallel controller for the targeted batch worker.

    The controller claims only tasks targeted to ``worker_id`` and manages up
    to ``WARDEN_MAX_CONCURRENT_TASKS`` E2B sandboxes. It is intentionally
    detached because the controller is a continuous queue process.
    """
    if not task_ids:
        return
    env = build_controller_env(
        os.environ,
        REPO_ROOT / ".env",
        warden_repo / ".env",
    )
    env["E2B_TEMPLATE"] = template
    env["WARDEN_WORKER_ID"] = worker_id
    env["WARDEN_SANDBOX_RUNTIME"] = "e2b"
    env["WARDEN_WORKER_CWD"] = "/workspace/warden"
    env["WARDEN_WORKER_COMMAND"] = 'npm run warden -- worker-task --task-id "$WARDEN_TASK_ID"'
    env.setdefault("WARDEN_MAX_CONCURRENT_TASKS", "20")
    command = [sys.executable, "-m", "warden_sandbox_infra", "run"]
    controller = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    print(json.dumps({
        "parallel_controller": command,
        "parallel_controller_pid": controller.pid,
        "worker_id": worker_id,
        "task_count": len(task_ids),
        "max_concurrent_tasks": env["WARDEN_MAX_CONCURRENT_TASKS"],
    }, sort_keys=True))


def require_ydc_api_key(warden_repo: Path) -> None:
    """Fail before launch unless the fixed launcher can inject YDC_API_KEY."""
    if os.environ.get("YDC_API_KEY"):
        return
    env_path = warden_repo / ".env"
    try:
        for line in env_path.read_text().splitlines():
            if line.strip().startswith("YDC_API_KEY=") and line.split("=", 1)[1].strip().strip("'\""):
                return
    except FileNotFoundError:
        pass
    raise SystemExit(
        "YDC_API_KEY is required for --launch; set it in the environment or Warden .env "
        "(the fixed launcher injects it without putting it in the E2B template)"
    )


def create_batch_ticket(
    *,
    batch_run_id: str,
    project_name: str,
    branch: str,
    commit: str,
    template: str,
    worker_id: str,
    job: str,
    from_step: str,
    snapshot_sources: list[str],
    artifact_source: str | None,
    entries: list[BatchEntry],
    started_at: str,
) -> dict[str, str]:
    project_data = linear_graphql(
        "query($name:String!){projects(first:100,filter:{name:{eq:$name}}){nodes{id name teams{nodes{id}}}}}",
        {"name": project_name},
    )
    projects = project_data["projects"]["nodes"]
    if len(projects) != 1:
        raise SystemExit(f"Expected exactly one Linear project named {project_name!r}; found {len(projects)}")
    project = projects[0]
    teams = project["teams"]["nodes"]
    if not teams:
        raise SystemExit(f"Linear project {project_name!r} has no team")

    title = f"[{batch_run_id[:8]}] E2B batch — {job} from {from_step} — {branch} @ {commit[:8]}"
    source_lines = "\n".join(f"- `{task_id}`" for task_id in snapshot_sources) or "- (adopted existing tasks)"
    description = (
        "## Batch run\n"
        f"- Run ID: `{batch_run_id}`\n"
        f"- Started at: `{started_at}`\n"
        f"- Branch: `{branch}`\n"
        f"- Commit: `{commit}`\n"
        f"- Template: `{template}`\n"
        f"- Worker: `{worker_id}`\n"
        f"- Job: `{job}`\n"
        f"- Resume boundary: `{from_step}`\n"
        f"- Artifact source task: `{artifact_source or 'same as snapshot source'}`\n\n"
        "## Snapshot source task IDs\n"
        f"{source_lines}\n\n"
        "## Posts and task IDs\n"
        f"{format_batch_entries(entries)}\n\n"
        "This is the single Linear ticket for the batch. Each post keeps its own "
        "Warden task ID; Warden does not create per-post Linear tickets."
    )
    created = linear_graphql(
        "mutation($input:IssueCreateInput!){issueCreate(input:$input){success issue{id identifier url title}}}",
        {
            "input": {
                "id": batch_run_id,
                "teamId": teams[0]["id"],
                "projectId": project["id"],
                "title": title,
                "description": description,
            }
        },
    )["issueCreate"]
    if not created["success"]:
        raise RuntimeError("Linear batch ticket creation failed")
    return created["issue"]


def get_linear_issue(issue_id: str) -> dict[str, str]:
    data = linear_graphql(
        "query($id:String!){issue(id:$id){id identifier url title}}",
        {"id": issue_id},
    )
    issue = data.get("issue")
    if not isinstance(issue, dict) or not all(isinstance(issue.get(key), str) and issue[key] for key in ("id", "identifier", "url", "title")):
        raise RuntimeError(f"Linear issue {issue_id} was not found or is incomplete")
    return {key: str(issue[key]) for key in ("id", "identifier", "url", "title")}


def update_batch_ticket(issue_id: str, entries: list[BatchEntry], project_name: str) -> None:
    """Append the created post/run mapping to the pre-created umbrella issue."""
    current = linear_graphql(
        "query($id:String!){issue(id:$id){description}}",
        {"id": issue_id},
    )
    description = str(current.get("issue", {}).get("description") or "")
    description += (
        f"\n\n## Created posts and run IDs for {project_name}\n"
        f"{format_batch_entries(entries)}"
    )
    updated = linear_graphql(
        "mutation($id:String!,$description:String!){issueUpdate(id:$id,input:{description:$description}){success}}",
        {"id": issue_id, "description": description},
    )
    if not updated.get("issueUpdate", {}).get("success"):
        raise RuntimeError("Linear batch ticket update failed")


def mark_superseded_issues_duplicate(canonical_issue_identifier: str, entries: list[BatchEntry]) -> None:
    """Mark accidental per-post issues as duplicates of the batch issue."""
    for entry in entries:
        if not entry.superseded_issue_identifier:
            continue
        result = linear_graphql(
            "mutation($input:IssueRelationCreateInput!){issueRelationCreate(input:$input){success issueRelation{id type}}}",
            {
                "input": {
                    "issueId": entry.superseded_issue_identifier,
                    "relatedIssueId": canonical_issue_identifier,
                    "type": "duplicate",
                }
            },
        )
        if not result.get("issueRelationCreate", {}).get("success"):
            raise RuntimeError(f"Could not mark {entry.superseded_issue_identifier} duplicate")


def format_batch_entries(entries: list[BatchEntry]) -> str:
    if not entries:
        return "- (created run IDs are appended after queueing)"
    rows = [
        "| Post | Task ID | Superseded ticket |",
        "| --- | --- | --- |",
    ]
    rows.extend(
        f"| {entry.post_id} | `{entry.task_id}` | {entry.superseded_issue_identifier or '—'} |"
        for entry in entries
    )
    return "\n".join(rows)


def linear_graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    api_key = os.environ.get("LINEAR_API_KEY", "")
    if not api_key:
        raise SystemExit("LINEAR_API_KEY is required for batch ticket operations")
    response = httpx.post(
        "https://api.linear.app/graphql",
        json={"query": query, "variables": variables},
        headers={"content-type": "application/json", "authorization": api_key},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"]))
    if "data" not in payload:
        raise RuntimeError("Linear GraphQL response is missing data")
    return payload["data"]


def run(command: list[str], *, cwd: Path) -> str:
    return subprocess.run(command, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE).stdout


if __name__ == "__main__":
    sys.exit(main())
