#!/usr/bin/env python3
"""Run one claimed Warden task in E2B with the canonical runtime environment."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WARDEN_REPO = REPO_ROOT.parent / "warden"
DEFAULT_PROJECT_DELIVERY_REPO = REPO_ROOT.parent / "project-delivery"
CANONICAL_WORKER_COMMAND = 'npm run warden -- worker-task --task-id "$WARDEN_TASK_ID"'

# Keep this list explicit: these values cross the host/sandbox trust boundary.
WARDEN_RUNTIME_ENV = (
    "WARDEN_ARTIFACT_STORAGE_ENABLED",
    "WARDEN_USER_EMAIL",
    "DEFAULT_PROVIDER",
    "DEFAULT_MODEL",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "WP_SSH",
    "TWITTER_CONSUMER_KEY",
    "TWITTER_CONSUMER_SECRET",
    "TWITTER_ACCESS_TOKEN",
    "TWITTER_ACCESS_TOKEN_SECRET",
    "AHREF_API_KEY",
    "DATAFORSEO_LOGIN",
    "DATAFORSEO_PASSWORD",
    "GOOGLE_SERVICE_ACCOUNT_KEY",
    "YDC_API_KEY",
    "SUBSTACK_SID",
    "SUBSTACK_URL",
    "BRIGHTDATA_API_KEY",
    "BRIGHTDATA_TRUSTPILOT_DATASET_ID",
    "BMI_OC_STAGING_BASE_URL",
    "BMI_OC_BASIC_USERNAME",
    "BMI_OC_BASIC_PASSWORD",
    "BMI_OC_USERNAME",
    "BMI_OC_PASSWORD",
    "BMI_OC_COOKIE_FILE",
    "BMI_OC_USER_TOKEN",
)

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--infra-env", type=Path, default=REPO_ROOT / ".env")
    parser.add_argument("--warden-env", type=Path, default=DEFAULT_WARDEN_REPO / ".env")
    args = parser.parse_args()

    env = build_controller_env(os.environ, args.infra_env, args.warden_env)
    _validate(env)

    python = REPO_ROOT / ".venv" / "bin" / "python"
    executable = str(python if python.is_file() else Path(os.sys.executable))
    argv = [
        executable,
        "-m",
        "warden_sandbox_infra",
        "run-task",
        "--task-id",
        args.task_id,
    ]
    os.execve(executable, argv, env)


def build_controller_env(
    source_env: os._Environ[str] | dict[str, str],
    infra_env_path: Path,
    warden_env_path: Path,
) -> dict[str, str]:
    env = dict(source_env)
    _fill_missing(env, _read_env_file(infra_env_path))

    warden_env = _read_env_file(warden_env_path)
    _fill_missing(env, {name: warden_env[name] for name in WARDEN_RUNTIME_ENV if name in warden_env})
    # The Warden Supabase .env owns the service role used for private artifacts.
    _fill_missing(
        env,
        {
            name: warden_env[name]
            for name in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY")
            if name in warden_env
        },
    )

    env["WARDEN_SANDBOX_RUNTIME"] = "e2b"
    env.setdefault("WARDEN_WORKER_CWD", "/workspace/warden")
    # Do not inherit this value: extra quote layers in ad hoc launchers caused
    # `/bin/bash: unexpected EOF` before Warden could execute step 0.
    env["WARDEN_WORKER_COMMAND"] = CANONICAL_WORKER_COMMAND
    env["WARDEN_SANDBOX_ENV"] = ",".join(WARDEN_RUNTIME_ENV)
    # These roots define the host/runtime boundary and must not inherit stale
    # values from an interactive shell or an unrelated worktree.
    env["WARDEN_CLIENT_RUNTIME_ROOT"] = str(DEFAULT_PROJECT_DELIVERY_REPO / "runtime-config")
    env["WARDEN_PRIVATE_SOURCE_ROOTS"] = os.pathsep.join(
        (
            str(DEFAULT_PROJECT_DELIVERY_REPO / "private-inputs"),
            str(REPO_ROOT.parent / "domain-niche"),
        )
    )
    return env


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError as exc:
        raise SystemExit(f"environment file not found: {path}") from exc

    values: dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if "=" not in stripped:
            raise SystemExit(f"invalid environment line {path}:{line_number}")
        name, raw_value = stripped.split("=", 1)
        name = name.strip()
        if not _ENV_NAME.fullmatch(name):
            raise SystemExit(f"invalid environment name {path}:{line_number}")
        values[name] = _dotenv_value(raw_value.strip())
    return values


def _dotenv_value(raw_value: str) -> str:
    if len(raw_value) >= 2 and raw_value[0] in {"'", '"'} and raw_value[-1] == raw_value[0]:
        return raw_value[1:-1]
    return re.split(r"\s+#", raw_value, maxsplit=1)[0].rstrip()


def _fill_missing(destination: dict[str, str], source: dict[str, str]) -> None:
    for name, value in source.items():
        if not destination.get(name) and value:
            destination[name] = value


def _validate(env: dict[str, str]) -> None:
    required = ["E2B_API_KEY", "E2B_TEMPLATE", "SUPABASE_URL"]
    if not (env.get("SUPABASE_SERVICE_ROLE_KEY") or env.get("SUPABASE_ANON_KEY")):
        required.append("SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY")

    provider = env.get("DEFAULT_PROVIDER", "openai-codex")
    provider_key = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }.get(provider)
    if provider_key:
        required.append(provider_key)

    missing = [name for name in required if " or " in name or not env.get(name)]
    if missing:
        raise SystemExit(f"missing required environment before sandbox start: {', '.join(missing)}")


if __name__ == "__main__":
    main()
