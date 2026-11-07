# GameAds-Agent

GameAds-Agent is a Windows-local demonstration for synthetic game-ad campaign analytics and budget review. It combines deterministic campaign metrics with a persisted task workflow for budget suggestions, explicit human approval, local simulation, and evidence reports.

**This is not an advertising integration or production service.** It does not place ads, change a budget on a platform, authenticate approvers, or support network/multi-user deployment. Use synthetic data only.

> **Publication status:** the public repository is [YsC-Brandon/GameAds-Agent](https://github.com/YsC-Brandon/GameAds-Agent). Original project code, documentation, the generator and synthetic CSVs are released under the [MIT License](LICENSE), copyright (c) 2026 YsC-Brandon. Remote CI is configured and its result is recorded in [release status](docs/release-status.md).

## Current Engineering Status

- Phase 4: **NOT ACCEPTED**.
- R05: **BLOCKED**. The frozen r4 switch protocol is unresolved; candidate activation/switch remains blocked.
- Power-loss durability: **UNPROVEN**. A clean process restart is not a power-loss test.
- Docker: **NOT VERIFIED** and outside the supported demo path.
- Earlier acceptance regression: 1,339 passes with `source_matches_tested_snapshot=false`; a separate 45-test run is not a full replacement. A later original-worktree run recorded 1,339 passes in 513.26 seconds, separately from acceptance. Neither establishes public-clone or remote CI success.

See [the public review status](docs/release-status.md) for this candidate's publication and evidence boundaries. The internal Phase 4A contract and historical evidence archive are not part of the proposed public file set; do not infer that target requirements are implemented or accepted.

## Supported Paths

### Default compatibility mode

`python -m app.server` launches the existing compatibility application on `127.0.0.1:8000`. This mode has no unified Phase 4 task API. It is suitable for health checks, synthetic CSV analytics, and existing Phase 3 routes. No API key is needed to start the service or use deterministic functions. Chat/model calls require separately configured credentials and may contact a provider; they are not part of this tutorial.

### Candidate task mode

The complete budget-demo workflow uses an explicitly initialized **new candidate database** and report directory. It does not migrate, activate, or switch an existing database. It supports a trusted single local user only. Approver names are self-attested labels, not authentication. Never expose the service beyond loopback.

## Windows Quick Start

Requirements: Windows, Python 3.11, Git, and network access to download Python packages. A public clone URL cannot yet be supplied. Open PowerShell in the authorized source directory; commands below are repository-relative and do not depend on a particular drive:

```powershell
py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -c requirements-windows-py311.lock -e '.[dev]'
& .\.venv\Scripts\python.exe -m pip check
```

If the Python launcher is unavailable, replace `py -3.11` with the full path to your Python 3.11 executable. The constraints file pins versions; it is not a cross-platform lockfile or wheel-hash attestation.

No `.env` file or model key is required. The demo explicitly clears the model key and model name for the server process. Do not copy real credentials into the example configuration.

## Full Offline Budget Demo

Run from the repository root in PowerShell. Choose a new absolute local directory outside the repository. The script refuses to initialize an existing database.

Terminal 1:

```powershell
$demo = Join-Path $env:LOCALAPPDATA 'GameAds-Agent-Demo'
New-Item -ItemType Directory -Path $demo -ErrorAction Stop | Out-Null
$database = Join-Path $demo 'candidate.sqlite3'
$reports = Join-Path $demo 'reports'
& .\.venv\Scripts\python.exe -m app.phase4.maintenance init --database $database
if ($LASTEXITCODE -ne 0) { throw 'Candidate initialization failed' }
$env:GAMEADS_LLM_API_KEY = ''
$env:GAMEADS_LLM_MODEL = ''
& .\.venv\Scripts\python.exe -m app.server --database $database --reports-dir $reports --port 8000
```

Keep this terminal running. The server binds only to loopback. Open a second PowerShell terminal in the repository root:

```powershell
$python = '.\.venv\Scripts\python.exe'
$cli = @('-m', 'app.task_cli', '--base-url', 'http://127.0.0.1:8000')
$request = @{
    kind = 'budget_review'
    request = @{
        start_date = '2026-10-24'
        end_date = '2026-10-30'
        budget_period = @{ start_date = '2026-11-01'; end_date = '2026-11-07' }
        campaigns = @(@{
            campaign_id = 'C001'
            current_budget = '100.00'
            min_budget = '0.00'
            max_budget = '200.00'
        })
    }
} | ConvertTo-Json -Depth 8 -Compress
$created = $request | & $python @cli create --request-json - --idempotency-key demo-create | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Budget review failed' }
$task = $created.data
$proposal = Invoke-RestMethod "http://127.0.0.1:8000/api/v1/budget-proposals/$($task.proposal_id)"
$hash = $proposal.data.proposal_sha256

# Inspect the proposal before explicitly confirming the synthetic approval.
$proposal | ConvertTo-Json -Depth 10
& $python @cli approve $task.id --expected-task-version 3 --expected-version 1 --proposal-sha256 $hash --approver demo-reviewer --confirm --idempotency-key demo-approve
if ($LASTEXITCODE -ne 0) { throw 'Approval failed' }
& $python @cli simulate $task.id --expected-task-version 4 --expected-version 2 --proposal-sha256 $hash --idempotency-key demo-simulate
if ($LASTEXITCODE -ne 0) { throw 'Local simulation failed' }
$completed = & $python @cli report $task.id --expected-task-version 5 --idempotency-key demo-report | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Report generation failed' }
$completed.data
& $python @cli report-get $completed.data.report_id
```

Stop the server with Ctrl+C, then restart the same candidate database and report directory using the Terminal 1 command (omit `init`, which correctly refuses an existing database). In Terminal 2, query the same task:

```powershell
& $python @cli get $task.id
```

The task should remain `completed`. This verifies process restart and local queryability only; it does not establish power-loss durability, backup readiness, or production recovery. The budget workflow uses explicit synthetic request values and does not call an LLM or modify campaign data.

## Architecture

Deterministic campaign tools read the bundled synthetic CSV. In candidate mode, a local CLI sends explicit task commands to a loopback-only service, which persists task state in a separate candidate SQLite database. A person reviews and confirms the proposal before a local-only simulation; a report records the resulting evidence. No advertising platform is contacted.

Artwork and reconstructed report previews are excluded until their redistribution rights are established. No live application screenshot is claimed.

## Development Checks

Install development dependencies and run the documented local checks from the repository root:

```powershell
& .\.venv\Scripts\python.exe -m pip install -c requirements-windows-py311.lock -e '.[dev]'
.\scripts\check_public.ps1 -EvidenceDir 'docs\generated\my-unique-local-run'
```

The second command runs dependency consistency, Ruff, compilation, public-source pytest, and runtime OpenAPI/sample validation. It explicitly deselects only two unchanged private-history byte-identity tests in `tests/phase3/test_guards.py`, because their private archives are excluded. It does not compare the private frozen OpenAPI contract and is not the internal acceptance pipeline. The internal scripts/tests/contracts remain unchanged. Do not overwrite earlier evidence directories. GitHub Actions is configured for pushes to `main`, pull requests and manual dispatch on Windows/Python 3.11; no remote run has occurred.

## Security, Data, and Licensing

- Use only synthetic data. The checked-in campaign sample is generated deterministically by `scripts/generate_demo_data.py` and is covered by the [MIT License](LICENSE).
- Keep `.env`, API keys, SQLite databases, backups, logs, reports, and local evidence out of source control.
- The service is for a trusted single user on the same machine, not a security boundary against other programs running as that user.
- Do not connect real advertising credentials, execute real campaigns, expose a network listener, or use real business data.
- Original project code, documentation, the generator and synthetic CSVs are covered by the [MIT License](LICENSE), copyright (c) 2026 YsC-Brandon. The [dependency inventory](docs/dependency-licenses.md) describes third-party metadata; dependencies retain their own licenses.

See [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md), [release status](docs/release-status.md), and [the reviewed file scope](docs/generated/public-review/public-file-manifest.md). Internal operations/architecture documents are intentionally excluded.