#!/usr/bin/env python3
"""Smoke-test VeADK Studio knowledge-base mounting through Studio APIs."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
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
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required. Run with the veadk project .venv.") from exc


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
BACKENDS = {"local", "opensearch", "viking", "context_search"}
RUNTIME_ID_RE = re.compile(r"\br-[a-z0-9]+\b")
CREATING_RUNTIME_RE = re.compile(r"Creating Runtime:\s*([A-Za-z0-9_.-]+)")


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


def normalize_match_text(value: str) -> str:
    cleaned = "".join(" " if ord(ch) < 32 else ch for ch in value)
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def contains_expected(haystack: str, expected: str) -> bool:
    return normalize_match_text(expected) in normalize_match_text(haystack)


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


def redact_key(key: str, value: str) -> str:
    sensitive = ("PASSWORD", "SECRET", "TOKEN", "APIKEY", "API_KEY", "COOKIE")
    if any(part in key.upper() for part in sensitive):
        return "***" if value else ""
    return value


def clean_env(env: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for key, value in (env or {}).items():
        if value is None:
            continue
        value_str = str(value)
        if value_str.strip():
            rows.append({"key": str(key), "value": value_str})
    return rows


def auth_hint(status: int, detail: str) -> str:
    if status not in {401, 403} and "Not authenticated" not in detail:
        return ""
    return (
        "\n\nStudio authentication is missing or expired. Put an authenticated "
        "Cookie header in studio.auth.cookie, use studio.auth.bearer_token, or "
        "fall back to browser UI automation with a logged-in Studio page."
    )


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
        timeout: float | None = None,
    ) -> HttpResponse:
        url = self.base_url + path
        data = None
        headers = dict(self.headers)
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
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
        self, method: str, path: str, body: Any, *, timeout: float | None = None
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
        print(f"[{event.get('phase') or 'deploy'}] {message}", flush=True)


def safe_agent_name(config: dict[str, Any], backend: str) -> str:
    agent = config.get("agent") or {}
    explicit = str(agent.get("agent_name") or "").strip()
    if explicit:
        return explicit
    prefix = str(agent.get("name_prefix") or "studio-kb-smoke").strip()
    return f"{prefix}-{backend}-{int(time.time())}"


def build_draft(config: dict[str, Any]) -> dict[str, Any]:
    agent = config.get("agent") or {}
    backend = str(deep_get(config, "knowledgebase.backend") or "local")
    name = safe_agent_name(config, backend)
    return {
        "name": name,
        "description": str(
            agent.get("description") or "Knowledge base mount smoke test agent."
        ),
        "instruction": str(
            agent.get("instruction") or "Use the mounted knowledge base when useful."
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
        "memory": {"shortTerm": False, "longTerm": False},
        "knowledgebase": True,
        "tracing": False,
        "subAgents": [],
        "builtinTools": [],
        "customTools": [],
        "mcpTools": [],
        "shortTermBackend": "local",
        "longTermBackend": "local",
        "autoSaveSession": False,
        "knowledgebaseBackend": backend,
        "tracingExporters": [],
        "selectedSkills": [],
        "deployment": {"feishuEnabled": False},
    }


def runtime_network(config: dict[str, Any]) -> dict[str, Any] | None:
    backend = str(deep_get(config, "knowledgebase.backend") or "local")
    if backend == "viking":
        return None
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
    project = client.request(
        "POST", "/web/generated-agent-projects", {"draft": draft}
    ).json()
    if not isinstance(project, dict) or not project.get("files"):
        raise SmokeError("Studio returned an invalid generated project.")
    return project


def deploy_project(
    client: StudioClient, config: dict[str, Any], project: dict[str, Any]
) -> dict[str, Any]:
    deployment = config.get("deployment") or {}
    env = {}
    env.update(deployment.get("extra_env") or {})
    env.update(deep_get(config, "knowledgebase.env", {}) or {})
    payload = {
        "name": project["name"],
        "files": project["files"],
        "config": {
            "region": str(deployment.get("region") or "cn-beijing"),
            "projectName": str(deployment.get("project_name") or "default"),
            "network": runtime_network(config),
        },
        "taskId": f"kb-smoke-{int(time.time())}",
        "envs": clean_env(env),
    }
    print(f"Deploying {project['name']}...")
    events = client.stream_sse("POST", "/web/deploy-agentkit", payload)
    final = next((event for event in reversed(events) if event.get("done")), None)
    if not final:
        raise SmokeError("Deployment stream ended without a terminal frame.")
    if not final.get("success"):
        runtime_ref = extract_runtime_ref(project["name"], events, final)
        fetch_agentkit_runtime_logs(runtime_ref)
        raise SmokeError(f"Deployment failed: {final.get('error') or final}")
    if not final.get("runtimeId"):
        raise SmokeError(f"Deployment did not return runtimeId: {final}")
    return final


def package_runtime_ingest_files(
    config: dict[str, Any], project: dict[str, Any]
) -> dict[str, Any] | None:
    backend = str(deep_get(config, "knowledgebase.backend") or "")
    verification = config.get("verification") or {}
    files = [
        str(path)
        for path in as_list(verification.get("ingest_files"))
        if str(path).strip()
    ]
    if backend not in {"local", "opensearch"} or not files:
        return None

    paths = [Path(path).expanduser().resolve() for path in files]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise SmokeError(f"verification.ingest_files missing files: {missing}")

    app = str(project.get("name") or "").strip()
    if not app:
        raise SmokeError("Generated project has no name; cannot package KB docs.")
    agent_path = f"agents/{app}/agent.py"
    files_list = project.get("files") or []
    agent_file = next(
        (item for item in files_list if item.get("path") == agent_path), None
    )
    if not agent_file:
        raise SmokeError(
            f"Generated project missing {agent_path}; cannot inject KB docs."
        )

    max_chars = int(verification.get("local_packaged_text_max_chars") or 120000)
    packaged: list[dict[str, Any]] = []
    for source in paths:
        text = extract_text_for_local_packaging(source)
        chunks = split_text(text, max_chars)
        for idx, chunk in enumerate(chunks, start=1):
            target_name = safe_asset_name(source, idx, len(chunks))
            target_path = f"agents/{app}/knowledgebase/{target_name}"
            files_list.append({"path": target_path, "content": chunk})
            packaged.append(
                {"source": str(source), "path": target_path, "chars": len(chunk)}
            )

    agent_file["content"] = inject_runtime_kb_loader(
        str(agent_file.get("content") or ""), backend
    )
    print(
        f"Packaged {len(packaged)} {backend} KB text asset(s) into generated project."
    )
    return {"mode": "packaged_at_build", "backend": backend, "files": packaged}


def extract_text_for_local_packaging(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text = extract_pdf_text(path)
    else:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SmokeError(
                f"{path} is not UTF-8 text. Convert it to .txt, or add PDF extraction support for this format."
            ) from exc
    text = text.strip()
    if not text:
        raise SmokeError(f"No text extracted from {path}.")
    return text


def extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as first_error:
        try:
            import pdfplumber  # type: ignore

            with pdfplumber.open(str(path)) as pdf:
                return "\n".join(page.extract_text() or "" for page in pdf.pages)
        except Exception:
            if shutil.which("pdftotext"):
                completed = subprocess.run(
                    ["pdftotext", str(path), "-"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=120,
                    check=False,
                )
                if completed.returncode == 0 and completed.stdout.strip():
                    return completed.stdout
            raise SmokeError(
                f"Could not extract PDF text from {path}. Install pypdf/pdfplumber or provide a .txt file."
            ) from first_error


def split_text(text: str, max_chars: int) -> list[str]:
    if max_chars <= 0:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + max_chars])
        start += max_chars
    return chunks or [text]


def safe_asset_name(path: Path, idx: int, total: int) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.stem).strip("._") or "document"
    if total == 1:
        return f"{stem}.txt"
    return f"{stem}_{idx:03d}.txt"


def inject_runtime_kb_loader(agent_py: str, backend: str) -> str:
    if "_load_packaged_kb_documents()" in agent_py:
        return agent_py
    match = re.search(
        rf"^(\w+)\s*=\s*KnowledgeBase\(backend=\"{re.escape(backend)}\".*$",
        agent_py,
        flags=re.MULTILINE,
    )
    if not match:
        raise SmokeError(
            f"Could not find {backend} KnowledgeBase assignment in generated agent.py."
        )
    kb_var = match.group(1)
    loader = f"""

