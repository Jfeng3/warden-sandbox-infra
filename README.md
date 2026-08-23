# warden-sandbox-infra

Runs Warden tasks inside isolated E2B sandboxes.

## Project Goal

Claim a Warden task from Supabase, run it in an isolated E2B sandbox, keep its
lease alive, and report success or failure. Warden business logic stays in the
Warden app repo.

## Definition of Done

- Warden business logic stays out of this repo.
- Claiming, leases, timeouts, and failures are deterministic and testable.
- Secrets are read from environment/config, never hardcoded.
- Infra changes include focused tests once the test harness exists.
- `make check` passes.
- Docs are updated when repo contracts or runtime behavior change.

## Commands

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
make check
```

## Make The E2B Sandbox Work

Build a client-neutral template from Warden's tracked working-tree files. This includes edits
to tracked source files for pre-commit testing, but cannot include `.env`,
`.git`, ignored credentials, generated artifacts, unrelated untracked files,
or anything under `client-delivery/` or `landing/public/clients/`:

```bash
E2B_API_KEY=<key> \
E2B_TEMPLATE=warden:dev \
make build-e2b-template
```

The build defaults to `../warden`, Node 22, 2 CPUs, and 4096 MB. Override the
source with `WARDEN_REPO_PATH`, or invoke the script directly for CPU, memory,
and cache flags.

The resulting template contains a built Warden checkout at
`/workspace/warden`. Before touching the real queue, validate that contract:

```bash
E2B_API_KEY=<key> \
E2B_TEMPLATE=<template-name-or-id> \
make smoke-e2b
```

The smoke command creates a disposable sandbox, checks Node, npm, and
`/workspace/warden/package.json`, prints the sandbox ID and versions, then kills
the sandbox. Override `WARDEN_E2B_SMOKE_COMMAND` if a template uses a different
layout.

### Build the buying-guide pipeline template

The buying-guide target is a convenience name for the same client-neutral
build. It never accepts or packages a client config, roadmap, or private source:

```bash
E2B_API_KEY=<key> make build-buying-guide-e2b-template
```

The target defaults to `2026-08-18-buying-guide-no-posthog`. Override
`BUYING_GUIDE_E2B_TEMPLATE` when creating a later dated code image.

After a task is claimed, the controller reads only
`metadata.sandbox_inputs`. It validates the selected client directory beneath
`WARDEN_CLIENT_RUNTIME_ROOT`, uploads that one directory, and uploads only the
selected local private source beneath `WARDEN_PRIVATE_SOURCE_ROOTS`. Files are
mode `600`; symlinks and paths outside the allowlists fail before E2B starts.
The client package is mapped beneath
`$WARDEN_WORKER_CWD/.warden-inputs/clients/<client>` and the selected private
file beneath `$WARDEN_WORKER_CWD/.warden-inputs/private/<client>/`; the worker
receives those sandbox paths, never the Mac host runtime root.
The defaults are sibling `project-delivery/runtime-config`,
`project-delivery/private-inputs`, and `domain_niche`. Separate multiple private
roots with the host path separator.

## Claimed Task Contract

The controller claims a task before starting E2B. Therefore the command inside
the sandbox must execute exactly `WARDEN_TASK_ID` as the existing
`WARDEN_WORKER_ID`; it must not start another general queue poller.

Warden exposes `worker-task --task-id`, which validates that the task is
`running` and owned by `WARDEN_WORKER_ID`, then directly executes it without
polling. For targeted/manual work, use the fixed launcher:

```bash
make run-task TASK_ID=<task-id>
```

The launcher reads controller settings from this repo's `.env`, reads only the
reviewed Warden runtime variables from `../warden/.env`, and sets the canonical
`WARDEN_SANDBOX_ENV` list. It validates Supabase, E2B, and the selected
LLM provider before paying to create a sandbox. It also replaces any inherited
`WARDEN_WORKER_COMMAND` with the canonical command, preventing nested shell
quotes from reaching E2B. It never prints secret values.

The equivalent low-level controller invocation is:

```bash
WARDEN_SANDBOX_RUNTIME=e2b \
E2B_TEMPLATE=<template> \
WARDEN_WORKER_CWD=/workspace/warden \
WARDEN_WORKER_COMMAND='npm run warden -- worker-task --task-id "$WARDEN_TASK_ID"' \
python3 -m warden_sandbox_infra run-task --task-id <task-id>
```

Use `run-task` for targeted/manual jobs so older unrelated queue entries cannot
be claimed accidentally. `run-once` polls and claims the oldest available task.
Continuous `run` and `run-once` only poll tasks whose
`metadata.target_worker_id` matches `WARDEN_WORKER_ID`, keeping Mac-targeted and
E2B-targeted work isolated.

One continuous controller can manage multiple task sandboxes concurrently.
`WARDEN_MAX_CONCURRENT_TASKS` sets the per-controller limit and defaults to 20;
each active task keeps its own lease and E2B sandbox lifecycle.

Forward only credentials the selected Warden provider and workflow need. For
example, an OpenRouter-backed worker may use:

```bash
WARDEN_SANDBOX_ENV=OPENROUTER_API_KEY,DEFAULT_PROVIDER,DEFAULT_MODEL
```

`SUPABASE_URL`, `SUPABASE_ANON_KEY`, and `SUPABASE_SERVICE_ROLE_KEY` are always
forwarded to the task sandbox. `WARDEN_SANDBOX_ENV` adds optional runtime names;
it cannot remove those core credentials. The service-role key is required for
the private `workflow-artifacts` bucket and must never be exposed to browser code.
Direct low-level invocations must maintain this list themselves; use
`make run-task` for the reviewed full Warden workflow whitelist.

If a startup failure already marked a task `failed`, create a new Warden task
and pass its new ID to `make run-task`. A failed row is terminal and is not a
retry target. Do not add outer quotes around the complete worker command; the
launcher owns the exact command value.

The default `openai-codex` provider expects local Codex authentication, which is
not baked into the template. For each task, the controller reads
`WARDEN_CODEX_AUTH_PATH` (default `~/.codex/auth.json`), uploads it to the
disposable sandbox as `/home/user/.codex/auth.json` with mode `600`, and destroys
it with the sandbox. Set `WARDEN_CODEX_AUTH_PATH=` to disable this behavior and
use an API-key provider instead.

Preview publishing uses the same task-scoped pattern for Vercel. When present,
the controller auto-detects the host Vercel CLI auth file and the sibling Warden
project link at `../warden/.vercel/project.json`, validates both JSON contracts,
then uploads them as private files to the disposable sandbox. Override discovery
with `WARDEN_VERCEL_AUTH_PATH` and `WARDEN_VERCEL_PROJECT_PATH`, or set either
variable to an empty value to disable that file. Vercel credentials are never
baked into the E2B template or added to the task environment.

Use `WARDEN_SANDBOX_RUNTIME=local` for local command execution during
development.
