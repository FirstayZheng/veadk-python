#!/usr/bin/env python3
"""Smoke-test VeADK Studio OpenSearch long-term memory through Studio APIs."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required. Run with the veadk project .venv.") from exc


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
KB_SMOKE_SCRIPT = Path(__file__).resolve().parent / "studio_kb_smoke.py"


def load_shared() -> Any:
    spec = importlib.util.spec_from_file_location(
        "studio_kb_smoke_shared", KB_SMOKE_SCRIPT
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load shared smoke helpers from {KB_SMOKE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


shared = load_shared()
SmokeError = shared.SmokeError
StudioClient = shared.StudioClient
as_list = shared.as_list
clean_env = shared.clean_env
contains_expected = shared.contains_expected
deep_get = shared.deep_get
proxy_path = shared.proxy_path
redact_key = shared.redact_key
request_with_retries = shared.request_with_retries
runtime_network = shared.runtime_network
truthy = shared.truthy


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SmokeError(f"Config file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SmokeError("Config root must be a YAML mapping.")
    base_ref = str(data.pop("extends", "") or "").strip()
    if base_ref:
        base_path = Path(base_ref).expanduser()
        if not base_path.is_absolute():
            base_path = path.parent / base_path
        data = deep_merge(load_config(base_path.resolve()), data)
    return data


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def ltm_env(config: dict[str, Any]) -> dict[str, Any]:
    env = deep_get(config, "long_term_memory.env", None)
    if isinstance(env, dict) and env:
        return env
    inherited = deep_get(config, "knowledgebase.env", None)
    if isinstance(inherited, dict):
        return inherited
    return {}


def safe_agent_name(config: dict[str, Any]) -> str:
    agent = config.get("agent") or {}
    explicit = str(agent.get("agent_name") or "").strip()
    if explicit:
        return explicit
    prefix = str(agent.get("name_prefix") or "ltmos").strip()
    return f"{prefix}{int(time.time())}"


def build_draft(config: dict[str, Any]) -> dict[str, Any]:
    agent = config.get("agent") or {}
    name = safe_agent_name(config)
    return {
        "name": name,
        "description": str(
            agent.get("description") or "OpenSearch long-term memory smoke test agent."
        ),
        "instruction": str(
            agent.get("instruction")
            or (
                "You are a concise memory smoke-test assistant. When the user gives a "
                "test memory fact, acknowledge it. When asked later, use long-term memory "
                "if needed and answer with the exact requested token."
            )
        ),
        "agentType": "llm",
        "maxIterations": 3,
        "a2aUrl": "",
        "model": "",
        "modelName": str(agent.get("model_name") or "doubao-seed-2-1-pro-260628"),
        "modelProvider": str(agent.get("model_provider") or ""),
        "modelApiBase": str(agent.get("model_api_base") or ""),
        "tools": [],
        "skills": [],
        "memory": {
            "shortTerm": truthy(agent.get("short_term_memory")),
            "longTerm": True,
        },
        "knowledgebase": False,
        "tracing": False,
        "subAgents": [],
        "builtinTools": [
            str(item)
            for item in as_list(agent.get("builtin_tools"))
            if str(item).strip()
        ],
        "customTools": [],
        "mcpTools": [],
        "shortTermBackend": str(agent.get("short_term_backend") or "local"),
        "longTermBackend": "opensearch",
        "autoSaveSession": truthy(agent.get("auto_save_session", True)),
        "knowledgebaseBackend": "local",
        "tracingExporters": [],
        "selectedSkills": [],
        "deployment": {"feishuEnabled": False},
    }


def generate_project(client: Any, draft: dict[str, Any]) -> dict[str, Any]:
    project = client.request(
        "POST", "/web/generated-agent-projects", {"draft": draft}
    ).json()
    if not isinstance(project, dict) or not project.get("files"):
        raise SmokeError("Studio returned an invalid generated project.")
    return project


def deploy_project(
    client: Any, config: dict[str, Any], project: dict[str, Any]
) -> dict[str, Any]:
    deployment = config.get("deployment") or {}
    env = {}
    env.update(deployment.get("extra_env") or {})
    env.update(ltm_env(config))
    payload = {
        "name": project["name"],
        "files": project["files"],
        "config": {
            "region": str(deployment.get("region") or "cn-beijing"),
            "projectName": str(deployment.get("project_name") or "default"),
            "network": runtime_network(config),
        },
        "taskId": f"ltm-opensearch-smoke-{int(time.time())}",
        "envs": clean_env(env),
    }
    print(f"Deploying {project['name']}...")
    events = client.stream_sse("POST", "/web/deploy-agentkit", payload)
    final = next((event for event in reversed(events) if event.get("done")), None)
    if not final:
        raise SmokeError("Deployment stream ended without a terminal frame.")
    if not final.get("success"):
        runtime_ref = shared.extract_runtime_ref(project["name"], events, final)
        shared.fetch_agentkit_runtime_logs(runtime_ref)
        raise SmokeError(f"Deployment failed: {final.get('error') or final}")
    if not final.get("runtimeId"):
        raise SmokeError(f"Deployment did not return runtimeId: {final}")
    return final


def verify_mount(
    client: Any,
    config: dict[str, Any],
    runtime_id: str,
    region: str,
    preferred_app: str,
) -> dict[str, Any]:
    apps = request_with_retries(
        client, "GET", proxy_path(runtime_id, region, "/list-apps")
    ).json()
    if not isinstance(apps, list) or not apps:
        raise SmokeError(f"Runtime returned no apps: {apps}")
    app = preferred_app if preferred_app in apps else str(apps[0])
    info = request_with_retries(
        client,
        "GET",
        proxy_path(
            runtime_id, region, f"/web/agent-info/{urllib.parse.quote(app, safe='')}"
        ),
    ).json()
    components = info.get("components") if isinstance(info, dict) else None
    if not isinstance(components, list):
        raise SmokeError(f"Agent info returned invalid components: {info}")
    memories = [
        c
        for c in components
        if isinstance(c, dict)
        and c.get("kind") == "memory"
        and c.get("source") == "long_term_memory"
    ]
    if not memories:
        raise SmokeError(f"Agent info does not report mounted long-term memory: {info}")
    if not any(item.get("backend") == "opensearch" for item in memories):
        raise SmokeError(f"Long-term memory backend mismatch: {memories}")
    sources = info.get("searchSources") if isinstance(info, dict) else []
    if "memory" not in (sources or []):
        raise SmokeError(f"Agent searchSources does not include memory: {info}")
    return {"app": app, "agentInfo": info, "memoryComponents": memories}


def create_session(
    client: Any, runtime_id: str, region: str, app: str, user_id: str, prefix: str
) -> str:
    session_id = f"{prefix}-{int(time.time())}"
    session_path = (
        f"/apps/{urllib.parse.quote(app, safe='')}/users/"
        f"{urllib.parse.quote(user_id, safe='')}/sessions"
    )
    created = client.request(
        "POST", proxy_path(runtime_id, region, session_path), {}
    ).json()
    if isinstance(created, dict) and created.get("id"):
        session_id = str(created["id"])
    return session_id


def run_messages(
    client: Any,
    config: dict[str, Any],
    runtime_id: str,
    region: str,
    app: str,
    user_id: str,
    messages: list[str],
    *,
    session_prefix: str,
) -> dict[str, Any]:
    session_id = create_session(
        client, runtime_id, region, app, user_id, session_prefix
    )
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
            timeout=float(deep_get(config, "verification.invoke_timeout_seconds", 900)),
        )
        for event in events:
            if (
                event.get("error")
                or event.get("errorMessage")
                or event.get("error_message")
            ):
                raise SmokeError(f"Runtime returned error event: {event}")
        all_events.extend(events)
    return {
        "sessionId": session_id,
        "response": shared.collect_text(all_events),
        "eventCount": len(all_events),
    }


def memory_messages(config: dict[str, Any]) -> list[str]:
    verification = config.get("verification") or {}
    messages = [
        str(item)
        for item in as_list(verification.get("memory_messages"))
        if str(item).strip()
    ]
    if messages:
        return messages
    sentinel = str(verification.get("expected_contains") or "").strip()
    if not sentinel:
        raise SmokeError(
            "verification.memory_messages or verification.expected_contains is required."
        )
    return [f"请记住这条长期记忆测试信息：OpenSearch 长期记忆暗号是 {sentinel}。"]


def verify_memory_search(
    client: Any,
    config: dict[str, Any],
    runtime_id: str,
    region: str,
    app: str,
    user_id: str,
) -> dict[str, Any]:
    verification = config.get("verification") or {}
    query = str(verification.get("query") or "").strip()
    expected = str(verification.get("expected_contains") or "").strip()
    if not query:
        raise SmokeError(
            "verification.query is required for long-term memory search verification."
        )
    attempts = int(verification.get("memory_search_attempts") or 12)
    delay = float(verification.get("memory_search_interval_seconds") or 10)
    last_result: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        params = urllib.parse.urlencode(
            {
                "source": "memory",
                "app_name": app,
                "q": query,
                "user_id": user_id,
            }
        )
        result = client.request(
            "GET", proxy_path(runtime_id, region, f"/web/search?{params}")
        ).json()
        if not isinstance(result, dict) or not result.get("mounted"):
            raise SmokeError(f"Long-term memory source is not mounted: {result}")
        last_result = result
        results = result.get("results") or []
        haystack = "\n".join(
            str(item.get("content") or "") for item in results if isinstance(item, dict)
        )
        if haystack.strip() and (not expected or contains_expected(haystack, expected)):
            return result
        print(f"Memory search not ready yet; retrying {attempt}/{attempts}...")
        if attempt < attempts:
            time.sleep(delay)
    if expected:
        raise SmokeError(
            f"Expected text not found in long-term memory search results: {expected}"
        )
    raise SmokeError(f"Long-term memory search returned no content: {last_result}")


def verify_chat_probe(
    client: Any,
    config: dict[str, Any],
    runtime_id: str,
    region: str,
    app: str,
    user_id: str,
) -> dict[str, Any] | None:
    verification = config.get("verification") or {}
    messages = [
        str(item)
        for item in as_list(verification.get("chat_messages"))
        if str(item).strip()
    ]
    if not messages:
        return None
    result = run_messages(
        client,
        config,
        runtime_id,
        region,
        app,
        user_id,
        messages,
        session_prefix="studio-ltm-chat",
    )
    expected = str(verification.get("expected_contains") or "").strip()
    if expected and not contains_expected(result["response"], expected):
        raise SmokeError(f"Expected text not found in chat response: {expected}")
    return result


def validate_config(config: dict[str, Any]) -> None:
    errors: list[str] = []
    if not str(deep_get(config, "studio.base_url", "") or "").strip():
        errors.append("studio.base_url is required.")
    env = ltm_env(config)
    for key in (
        "DATABASE_OPENSEARCH_HOST",
        "DATABASE_OPENSEARCH_USERNAME",
        "DATABASE_OPENSEARCH_PASSWORD",
    ):
        if not str(env.get(key) or "").strip():
            errors.append(f"long_term_memory.env.{key} is required.")
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
    if not truthy(deep_get(config, "deployment.network.enable_shared_internet_access")):
        errors.append(
            "deployment.network.enable_shared_internet_access should be true for end-to-end chat/model tests."
        )
    if errors:
        raise SmokeError("Invalid config:\n- " + "\n- ".join(errors))


def print_plan(config: dict[str, Any]) -> None:
    env = ltm_env(config)
    redacted = {str(k): redact_key(str(k), str(v)) for k, v in env.items()}
    print("Studio:", deep_get(config, "studio.base_url", ""))
    print("Region:", deep_get(config, "deployment.region", "cn-beijing"))
    print("Project:", deep_get(config, "deployment.project_name", "default"))
    print("Network:", json.dumps(runtime_network(config), ensure_ascii=False))
    print("Long-term memory:", "opensearch", redacted)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG), help="Path to config.yaml"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the plan without network calls",
    )
    parser.add_argument(
        "--runtime-id",
        default="",
        help="Verify an existing Runtime instead of deploying a new one",
    )
    parser.add_argument(
        "--app-name", default="", help="Preferred app name when --runtime-id is used"
    )
    args = parser.parse_args(argv)

    config = load_config(Path(args.config).expanduser().resolve())
    print_plan(config)
    validate_config(config)
    if args.dry_run:
        return 0

    client = StudioClient(config)
    region = str(deep_get(config, "deployment.region", "cn-beijing"))
    verification = config.get("verification") or {}
    user_id = str(verification.get("user_id") or "studio_ltm_opensearch_user")
    runtime_id = ""
    try:
        if args.runtime_id:
            runtime_id = str(args.runtime_id)
            preferred_app = str(
                args.app_name or deep_get(config, "agent.agent_name") or ""
            )
            mount = verify_mount(client, config, runtime_id, region, preferred_app)
        else:
            draft = build_draft(config)
            project = generate_project(client, draft)
            final = deploy_project(client, config, project)
            runtime_id = str(final["runtimeId"])
            mount = verify_mount(client, config, runtime_id, region, project["name"])

        seed = run_messages(
            client,
            config,
            runtime_id,
            region,
            mount["app"],
            user_id,
            memory_messages(config),
            session_prefix="studio-ltm-seed",
        )
        search = verify_memory_search(
            client, config, runtime_id, region, mount["app"], user_id
        )
        chat = verify_chat_probe(
            client, config, runtime_id, region, mount["app"], user_id
        )
        result = {
            "success": True,
            "runtimeId": runtime_id,
            "app": mount["app"],
            "memory": mount["memoryComponents"],
            "seed": seed,
            "search": search,
            "chat": chat,
        }
        print("\n=== Summary ===")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except SmokeError as exc:
        if runtime_id:
            shared.fetch_agentkit_runtime_logs(runtime_id)
        print(f"\nSmoke failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SmokeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
