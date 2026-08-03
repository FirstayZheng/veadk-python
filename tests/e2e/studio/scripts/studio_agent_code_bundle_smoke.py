#!/usr/bin/env python3
"""Inspect and smoke-test a Studio-generated Agent code bundle."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - local environment issue.
    raise SystemExit(
        "PyYAML is required. Run this with the veadk project .venv."
    ) from exc


SKIP_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}


def load_config(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    cfg_path = Path(path).expanduser().resolve()
    if not cfg_path.exists():
        raise SystemExit(f"Config not found: {cfg_path}")
    return yaml.safe_load(cfg_path.read_text()) or {}


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def prepare_project(
    input_path: Path, keep_dir: Path | None = None
) -> tuple[Path, Path | None]:
    source = input_path.expanduser().resolve()
    if not source.exists():
        raise SystemExit(f"Bundle path does not exist: {source}")
    if source.is_dir():
        return source, None

    work_dir = keep_dir or Path(tempfile.mkdtemp(prefix="studio-agent-code-"))
    work_dir.mkdir(parents=True, exist_ok=True)
    extract_dir = work_dir / source.stem
    extract_dir.mkdir(parents=True, exist_ok=True)

    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            archive.extractall(extract_dir)
    elif tarfile.is_tarfile(source):
        with tarfile.open(source) as archive:
            archive.extractall(extract_dir)
    else:
        raise SystemExit(f"Unsupported bundle format: {source}")

    children = [p for p in extract_dir.iterdir() if p.name not in {"__MACOSX"}]
    if len(children) == 1 and children[0].is_dir():
        return children[0], work_dir
    return extract_dir, work_dir


def iter_python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        files.append(path)
    return sorted(files)


def find_key_files(root: Path) -> dict[str, list[str]]:
    names = {
        "pyproject": ["pyproject.toml"],
        "requirements": ["requirements.txt"],
        "app": ["app.py"],
        "agent": ["agent.py", "root_agent.py"],
        "tests": [],
    }
    result: dict[str, list[str]] = {key: [] for key in names}
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        for key, wanted in names.items():
            if wanted and path.name in wanted:
                result[key].append(rel)
        if path.name.startswith("test_") and path.suffix == ".py":
            result["tests"].append(rel)
        elif "tests" in path.relative_to(root).parts and path.suffix == ".py":
            result["tests"].append(rel)
    return result


def run_command(command: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "returncode": None,
            "timeout": True,
            "stdout": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
        }


def compile_python(root: Path, timeout: int) -> dict[str, Any]:
    files = iter_python_files(root)
    failures: list[dict[str, Any]] = []
    for path in files:
        result = run_command(
            [sys.executable, "-m", "py_compile", str(path)], root, timeout
        )
        if result["returncode"] != 0:
            failures.append(
                {
                    "file": str(path.relative_to(root)),
                    "stderr": result.get("stderr", ""),
                    "stdout": result.get("stdout", ""),
                }
            )
    return {"files": len(files), "failures": failures, "success": not failures}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path", nargs="?", help="Downloaded archive or extracted project directory"
    )
    parser.add_argument("--config", help="Optional config.yaml")
    parser.add_argument("--keep-dir", help="Directory used for extraction")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    code_test = config.get("code_test") or {}
    path_value = args.path or (
        (config.get("download") or {}).get("existing_path") or ""
    )
    if not path_value:
        raise SystemExit(
            "Provide a bundle path argument or download.existing_path in config."
        )

    keep_dir = Path(args.keep_dir).expanduser().resolve() if args.keep_dir else None
    project_root, temp_root = prepare_project(Path(path_value), keep_dir)
    timeout = int(code_test.get("timeout_seconds") or 300)
    key_files = find_key_files(project_root)

    checks: dict[str, Any] = {}
    if as_bool(code_test.get("compile_python"), True):
        checks["compile_python"] = compile_python(project_root, timeout)

    commands = list(code_test.get("commands") or [])
    if as_bool(code_test.get("run_pytest_if_present"), False) and key_files["tests"]:
        commands.append([sys.executable, "-m", "pytest", "-q"])
    for module in code_test.get("import_modules") or []:
        commands.append([sys.executable, "-c", f"import {module}"])
    if commands:
        checks["commands"] = [
            run_command([str(part) for part in command], project_root, timeout)
            for command in commands
        ]

    summary = {
        "project_root": str(project_root),
        "temporary_root": str(temp_root) if temp_root else "",
        "key_files": key_files,
        "python_files": len(iter_python_files(project_root)),
        "checks": checks,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    failed = False
    compile_result = checks.get("compile_python")
    if compile_result and not compile_result.get("success"):
        failed = True
    for result in checks.get("commands") or []:
        if result.get("returncode") != 0:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
