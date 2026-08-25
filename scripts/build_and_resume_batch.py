#!/usr/bin/env python3
"""Build a branch-scoped E2B template and resume Warden runs from snapshots.

The command is dry-run by default. ``--execute`` creates fresh Warden tasks;
``--launch`` additionally runs those tasks through the fixed ``make run-task``
launcher. ``--artifact-run-id`` may provide durable artifact refs from another
run, but only when its client/post identity matches the snapshot source.

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

import httpx


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", action="append", required=True, help="Snapshot source run/task ID; repeatable")
    parser.add_argument("--from-step", required=True, help="Stable workflow step key, e.g. draft-step")
    parser.add_argument("--job", default="buying-guide-pipeline")
    parser.add_argument("--artifact-run-id", help="Optional run whose latest snapshot supplies artifact refs")
    parser.add_argument("--warden-repo", type=Path, default=DEFAULT_WARDEN_REPO)
    parser.add_argument("--template", help="Template name; default is generated from date/job/branch/commit")
    parser.add_argument("--worker-id", help="Exact target worker for created tasks")
    parser.add_argument("--linear-project", default="blog_e2b_run", help="Linear project for the batch ticket")
    parser.add_argument("--build", action="store_true", help="Build the generated/current template")
    parser.add_argument("--execute", action="store_true", help="Create fresh resume tasks")
    parser.add_argument("--launch", action="store_true", help="Launch created tasks with make run-task")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    if args.launch and not args.execute:
        parser.error("--launch requires --execute")
    source_repo = args.warden_repo.resolve()
    branch, commit = git_identity(source_repo)
    template = args.template or generated_template(args.job, branch, commit)
    worker_id = args.worker_id or f"e2b:{args.job}-{short_slug(branch)}-{commit[:8]}"

    artifact_state: dict[str, Any] = {}
    if args.artifact_run_id:
        artifact_snapshot = fetch_latest_snapshot(args.artifact_run_id)
        artifact_state = artifact_snapshot.state

    batch_started_at = datetime.now(timezone.utc).isoformat()
    print(json.dumps({
        "batch_started_at": batch_started_at,
        "branch": branch,
        "commit": commit,
        "template": template,
        "worker_id": worker_id,
        "job": args.job,
        "from_step": args.from_step,
        "snapshot_sources": args.run_id,
        "artifact_source": args.artifact_run_id,
        "execute": args.execute,
        "launch": args.launch,
    }, sort_keys=True))

    if args.build:
        build_template(template, source_repo, no_cache=args.no_cache)

    batch_ticket: dict[str, str] | None = None
    if args.execute:
        batch_ticket = create_batch_ticket(
            project_name=args.linear_project,
            branch=branch,
            commit=commit,
            template=template,
            worker_id=worker_id,
            job=args.job,
            from_step=args.from_step,
            snapshot_sources=args.run_id,
            artifact_source=args.artifact_run_id,
            task_ids=[],
            started_at=batch_started_at,
        )
        print(json.dumps({"linear_batch_ticket": batch_ticket}, sort_keys=True))

    task_ids: list[str] = []
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
            batch_ticket=batch_ticket,
            execute=args.execute,
        )
        if task_id:
            task_ids.append(task_id)
        else:
            print(json.dumps({"source_run_id": source_id, "artifact_override_keys": sorted(overrides)}))

    if batch_ticket:
        update_batch_ticket(batch_ticket["id"], task_ids, args.linear_project)

    if args.launch:
        require_ydc_api_key(args.warden_repo)
        launch_tasks(task_ids, template)
    print(json.dumps({"created_task_ids": task_ids, "count": len(task_ids), "linear_batch_ticket": batch_ticket}, sort_keys=True))
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
    if batch_ticket:
        command.extend(["--set", f"linear_batch_issue={json.dumps(batch_ticket, separators=(',', ':'))}"])
    print(json.dumps({"command": command[:14], "source_run_id": source_id, "artifact_override_keys": sorted(overrides)}))
    if not execute:
        return None
    env = os.environ.copy()
    env.setdefault("WARDEN_CLIENT_RUNTIME_ROOT", str(warden_repo.parent / "project-delivery" / "runtime-config"))
    result = subprocess.run(command, cwd=warden_repo, env=env, check=True, text=True, capture_output=True)
    print(result.stdout, end="")
    match = re.search(r"→ run ([0-9a-f-]{36}) ·", result.stdout)
    if not match:
        raise RuntimeError(f"Could not parse created task ID for {source_id}")
    return match.group(1)


def launch_tasks(task_ids: list[str], template: str) -> None:
    if not task_ids:
        return
    env = os.environ.copy()
    env["E2B_TEMPLATE"] = template
    for task_id in task_ids:
        subprocess.run(["make", "run-task", f"TASK_ID={task_id}"], cwd=REPO_ROOT, env=env, check=True)


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
    project_name: str,
    branch: str,
    commit: str,
    template: str,
    worker_id: str,
    job: str,
    from_step: str,
    snapshot_sources: list[str],
    artifact_source: str | None,
    task_ids: list[str],
    started_at: str,
) -> dict[str, str]:
    api_key = os.environ.get("LINEAR_API_KEY", "")
    if not api_key:
        raise SystemExit("LINEAR_API_KEY is required when --execute creates the batch ticket")
    query_url = "https://api.linear.app/graphql"

    def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = httpx.post(
            query_url,
            json={"query": query, "variables": variables},
            headers={"content-type": "application/json", "authorization": api_key},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError(json.dumps(payload["errors"]))
        return payload["data"]

    project_data = graphql(
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

    title = f"E2B batch run — {job} from {from_step} — {branch} @ {commit[:8]} — {started_at}"
    source_lines = "\n".join(f"- `{run_id}`" for run_id in snapshot_sources)
    task_lines = "\n".join(f"- `{task_id}`" for task_id in task_ids) or "- (dry-run: none created)"
    description = (
        "## Batch run\n"
        f"- Started at: `{started_at}`\n"
        f"- Branch: `{branch}`\n"
        f"- Commit: `{commit}`\n"
        f"- Template: `{template}`\n"
        f"- Worker: `{worker_id}`\n"
        f"- Job: `{job}`\n"
        f"- Resume boundary: `{from_step}`\n"
        f"- Artifact source run: `{artifact_source or 'same as snapshot source'}`\n\n"
        "## Snapshot source run IDs\n"
        f"{source_lines}\n\n"
        "## Created task IDs\n"
        f"{task_lines}\n\n"
        "This is the umbrella ticket for the batch. Individual Warden run issues "
        "may still be created by the normal run-registration contract."
    )
    created = graphql(
        "mutation($input:IssueCreateInput!){issueCreate(input:$input){success issue{identifier url title}}}",
        {
            "input": {
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


def update_batch_ticket(issue_id: str, task_ids: list[str], project_name: str) -> None:
    """Append created task IDs to the pre-created umbrella issue."""
    api_key = os.environ.get("LINEAR_API_KEY", "")
    if not api_key:
        raise SystemExit("LINEAR_API_KEY is required to update the batch ticket")
    task_lines = "\n".join(f"- `{task_id}`" for task_id in task_ids) or "- (no tasks created)"
    lookup = httpx.post(
        "https://api.linear.app/graphql",
        json={
            "query": "query($id:String!){issue(id:$id){description}}",
            "variables": {"id": issue_id},
        },
        headers={"content-type": "application/json", "authorization": api_key},
        timeout=30,
    )
    lookup.raise_for_status()
    current = lookup.json()
    if current.get("errors"):
        raise RuntimeError(json.dumps(current["errors"]))
    description = str(current.get("data", {}).get("issue", {}).get("description") or "")
    description += f"\n\n## Created task IDs\nCreated task IDs for {project_name}:\n{task_lines}"
    response = httpx.post(
        "https://api.linear.app/graphql",
        json={
            "query": "mutation($id:String!,$description:String!){issueUpdate(id:$id,input:{description:$description}){success}}",
            "variables": {"id": issue_id, "description": description},
        },
        headers={"content-type": "application/json", "authorization": api_key},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors") or not payload.get("data", {}).get("issueUpdate", {}).get("success"):
        raise RuntimeError(json.dumps(payload.get("errors") or payload))


def run(command: list[str], *, cwd: Path) -> str:
    return subprocess.run(command, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE).stdout


if __name__ == "__main__":
    sys.exit(main())
