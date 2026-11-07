# Contributing

The project is released under the [MIT License](LICENSE), copyright (c) 2026 YsC-Brandon. Submit only changes you have authority to contribute under that license; disclose third-party material and its license separately.

## Scope

The supported demonstration boundary is Windows-local, trusted single-user, and synthetic-data only. Do not add advertising integrations, real campaign actions, multi-user/network exposure, credential handling, or Phase 5 work. Do not change frozen Phase 4 contracts without explicit owner authorization.

## Local Development

Use Python 3.11 on Windows. From the repository root:

```powershell
py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -c requirements-windows-py311.lock -e '.[dev]'
& .\.venv\Scripts\python.exe -m pip check
.\scripts\check_public.ps1 -EvidenceDir 'docs\generated\my-unique-local-run'
```

If `py` is unavailable, use the full path to a Python 3.11 executable. The public validation script refuses to overwrite its evidence directory and runs dependency checks, Ruff lint/format, compilation, pytest and runtime OpenAPI/sample validation. It deselects exactly the two private-history tests named in the script, not runtime acceptance assertions. Private contract comparison remains an internal-only check. Keep generated reports, databases, logs, credentials, and private acceptance evidence out of source control.

Public CI is not Phase 4 acceptance. Phase 4 remains NOT ACCEPTED, R05 BLOCKED, power-loss durability UNPROVEN and Docker NOT VERIFIED. See [release status](docs/release-status.md) for actual publication and CI evidence.