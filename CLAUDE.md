# warden-sandbox-infra

This repo is the infrastructure layer for running Warden workflows in E2B
sandboxes.

The Warden app repo is the source of truth for product logic and workflow
contracts:

```text
relative: ../warden
absolute: /Users/jie/Codes/warden
opened parent: /Users/jie/Codes/warden-org
```

`/Users/jie/Codes/warden-org/warden` is a symlink to the real Warden repo at
`/Users/jie/Codes/warden`.

## Index

- `/Users/jie/Codes/warden-org/` - opened parent workspace containing the
  Warden app symlink and this infra repo.
- `AGENTS.md` and `CLAUDE.md` - mirrored repo handbooks for Codex and Claude
  Code; keep these files identical.
- `README.md` - human-facing repo summary.
- `../warden/AGENTS.md` - Warden app rules.
- `../warden/CLAUDE.md` - Warden app handbook.
- `../warden/.claude/rules/` - Warden app implementation rules.

## Instruction Files

- `AGENTS.md` and `CLAUDE.md` must stay byte-for-byte equivalent in this repo
  so Codex and Claude Code receive the same repo context.
- `../warden/AGENTS.md` and `../warden/CLAUDE.md` belong to the Warden app.
  Read them for app contracts and context; do not copy Warden business rules
  into this infra repo.

## Project Goal

Claim a Warden task from Supabase, run it in an isolated E2B sandbox, keep its
lease alive, and report success or failure. Warden business logic stays in the
Warden app repo.

## Responsibility Split

This repo owns infrastructure:

- finding claimable Warden tasks;
- claiming a task with `worker_id` and `lease_expires_at`;
- starting an E2B sandbox for the claimed task;
- passing task-scoped environment variables into the sandbox;
- renewing the task lease while sandbox work runs;
- observing sandbox completion/failure;
- keeping infrastructure tests separate from Warden business tests.

The Warden app repo owns business behavior:

- workflow steps and wrappers;
- content strategy and publishing rules;
- Supabase schema migrations;
- task status semantics;
- workflow progress and snapshots;
- artifact paths and storage semantics;
- dashboard behavior.

The integration boundary is:

```text
task_id + Supabase API contract
```

Infra should not import Warden source files at runtime. If it needs Warden
behavior, run Warden code inside the sandbox through a clearly configured worker
command.

## Warden Context To Read

For queue and lease work:

- `../warden/src/data_model/db.ts`
- `../warden/src/data_model/types.ts`
- `../warden/src/runner.ts`
- `../warden/supabase/migrations/`
- `../warden/docs/plans/2026-06-15-supabase-db-simplification-for-e2b.md`

For artifact and storage work:

- `../warden/docs/plans/2026-06-15-e2b-artifact-storage-migration.md`
- `../warden/.claude/rules/data-flow.md`
- `../warden/.claude/rules/pipeline-flow.md`

For dashboard/user-visible workflow state:

- `../warden/.claude/rules/data-flow.md`
- `../warden/landing/CLAUDE.md`
- `../warden/landing/app/lib/types.ts`

## Expected Runtime Model

```text
warden-sandbox-infra controller
  -> polls Supabase for claimable warden_tasks
  -> claims one task with worker_id + lease_expires_at
  -> starts E2B sandbox
  -> passes WARDEN_TASK_ID and WARDEN_WORKER_ID
  -> runs the configured Warden worker command
  -> renews lease until completion/failure

Warden worker command inside sandbox
  -> reads WARDEN_TASK_ID
  -> runs Warden business logic
  -> writes task status/progress/snapshots to Supabase
```

## Working With Both Repos

Start Codex from the repo you want to change:

```bash
cd /Users/jie/Codes/warden-org/infra
codex
```

When Warden context is needed, read it by relative path:

```bash
sed -n '1,220p' ../warden/AGENTS.md
sed -n '1,220p' ../warden/src/data_model/db.ts
```

Check Git state separately:

```bash
git status --short
git -C ../warden status --short
```

Do not make one commit that mixes files from both repos unless the user
explicitly asks for a cross-repo change.

When the user says "commit, push" without naming a branch, commit the current
repo changes to a `progress` branch and push `origin progress`. Do not push
directly to `main` unless the user explicitly asks. This lets the user review
the diff in GitHub Desktop before merging to `main`.

## Design Rules

- Prefer Supabase as the source of truth for task state.
- Treat E2B sandboxes as temporary execution environments.
- When creating an E2B template, name it with a date prefix followed by the job
  name: `YYYY-MM-DD-<job-name>` (for example, `2026-07-16-bmi-blog`).
- Keep worker identity explicit through `WARDEN_WORKER_ID`.
- Keep task ownership explicit through `worker_id` and `lease_expires_at`.
- Renew leases while a sandbox is doing work.
- Assume sandbox local files are temporary unless a Warden storage design says
  otherwise.
- Keep business workflow decisions in Warden, not in this infra repo.
- Do not over-engineer; prefer the simplest deterministic design that satisfies
  the current requirement and matches existing repo patterns.

## Environment

Do not commit real secrets. Expected runtime variables will include:

```text
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
E2B_API_KEY
E2B_TEMPLATE
YDC_API_KEY
WARDEN_WORKER_COMMAND
WARDEN_WORKER_ID
WARDEN_SANDBOX_RUNTIME
WARDEN_SANDBOX_ENV
WARDEN_VERCEL_AUTH_PATH
WARDEN_VERCEL_PROJECT_PATH
```

Use `.env.example` for placeholders only if this repo later adds one.

## Definition of Done

- Warden business logic stays out of this repo.
- Claiming, leases, timeouts, and failures are deterministic and testable.
- Secrets are read from environment/config, never hardcoded.
- Infra changes include focused tests once the test harness exists.
- `make check` passes.
- Docs are updated when repo contracts or runtime behavior change.

## Verification

Run:

```bash
make check
```

This compiles Python source and runs the focused async unit tests. If a change
depends on Warden's task contract, verify against the relevant Warden
types/migrations before committing.

## Running A Targeted Warden Task

Always use the fixed launcher for a real targeted E2B task:

```bash
make run-task TASK_ID=<task-id>
```

Do not assemble an ad hoc `WARDEN_SANDBOX_ENV` list or retry a task once per
missing variable. `scripts/run_warden_task.py` loads controller settings from
this repo's `.env`, loads only its reviewed Warden runtime whitelist from
`../warden/.env`, and validates required E2B, Supabase, and provider
settings before creating the sandbox. It always replaces inherited
`WARDEN_WORKER_COMMAND` with its canonical command; never wrap that complete
command in another quote layer. If a startup failure has already marked a task
`failed`, create a new Warden task and invoke the launcher with the new task ID;
do not try to reuse the terminal failed row. Update the script whitelist, tests,
and docs together when Warden adds a new sandbox runtime credential.
