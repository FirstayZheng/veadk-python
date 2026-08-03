#!/usr/bin/env python3
"""Smoke-test VeADK Studio SQL short-term memory through a VPC-attached Studio.

The script intentionally talks to Studio's backend APIs instead of invoking the
AgentKit Runtime from the local machine. This supports private-only Runtime
endpoints as long as the Studio VeFaaS deployment is attached to the same VPC.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - only for minimal environments.
    raise SystemExit(
        "PyYAML is required. Run this with the veadk project .venv."
    ) from exc


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
STUDIO_DEPLOY_URL_RE = re.compile(r"Frontend deployed:\s*(https?://\S+)")


class SmokeError(RuntimeError):
    pass


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SmokeError(f"Config file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SmokeError("Config root must be a YAML mapping.")
    return data


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def deep_get(data: dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def clean_env(env: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for key, value in (env or {}).items():
        if value is None:
            continue
        value_str = str(value)
        if value_str.strip():
            rows.append({"key": str(key), "value": value_str})
    return rows


def redact_key(key: str, value: str) -> str:
    sensitive = ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "ACCESS_KEY")
    if any(part in key.upper() for part in sensitive):
        return "***" if value else ""
    return value


def append_option(args: list[str], option: str, value: Any) -> None:
    if value is None:
        return
    value_str = str(value).strip()
    if value_str:
        args.extend([option, value_str])


def append_flag(args: list[str], option: str, enabled: Any) -> None:
    if truthy(enabled):
        args.append(option)


def redact_command(args: list[str]) -> str:
    sensitive_options = {
        "--volcengine-access-key",
        "--volcengine-secret-key",
        "--client-secret",
    }
    out: list[str] = []
    skip_next = False
    for idx, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        out.append(arg)
        if arg in sensitive_options and idx + 1 < len(args):
            out.append("***")
            skip_next = True
    return " ".join(out)


def auth_hint(status: int, detail: str) -> str:
    if status not in {401, 403} and "Not authenticated" not in detail:
        return ""
    return (
        "\n\nStudio authentication is missing or expired. Put an authenticated "
        "Cookie header in studio.auth.cookie, use studio.auth.bearer_token, or "
        "fall back to browser UI automation with a logged-in Studio page."
    )


def studio_deploy_enabled(config: dict[str, Any]) -> bool:
    return truthy(deep_get(config, "studio.deploy.enabled"))


def build_studio_deploy_command(
    config: dict[str, Any],
) -> tuple[list[str], dict[str, str]]:
    deploy = deep_get(config, "studio.deploy", {}) or {}
    if not isinstance(deploy, dict):
        raise SmokeError("studio.deploy must be a mapping.")
    python_bin = str(deploy.get("python") or sys.executable)
    args = [python_bin, "-m", "veadk.cli.cli", "studio", "deploy"]

    append_option(args, "--user-pool-id", deploy.get("user_pool_id"))
    append_option(args, "--allowed-client-id", deploy.get("allowed_client_id"))
    append_option(args, "--client-secret", deploy.get("client_secret"))
    append_option(args, "--vefaas-app-name", deploy.get("vefaas_app_name"))
    append_option(
        args, "--region", deploy.get("region") or deep_get(config, "deployment.region")
    )
    append_option(
        args,
        "--project",
        deploy.get("project") or deep_get(config, "deployment.project_name"),
    )
    append_option(args, "--iam-role", deploy.get("iam_role"))
    append_option(args, "--gateway-name", deploy.get("gateway_name"))
    append_option(args, "--gateway-service-name", deploy.get("gateway_service_name"))
    append_option(args, "--gateway-upstream-name", deploy.get("gateway_upstream_name"))
    append_option(args, "--volcengine-access-key", deploy.get("volcengine_access_key"))
    append_option(args, "--volcengine-secret-key", deploy.get("volcengine_secret_key"))
    append_option(args, "--veadk-version", deploy.get("veadk_version"))
    append_option(args, "--site-logo", deploy.get("site_logo"))
    append_option(args, "--site-title", deploy.get("site_title"))
    append_option(args, "--admin", deploy.get("admin"))
    append_option(args, "--developer", deploy.get("developer"))
    append_flag(args, "--from-source", deploy.get("from_source"))
    for item in as_list(deploy.get("extra_args")):
        if str(item).strip():
            args.append(str(item))

    env = os.environ.copy()
    for key, value in (deploy.get("env") or {}).items():
        if value is not None:
            env[str(key)] = str(value)
    return args, env


def validate_studio_deploy_config(config: dict[str, Any]) -> None:
    if not studio_deploy_enabled(config):
        return
    deploy = deep_get(config, "studio.deploy", {}) or {}
    required = ("user_pool_id", "allowed_client_id", "vefaas_app_name")
    missing = [
        f"studio.deploy.{key}"
        for key in required
        if not str(deploy.get(key) or "").strip()
    ]
    if missing:
        raise SmokeError(
            "Invalid Studio deploy config:\n- "
            + "\n- ".join(f"{key} is required." for key in missing)
        )


def run_studio_deploy(config: dict[str, Any]) -> str:
    args, env = build_studio_deploy_command(config)
    cwd = str(deep_get(config, "studio.deploy.cwd") or Path.cwd())
    timeout_raw = deep_get(config, "studio.deploy.timeout_seconds")
    timeout = float(timeout_raw) if timeout_raw else None
    print("Running Studio deploy:")
    print(redact_command(args), flush=True)
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    output: list[str] = []
    started = time.time()
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            output.append(line)
            print(line, end="", flush=True)
            if timeout and time.time() - started > timeout:
                proc.kill()
                raise SmokeError(f"Studio deploy timed out after {timeout:g}s.")
        code = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    text = "".join(output)
    if code != 0:
        raise SmokeError(f"Studio deploy failed with exit code {code}.")
    match = STUDIO_DEPLOY_URL_RE.search(text)
    if not match:
        raise SmokeError(
            "Studio deploy finished but no 'Frontend deployed:' URL was found in output."
        )
    return match.group(1).rstrip("/")


class StudioClient:
    def __init__(self, config: dict[str, Any]) -> None:
        studio = config.get("studio") or {}
        base_url = str(studio.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            raise SmokeError("studio.base_url is required.")
        self.base_url = base_url
        self.timeout = float(studio.get("timeout_seconds") or 900)
        self.headers = self._build_headers(studio.get("auth") or {})

    def _build_headers(self, auth: dict[str, Any]) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        extra = auth.get("headers") or {}
        if isinstance(extra, dict):
            headers.update({str(k): str(v) for k, v in extra.items() if v is not None})
        cookie = str(auth.get("cookie") or "").strip()
        if cookie:
            headers["Cookie"] = cookie
        bearer = str(auth.get("bearer_token") or "").strip()
        if bearer:
            headers["Authorization"] = (
                bearer if bearer.lower().startswith("bearer ") else f"Bearer {bearer}"
            )
        local_user = str(auth.get("local_user") or "").strip()
        if local_user:
            headers["X-VeADK-Local-User"] = local_user
        return headers

    def request(
        self,
        method: str,
        path: str,
        body: Any | None = None,
        *,
        accept_sse: bool = False,
        timeout: float | None = None,
    ) -> HttpResponse:
        url = self.base_url + path
        data = None
        headers = dict(self.headers)
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if accept_sse:
            headers["Accept"] = "text/event-stream"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return HttpResponse(
                    status=int(resp.status),
                    headers={k.lower(): v for k, v in resp.headers.items()},
                    body=resp.read(),
                )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise SmokeError(
                f"{method} {path} failed: HTTP {exc.code}\n{detail}{auth_hint(exc.code, detail)}"
            ) from exc
        except urllib.error.URLError as exc:
            raise SmokeError(f"{method} {path} failed: {exc}") from exc

    def stream_sse(
        self,
        method: str,
        path: str,
        body: Any,
        *,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        url = self.base_url + path
        headers = dict(self.headers)
        headers["Accept"] = "text/event-stream"
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        events: list[dict[str, Any]] = []
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                pending: list[str] = []
                for raw in resp:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line:
                        event = parse_sse_data(pending)
                        pending = []
                        if event is not None:
                            events.append(event)
                            yield_event(event)
                        continue
                    if line.startswith("data:"):
                        pending.append(line[5:].lstrip())
                event = parse_sse_data(pending)
                if event is not None:
                    events.append(event)
                    yield_event(event)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise SmokeError(
                f"{method} {path} failed: HTTP {exc.code}\n{detail}{auth_hint(exc.code, detail)}"
            ) from exc
        except urllib.error.URLError as exc:
            raise SmokeError(f"{method} {path} failed: {exc}") from exc
        return events


def parse_sse_data(lines: list[str]) -> dict[str, Any] | None:
    if not lines:
        return None
    payload = "\n".join(lines)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return {"raw": payload}
    return data if isinstance(data, dict) else {"data": data}


def yield_event(event: dict[str, Any]) -> None:
    if event.get("done"):
        return
    message = event.get("message")
    if message:
        phase = event.get("phase") or "deploy"
        print(f"[{phase}] {message}", flush=True)


def build_draft(config: dict[str, Any], case_name: str) -> dict[str, Any]:
    agent_cfg = config.get("agent") or {}
    case_cfg = (config.get("cases") or {}).get(case_name) or {}
    backend = str(case_cfg.get("backend") or case_name)
    name_prefix = str(agent_cfg.get("name_prefix") or "studio_sql_smoke").strip()
    safe_case = case_name.replace("_", "-")
    display_name = str(
        case_cfg.get("agent_name") or f"{name_prefix}-{safe_case}-{int(time.time())}"
    )
    py_name = display_name.replace("-", "_")
    return {
        "name": display_name,
        "description": str(
            agent_cfg.get("description")
            or f"Smoke test agent for {backend} short-term memory."
        ),
        "instruction": str(
            agent_cfg.get("instruction")
            or "You are a concise smoke-test assistant. Reply with a short acknowledgement."
        ),
        "agentType": "llm",
        "maxIterations": 3,
        "a2aUrl": "",
        "model": "",
        "modelName": str(agent_cfg.get("model_name") or "doubao-seed-2-1-pro-260628"),
        "modelProvider": str(agent_cfg.get("model_provider") or ""),
        "modelApiBase": str(agent_cfg.get("model_api_base") or ""),
        "tools": [],
        "skills": [],
        "memory": {"shortTerm": True, "longTerm": False},
        "knowledgebase": False,
        "tracing": False,
        "subAgents": [],
        "builtinTools": [str(item) for item in as_list(agent_cfg.get("builtin_tools"))],
        "customTools": [],
        "mcpTools": [],
        "shortTermBackend": backend,
        "longTermBackend": "local",
        "autoSaveSession": False,
        "knowledgebaseBackend": "local",
        "tracingExporters": [],
        "selectedSkills": [],
        "deployment": {"feishuEnabled": False},
        "_expectedPythonName": py_name,
    }


def runtime_network(config: dict[str, Any]) -> dict[str, Any] | None:
    network = (config.get("deployment") or {}).get("network") or {}
    if not isinstance(network, dict):
        return None
    mode = str(network.get("mode") or "public").strip()
    if not mode or mode == "public":
        return None
    subnet_ids = network.get("subnet_ids") or network.get("SubnetIds")
    if isinstance(subnet_ids, str):
        subnet_ids = [item.strip() for item in subnet_ids.split(",") if item.strip()]
    result: dict[str, Any] = {"mode": mode}
    vpc_id = network.get("vpc_id") or network.get("VpcId")
    if vpc_id not in (None, ""):
        result["vpc_id"] = vpc_id
    if subnet_ids:
        result["subnet_ids"] = subnet_ids
    if "enable_shared_internet_access" in network and network[
        "enable_shared_internet_access"
    ] not in (None, ""):
        result["enable_shared_internet_access"] = truthy(
            network["enable_shared_internet_access"]
        )
    elif "EnableSharedInternetAccess" in network and network[
        "EnableSharedInternetAccess"
    ] not in (None, ""):
        result["enable_shared_internet_access"] = truthy(
            network["EnableSharedInternetAccess"]
        )
    return result


def generate_project(client: StudioClient, draft: dict[str, Any]) -> dict[str, Any]:
    payload = {"draft": {k: v for k, v in draft.items() if not k.startswith("_")}}
    project = client.request("POST", "/web/generated-agent-projects", payload).json()
    if not isinstance(project, dict) or not project.get("files"):
        raise SmokeError("Studio returned an invalid generated project.")
    return project


def deploy_project(
    client: StudioClient,
    config: dict[str, Any],
    case_name: str,
    project: dict[str, Any],
) -> dict[str, Any]:
    deployment = config.get("deployment") or {}
    case_cfg = (config.get("cases") or {}).get(case_name) or {}
    env = {}
    env.update(deployment.get("extra_env") or {})
    env.update(case_cfg.get("env") or {})
    payload = {
        "name": project["name"],
        "files": project["files"],
        "config": {
            "region": str(deployment.get("region") or "cn-beijing"),
            "projectName": str(deployment.get("project_name") or "default"),
            "network": runtime_network(config),
        },
        "taskId": f"smoke-{case_name}-{int(time.time())}",
        "envs": clean_env(env),
    }
    print(f"Deploying {case_name} as {project['name']}...")
    events = client.stream_sse("POST", "/web/deploy-agentkit", payload)
    final = next((event for event in reversed(events) if event.get("done")), None)
    if not final:
        raise SmokeError("Deployment stream ended without a terminal frame.")
    if not final.get("success"):
        raise SmokeError(f"Deployment failed: {final.get('error') or final}")
    if not final.get("runtimeId"):
        raise SmokeError(f"Deployment did not return runtimeId: {final}")
    return final


def proxy_path(runtime_id: str, region: str, path: str) -> str:
    quoted = urllib.parse.quote(runtime_id, safe="")
    sep = "&" if "?" in path else "?"
    return f"/web/runtime-proxy/{quoted}{path}{sep}region={urllib.parse.quote(region)}"


def invoke_runtime(
    client: StudioClient,
    config: dict[str, Any],
    runtime_id: str,
    region: str,
    preferred_app: str,
) -> dict[str, Any]:
    smoke = config.get("smoke") or {}
    apps = client.request("GET", proxy_path(runtime_id, region, "/list-apps")).json()
    if not isinstance(apps, list) or not apps:
        raise SmokeError(f"Runtime returned no apps: {apps}")
    app = preferred_app if preferred_app in apps else str(apps[0])
    user_id = str(smoke.get("user_id") or "studio_sql_smoke_user")
    session_prefix = str(smoke.get("session_id_prefix") or "studio-sql-smoke")
    session_id = f"{session_prefix}-{int(time.time())}"
    session_path = (
        f"/apps/{urllib.parse.quote(app, safe='')}/users/"
        f"{urllib.parse.quote(user_id, safe='')}/sessions"
    )
    created_session = client.request(
        "POST", proxy_path(runtime_id, region, session_path), {}
    ).json()
    if isinstance(created_session, dict) and created_session.get("id"):
        session_id = str(created_session["id"])

    messages = [str(m) for m in as_list(smoke.get("messages")) if str(m).strip()]
    if not messages:
        messages = ["Please reply with OK for this SQL memory smoke test."]
    all_events: list[dict[str, Any]] = []
    for idx, text in enumerate(messages, start=1):
        print(f"Invoking turn {idx}: {text}")
        payload = {
            "app_name": app,
            "user_id": user_id,
            "session_id": session_id,
            "new_message": {"role": "user", "parts": [{"text": text}]},
            "streaming": True,
        }
        events = client.stream_sse(
            "POST",
            proxy_path(runtime_id, region, "/run_sse"),
            payload,
            timeout=float(smoke.get("invoke_timeout_seconds") or 900),
        )
        for event in events:
            if (
                event.get("error")
                or event.get("errorMessage")
                or event.get("error_message")
            ):
                raise SmokeError(f"Runtime returned error event: {event}")
        all_events.extend(events)
    response_text = collect_text(all_events)
    if truthy(smoke.get("require_response", True)) and not response_text.strip():
        raise SmokeError("Runtime invocation produced no text response.")
    return {
        "app": app,
        "user_id": user_id,
        "session_id": session_id,
        "response_text": response_text,
        "event_count": len(all_events),
    }


def collect_text(events: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for event in events:
        content = event.get("content")
        if not isinstance(content, dict):
            continue
        for part in content.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    return "".join(chunks)


def maybe_verify_db(
    config: dict[str, Any], case_name: str, session_id: str
) -> dict[str, Any] | None:
    case_cfg = (config.get("cases") or {}).get(case_name) or {}
    verify = case_cfg.get("db_verify") or {}
    if not truthy(verify.get("enabled")):
        return None
    try:
        from sqlalchemy import create_engine, text
    except ImportError as exc:
        raise SmokeError("sqlalchemy is required for db_verify.enabled=true") from exc

    backend = str(case_cfg.get("backend") or case_name)
    env = case_cfg.get("env") or {}
    if backend == "postgresql":
        driver = str(verify.get("driver") or "postgresql+psycopg2")
        user = urllib.parse.quote_plus(str(env.get("DATABASE_POSTGRESQL_USER") or ""))
        password = urllib.parse.quote_plus(
            str(env.get("DATABASE_POSTGRESQL_PASSWORD") or "")
        )
        host = str(env.get("DATABASE_POSTGRESQL_HOST") or "")
        port = str(env.get("DATABASE_POSTGRESQL_PORT") or "5432")
        database = str(env.get("DATABASE_POSTGRESQL_DATABASE") or "")
        db_url = f"{driver}://{user}:{password}@{host}:{port}/{database}"
        schema = str(
            env.get("DATABASE_POSTGRESQL_SCHEMA") or verify.get("schema") or "public"
        )
        table_query = text(
            "select table_schema, table_name from information_schema.tables "
            "where table_schema = :schema and lower(table_name) like '%session%'"
        )
        params = {"schema": schema}
    elif backend == "mysql":
        driver = str(verify.get("driver") or "mysql+pymysql")
        user = urllib.parse.quote_plus(str(env.get("DATABASE_MYSQL_USER") or ""))
        password = urllib.parse.quote_plus(
            str(env.get("DATABASE_MYSQL_PASSWORD") or "")
        )
        host = str(env.get("DATABASE_MYSQL_HOST") or "")
        database = str(env.get("DATABASE_MYSQL_DATABASE") or "")
        db_url = f"{driver}://{user}:{password}@{host}/{database}"
        table_query = text(
            "select table_schema, table_name from information_schema.tables "
            "where table_schema = database() and lower(table_name) like '%session%'"
        )
        params = {}
    else:
        raise SmokeError(f"db_verify does not support backend: {backend}")

    engine = create_engine(db_url, pool_pre_ping=True)
    with engine.connect() as conn:
        tables = [dict(row._mapping) for row in conn.execute(table_query, params)]
        hits: list[dict[str, Any]] = []
        for table in tables:
            table_schema = table.get("table_schema")
            table_name = table.get("table_name")
            if not table_name:
                continue
            if backend == "postgresql":
                qualified = f'"{table_schema}"."{table_name}"'
            else:
                qualified = f"`{table_name}`"
            try:
                count = conn.execute(
                    text(f"select count(*) from {qualified} where id = :session_id"),
                    {"session_id": session_id},
                ).scalar()
            except Exception:
                count = conn.execute(text(f"select count(*) from {qualified}")).scalar()
            hits.append(
                {"table": f"{table_schema}.{table_name}", "count": int(count or 0)}
            )
    return {"tables": tables, "hits": hits}


def enabled_cases(config: dict[str, Any], selected: str | None) -> list[str]:
    cases = config.get("cases") or {}
    out = []
    for name, cfg in cases.items():
        if selected and name != selected:
            continue
        if truthy((cfg or {}).get("enabled")):
            out.append(str(name))
    return out


def print_plan(config: dict[str, Any], cases: list[str]) -> None:
    deployment = config.get("deployment") or {}
    if studio_deploy_enabled(config):
        deploy_args, _ = build_studio_deploy_command(config)
        print("Studio deploy: enabled")
        print("Studio deploy command:", redact_command(deploy_args))
    else:
        print("Studio deploy: disabled")
    print("Studio:", deep_get(config, "studio.base_url", ""))
    print("Region:", deployment.get("region") or "cn-beijing")
    print("Project:", deployment.get("project_name") or "default")
    print("Network:", json.dumps(runtime_network(config), ensure_ascii=False))
    print("Cases:")
    for name in cases:
        case = (config.get("cases") or {}).get(name) or {}
        env = case.get("env") or {}
        redacted = {k: redact_key(k, str(v)) for k, v in env.items()}
        print(f"  - {name}: backend={case.get('backend') or name}, env={redacted}")


def validate_config(config: dict[str, Any], cases: list[str]) -> None:
    errors: list[str] = []
    if not str(deep_get(config, "studio.base_url", "") or "").strip():
        errors.append("studio.base_url is required.")

    network_config = (config.get("deployment") or {}).get("network") or {}
    if isinstance(network_config, dict) and str(
        network_config.get("mode") or ""
    ).strip() in {"private", "both"}:
        if not str(
            network_config.get("vpc_id") or network_config.get("VpcId") or ""
        ).strip():
            errors.append(
                "deployment.network.vpc_id is required for private/both mode."
            )

    required_by_backend = {
        "postgresql": [
            "DATABASE_POSTGRESQL_HOST",
            "DATABASE_POSTGRESQL_PORT",
            "DATABASE_POSTGRESQL_USER",
            "DATABASE_POSTGRESQL_PASSWORD",
            "DATABASE_POSTGRESQL_DATABASE",
        ],
        "mysql": [
            "DATABASE_MYSQL_HOST",
            "DATABASE_MYSQL_USER",
            "DATABASE_MYSQL_PASSWORD",
            "DATABASE_MYSQL_DATABASE",
        ],
    }
    for case_name in cases:
        case = (config.get("cases") or {}).get(case_name) or {}
        backend = str(case.get("backend") or case_name)
        env = case.get("env") or {}
        for key in required_by_backend.get(backend, []):
            if not str(env.get(key) or "").strip():
                errors.append(f"cases.{case_name}.env.{key} is required.")

    if errors:
        raise SmokeError("Invalid config:\n- " + "\n- ".join(errors))


def delete_runtime(client: StudioClient, runtime_id: str, region: str) -> None:
    print(f"Deleting runtime {runtime_id}...")
    client.request(
        "POST", "/web/delete-runtime", {"runtimeId": runtime_id, "region": region}
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG), help="Path to config.yaml"
    )
    parser.add_argument(
        "--case", choices=["mysql", "postgresql"], help="Run one case only"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the plan without network calls",
    )
    parser.add_argument(
        "--delete-runtime",
        action="store_true",
        help="Delete each Runtime after a successful test",
    )
    parser.add_argument(
        "--deploy-studio-only",
        action="store_true",
        help="Only run the configured `veadk studio deploy` step, then exit.",
    )
    parser.add_argument(
        "--skip-studio-deploy",
        action="store_true",
        help="Skip studio.deploy even when it is enabled in config.yaml.",
    )
    parser.add_argument(
        "--continue-after-deploy",
        action="store_true",
        help="Continue into SQL smoke after Studio deploy instead of stopping for VPC/auth setup.",
    )
    args = parser.parse_args(argv)

    config = load_config(Path(args.config).expanduser().resolve())
    cases = [] if args.deploy_studio_only else enabled_cases(config, args.case)
    if not cases and not args.deploy_studio_only:
        raise SmokeError("No enabled cases selected.")
    print_plan(config, cases)
    if args.deploy_studio_only and not studio_deploy_enabled(config):
        raise SmokeError(
            "studio.deploy.enabled must be true when using --deploy-studio-only."
        )
    if args.dry_run:
        if studio_deploy_enabled(config):
            validate_studio_deploy_config(config)
        return 0

    if studio_deploy_enabled(config) and not args.skip_studio_deploy:
        validate_studio_deploy_config(config)
        studio_url = run_studio_deploy(config)
        studio_cfg = config.setdefault("studio", {})
        if not isinstance(studio_cfg, dict):
            raise SmokeError("studio must be a mapping.")
        studio_cfg["base_url"] = studio_url
        print(f"\nStudio URL: {studio_url}")
        stop_after_deploy = args.deploy_studio_only or (
            truthy(deep_get(config, "studio.deploy.stop_after_deploy", True))
            and not args.continue_after_deploy
        )
        if stop_after_deploy:
            print(
                "\nStudio deploy finished. Attach the VeFaaS application to the target "
                "VPC/subnet, open Studio once to authenticate, put the URL/auth values "
                "in config.yaml, then rerun without --deploy-studio-only."
            )
            return 0
    elif args.deploy_studio_only:
        raise SmokeError(
            "--deploy-studio-only cannot be combined with --skip-studio-deploy."
        )

    validate_config(config, cases)

    client = StudioClient(config)
    results = []
    region = str((config.get("deployment") or {}).get("region") or "cn-beijing")
    cleanup_on_success = args.delete_runtime or truthy(
        deep_get(config, "cleanup.delete_runtime_on_success")
    )
    cleanup_on_failure = truthy(deep_get(config, "cleanup.delete_runtime_on_failure"))

    for case_name in cases:
        runtime_id = ""
        try:
            print(f"\n=== Case: {case_name} ===")
            draft = build_draft(config, case_name)
            project = generate_project(client, draft)
            final = deploy_project(client, config, case_name, project)
            runtime_id = str(final["runtimeId"])
            invocation = invoke_runtime(
                client, config, runtime_id, region, project["name"]
            )
            db_result = maybe_verify_db(config, case_name, invocation["session_id"])
            result = {
                "case": case_name,
                "success": True,
                "runtimeId": runtime_id,
                "agentName": final.get("agentName"),
                "app": invocation["app"],
                "sessionId": invocation["session_id"],
                "response": invocation["response_text"],
                "dbVerify": db_result,
            }
            results.append(result)
            print("Case passed:", json.dumps(result, ensure_ascii=False, indent=2))
            if cleanup_on_success and runtime_id:
                delete_runtime(client, runtime_id, region)
        except Exception:
            if cleanup_on_failure and runtime_id:
                try:
                    delete_runtime(client, runtime_id, region)
                except Exception as cleanup_error:
                    print(f"Runtime cleanup failed: {cleanup_error}", file=sys.stderr)
            raise

    print("\n=== Summary ===")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SmokeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