def _load_packaged_kb_documents():
    kb_dir = __import__("pathlib").Path(__file__).resolve().parent / "knowledgebase"
    if not kb_dir.exists():
        return
    kb_files = sorted(str(path) for path in kb_dir.rglob("*") if path.is_file())
    if kb_files:
        {kb_var}.add_from_files(kb_files)


_load_packaged_kb_documents()
"""
    insert_at = match.end()
    return agent_py[:insert_at] + loader + agent_py[insert_at:]


def extract_runtime_ref(
    project_name: str, events: list[dict[str, Any]], final: dict[str, Any] | None = None
) -> str:
    if final:
        for key in ("runtimeId", "agentName"):
            value = str(final.get(key) or "").strip()
            if value:
                return value
    runtime_name = ""
    for event in events:
        message = str(event.get("message") or "")
        id_match = RUNTIME_ID_RE.search(message)
        if id_match:
            return id_match.group(0)
        name_match = CREATING_RUNTIME_RE.search(message)
        if name_match:
            runtime_name = name_match.group(1)
    return runtime_name or project_name


def redact_log_text(text: str) -> str:
    redacted_lines = []
    sensitive = ("PASSWORD", "SECRET", "TOKEN", "APIKEY", "API_KEY", "COOKIE", "KEY")
    for line in text.splitlines():
        upper = line.upper()
        if any(marker in upper for marker in sensitive):
            redacted_lines.append("[redacted sensitive log line]")
        else:
            redacted_lines.append(line)
    return "\n".join(redacted_lines)


def fetch_agentkit_runtime_logs(runtime_ref: str) -> None:
    if not runtime_ref:
        return
    if not shutil.which("agentkit"):
        print("agentkit CLI not found; skip Runtime log fetch.", file=sys.stderr)
        return
    command = ["agentkit", "runtime", "logs", runtime_ref, "-n", "200"]
    print(f"\n=== AgentKit Runtime Logs: {runtime_ref} ===", file=sys.stderr)
    try:
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
    except Exception as exc:
        print(f"Could not fetch AgentKit Runtime logs: {exc}", file=sys.stderr)
        return
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    if output.strip():
        print(redact_log_text(output[-12000:]), file=sys.stderr)
    if completed.returncode != 0:
        print(
            f"agentkit runtime logs exited with {completed.returncode}.",
            file=sys.stderr,
        )


def proxy_path(runtime_id: str, region: str, path: str) -> str:
    quoted = urllib.parse.quote(runtime_id, safe="")
    sep = "&" if "?" in path else "?"
    return f"/web/runtime-proxy/{quoted}{path}{sep}region={urllib.parse.quote(region)}"


def request_with_retries(
    client: StudioClient,
    method: str,
    path: str,
    body: Any | None = None,
    *,
    attempts: int = 6,
    delay_seconds: float = 10.0,
) -> HttpResponse:
    last_error: SmokeError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return client.request(method, path, body)
        except SmokeError as exc:
            last_error = exc
            if attempt == attempts:
                break
            print(f"Runtime proxy not ready yet; retrying {attempt}/{attempts}...")
            time.sleep(delay_seconds)
    assert last_error is not None
    raise last_error


def verify_mount(
    client: StudioClient,
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
    kb_components = [
        c
        for c in components
        if isinstance(c, dict) and c.get("kind") == "knowledgebase"
    ]
    if not kb_components:
        raise SmokeError(f"Agent info does not report a mounted knowledgebase: {info}")
    expected = str(deep_get(config, "knowledgebase.backend") or "")
    if expected and not any(c.get("backend") == expected for c in kb_components):
        raise SmokeError(
            f"Knowledgebase backend mismatch. expected={expected}, components={kb_components}"
        )
    return {"app": app, "agentInfo": info, "knowledgebaseComponents": kb_components}


def verify_search(
    client: StudioClient, config: dict[str, Any], runtime_id: str, region: str, app: str
) -> dict[str, Any] | None:
    verification = config.get("verification") or {}
    query = str(verification.get("query") or "").strip()
    expected = str(verification.get("expected_contains") or "").strip()
    require_result = truthy(verification.get("require_search_result")) or bool(expected)
    if not query and not require_result:
        return None
    if not query:
        raise SmokeError(
            "verification.query is required when search result verification is enabled."
        )
    params = urllib.parse.urlencode(
        {
            "source": "knowledge",
            "app_name": app,
            "q": query,
            "user_id": str(verification.get("user_id") or "studio_kb_smoke_user"),
        }
    )
    result = client.request(
        "GET", proxy_path(runtime_id, region, f"/web/search?{params}")
    ).json()
    if not isinstance(result, dict) or not result.get("mounted"):
        raise SmokeError(f"Knowledge search source is not mounted: {result}")
    results = result.get("results") or []
    haystack = "\n".join(
        str(item.get("content") or "") for item in results if isinstance(item, dict)
    )
    if require_result and not haystack.strip():
        raise SmokeError(f"Knowledge search returned no content: {result}")
    if expected and not contains_expected(haystack, expected):
        raise SmokeError(
            f"Expected text not found in knowledge search results: {expected}"
        )
    return result


def knowledgebase_index_for_app(app: str) -> str:
    return f"{app}_kb"


def ingest_files_into_backend(
    config: dict[str, Any], app: str
) -> dict[str, Any] | None:
    verification = config.get("verification") or {}
    files = [
        str(path)
        for path in as_list(verification.get("ingest_files"))
        if str(path).strip()
    ]
    if not files:
        return None
    backend = str(deep_get(config, "knowledgebase.backend") or "")
    if backend == "local":
        return None
    if backend != "viking":
        raise SmokeError(
            "verification.ingest_files is currently supported only for backend=viking."
        )

    paths = [Path(path).expanduser().resolve() for path in files]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise SmokeError(f"verification.ingest_files missing files: {missing}")

    try:
        from veadk.knowledgebase import KnowledgeBase
    except ImportError as exc:
        raise SmokeError(
            "veadk package is required for local backend ingestion."
        ) from exc

    index = knowledgebase_index_for_app(app)
    env = deep_get(config, "knowledgebase.env", {}) or {}
    old_env: dict[str, str | None] = {}
    for key, value in env.items():
        if value is None:
            continue
        old_env[str(key)] = os.environ.get(str(key))
        os.environ[str(key)] = str(value)
    try:
        kb = KnowledgeBase(backend=backend, index=index, app_name=index)
        kwargs: dict[str, Any] = {
            "tos_bucket_path": str(
                verification.get("ingest_tos_bucket_path") or f"knowledgebase/{app}"
            ),
        }
        bucket = str(verification.get("ingest_tos_bucket_name") or "").strip()
        if bucket:
            kwargs["tos_bucket_name"] = bucket
        metadata = verification.get("ingest_metadata")
        if isinstance(metadata, dict) and metadata:
            kwargs["metadata"] = metadata
        print(f"Ingesting {len(paths)} file(s) into {backend} index {index}...")
        ok = kb.add_from_files([str(path) for path in paths], **kwargs)
        if not ok:
            raise SmokeError("KnowledgeBase.add_from_files returned false.")
        docs = wait_for_ingested_chunks(kb, config)
        local_search = verify_local_backend_search(kb, config)
        return {
            "backend": backend,
            "index": index,
            "files": [str(path) for path in paths],
            "docs": docs,
            "localSearch": local_search,
        }
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def wait_for_ingested_chunks(kb: Any, config: dict[str, Any]) -> list[dict[str, Any]]:
    verification = config.get("verification") or {}
    attempts = int(verification.get("ingest_poll_attempts") or 12)
    delay = float(verification.get("ingest_poll_interval_seconds") or 10)
    docs: list[dict[str, Any]] = []
    for attempt in range(1, attempts + 1):
        docs = kb.list_docs(offset=0, limit=20)
        point_total = sum(
            int(doc.get("point_num") or 0) for doc in docs if isinstance(doc, dict)
        )
        print(f"Ingestion poll {attempt}/{attempts}: point_num_total={point_total}")
        if point_total > 0:
            break
        if attempt < attempts:
            time.sleep(delay)
    if not docs:
        raise SmokeError("Knowledge ingestion produced no documents.")
    if not any(
        int(doc.get("point_num") or 0) > 0 for doc in docs if isinstance(doc, dict)
    ):
        raise SmokeError(f"Knowledge ingestion produced no chunks: {docs}")
    return summarize_docs(docs)


def summarize_docs(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        summary.append(
            {
                "doc_name": doc.get("doc_name"),
                "doc_id": doc.get("doc_id"),
                "doc_type": doc.get("doc_type"),
                "point_num": doc.get("point_num"),
                "status": doc.get("status"),
            }
        )
    return summary


def verify_local_backend_search(
    kb: Any, config: dict[str, Any]
) -> dict[str, Any] | None:
    verification = config.get("verification") or {}
    if not truthy(verification.get("verify_local_backend_search")):
        return None
    query = str(verification.get("query") or "").strip()
    if not query:
        raise SmokeError(
            "verification.query is required for local backend search verification."
        )
    expected = str(verification.get("expected_contains") or "").strip()
    entries = kb.search(query=query, top_k=3)
    contents = [str(getattr(entry, "content", entry)) for entry in entries]
    haystack = "\n".join(contents)
    if not haystack.strip():
        raise SmokeError("Local backend search returned no content.")
    if expected and not contains_expected(haystack, expected):
        raise SmokeError(
            f"Expected text not found in local backend search results: {expected}"
        )
    return {
        "resultCount": len(contents),
        "firstContent": contents[0][:1200] if contents else "",
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


def verify_chat(
    client: StudioClient, config: dict[str, Any], runtime_id: str, region: str, app: str
) -> dict[str, Any] | None:
    verification = config.get("verification") or {}
    messages = [
        str(m) for m in as_list(verification.get("chat_messages")) if str(m).strip()
    ]
    if not messages:
        return None
    user_id = str(verification.get("user_id") or "studio_kb_smoke_user")
    session_id = f"studio-kb-smoke-{int(time.time())}"
    session_path = f"/apps/{urllib.parse.quote(app, safe='')}/users/{urllib.parse.quote(user_id, safe='')}/sessions"
    created_session = client.request(
        "POST", proxy_path(runtime_id, region, session_path), {}
    ).json()
    if isinstance(created_session, dict) and created_session.get("id"):
        session_id = str(created_session["id"])
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
            "POST", proxy_path(runtime_id, region, "/run_sse"), payload
        )
        for event in events:
            if (
                event.get("error")
                or event.get("errorMessage")
                or event.get("error_message")
            ):
                raise SmokeError(f"Runtime returned error event: {event}")
        all_events.extend(events)
    text = collect_text(all_events)
    expected = str(verification.get("expected_contains") or "").strip()
    if expected and not contains_expected(text, expected):
        raise SmokeError(f"Expected text not found in chat response: {expected}")
    return {"sessionId": session_id, "response": text, "eventCount": len(all_events)}


def validate_config(config: dict[str, Any]) -> None:
    errors: list[str] = []
    if not str(deep_get(config, "studio.base_url", "") or "").strip():
        errors.append("studio.base_url is required.")
    backend = str(deep_get(config, "knowledgebase.backend") or "local")
    if backend not in BACKENDS:
        errors.append(
            f"knowledgebase.backend must be one of: {', '.join(sorted(BACKENDS))}."
        )
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
    env = deep_get(config, "knowledgebase.env", {}) or {}
    required_by_backend = {
        "opensearch": [
            "DATABASE_OPENSEARCH_HOST",
            "DATABASE_OPENSEARCH_USERNAME",
            "DATABASE_OPENSEARCH_PASSWORD",
        ],
        "context_search": [
            "DATABASE_CONTEXT_SEARCH_ENGINE_ID",
            "DATABASE_CONTEXT_SEARCH_ENGINE_ENDPOINT",
            "DATABASE_CONTEXT_SEARCH_ENGINE_APIKEY",
        ],
    }
    for key in required_by_backend.get(backend, []):
        if not str(env.get(key) or "").strip():
            errors.append(f"knowledgebase.env.{key} is required for backend={backend}.")
    if errors:
        raise SmokeError("Invalid config:\n- " + "\n- ".join(errors))


def print_plan(config: dict[str, Any]) -> None:
    backend = str(deep_get(config, "knowledgebase.backend") or "local")
    env = deep_get(config, "knowledgebase.env", {}) or {}
    redacted = {str(k): redact_key(str(k), str(v)) for k, v in env.items()}
    print("Studio:", deep_get(config, "studio.base_url", ""))
    print("Region:", deep_get(config, "deployment.region", "cn-beijing"))
    print("Project:", deep_get(config, "deployment.project_name", "default"))
    print("Network:", json.dumps(runtime_network(config), ensure_ascii=False))
    print("Knowledgebase:", backend, redacted)


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
        "--dry-run",
        action="store_true",
        help="Validate and print the plan without network calls",
    )
    parser.add_argument(
        "--delete-runtime",
        action="store_true",
        help="Delete Runtime after a successful test",
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
    cleanup_on_success = args.delete_runtime or truthy(
        deep_get(config, "cleanup.delete_runtime_on_success")
    )
    cleanup_on_failure = truthy(deep_get(config, "cleanup.delete_runtime_on_failure"))
    runtime_id = ""
    try:
        if args.runtime_id:
            runtime_id = str(args.runtime_id)
            preferred_app = str(
                args.app_name or deep_get(config, "agent.agent_name") or ""
            )
            mount = verify_mount(client, config, runtime_id, region, preferred_app)
            search = verify_search(client, config, runtime_id, region, mount["app"])
            chat = verify_chat(client, config, runtime_id, region, mount["app"])
            result = {
                "success": True,
                "runtimeId": runtime_id,
                "agentName": None,
                "app": mount["app"],
                "knowledgebase": mount["knowledgebaseComponents"],
                "ingestion": None,
                "search": search,
                "chat": chat,
            }
            print("\n=== Summary ===")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if cleanup_on_success:
                delete_runtime(client, runtime_id, region)
            return 0

        draft = build_draft(config)
        project = generate_project(client, draft)
        packaged_ingestion = package_runtime_ingest_files(config, project)
        final = deploy_project(client, config, project)
        runtime_id = str(final["runtimeId"])
        mount = verify_mount(client, config, runtime_id, region, project["name"])
        ingestion = packaged_ingestion or ingest_files_into_backend(
            config, mount["app"]
        )
        search = verify_search(client, config, runtime_id, region, mount["app"])
        chat = verify_chat(client, config, runtime_id, region, mount["app"])
        result = {
            "success": True,
            "runtimeId": runtime_id,
            "agentName": final.get("agentName"),
            "app": mount["app"],
            "knowledgebase": mount["knowledgebaseComponents"],
            "ingestion": ingestion,
            "search": search,
            "chat": chat,
        }
        print("\n=== Summary ===")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if cleanup_on_success and runtime_id:
            delete_runtime(client, runtime_id, region)
        return 0
    except Exception:
        if cleanup_on_failure and runtime_id:
            try:
                delete_runtime(client, runtime_id, region)
            except Exception as cleanup_error:
                print(f"Runtime cleanup failed: {cleanup_error}", file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SmokeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
