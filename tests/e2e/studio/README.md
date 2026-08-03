# VeADK Studio E2E Scripts

This directory contains repository-owned copies of the existing `.agents`
Studio smoke-test scripts. The original `.agents` scripts are intentionally
kept in place; these copies make the smoke tests available as project test
assets.

## Scripts

- `scripts/studio_kb_smoke.py`: Studio knowledge-base mount and retrieval smoke.
- `scripts/studio_sql_memory_smoke.py`: Studio SQL short-term-memory smoke.
- `scripts/studio_ltm_opensearch_smoke.py`: Studio OpenSearch long-term-memory smoke.
- `scripts/studio_agent_code_bundle_smoke.py`: Downloaded/generated Agent code bundle smoke.

## Config

Copy an example config before running a script:

```bash
cp tests/e2e/studio/configs/kb.example.yaml \
  tests/e2e/studio/configs/kb.local.yaml
```

Fill `studio.base_url` and one auth method under `studio.auth`:

- `cookie`: an authenticated Studio Cookie header.
- `bearer_token`: a bearer token.
- `local_user`: local no-SSO development only.

Keep local config files under `tests/e2e/studio/configs/*.local.yaml`; they are
ignored by git.

## Run

Knowledge base:

```bash
python3 tests/e2e/studio/scripts/studio_kb_smoke.py \
  --config tests/e2e/studio/configs/kb.local.yaml \
  --dry-run
```

SQL short-term memory:

```bash
python3 tests/e2e/studio/scripts/studio_sql_memory_smoke.py \
  --config tests/e2e/studio/configs/sql_memory.local.yaml \
  --case postgresql \
  --dry-run
```

OpenSearch long-term memory:

```bash
python3 tests/e2e/studio/scripts/studio_ltm_opensearch_smoke.py \
  --config tests/e2e/studio/configs/ltm_opensearch.local.yaml \
  --dry-run
```

Generated Agent code bundle:

```bash
python3 tests/e2e/studio/scripts/studio_agent_code_bundle_smoke.py \
  /path/to/downloaded-agent-bundle.zip
```

Remove `--dry-run` only when you want the script to call a real Studio backend
and create or verify cloud/runtime resources. Use script-specific cleanup flags
such as `--delete-runtime` only for disposable runtimes.

## Notes

- These scripts can create AgentKit Runtime resources and evaluation/backend
  data. Run them against a test project/account when possible.
- Do not commit cookies, bearer tokens, access keys, database passwords, or
  generated artifacts.
- If deployment reaches Runtime creation and then fails, inspect AgentKit
  runtime logs before changing the test.
