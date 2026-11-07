# Exact Reviewed Public-Source Scope

This is the exact reviewed public-source scope. The project is released under the [MIT License](../../LICENSE), copyright (c) 2026 YsC-Brandon. The scope contains only reviewed text or empty-placeholder files; no general `git add .` was used.

## Disposition

- **Content-screened, rights confirmed for project material:** the explicit paths below. All are UTF-8 text or empty directory placeholders; no binaries are included.
- **Excluded:** local environments/secrets, databases/journals/backups, build output, caches, raw logs/reports, internal contracts/acceptance evidence and archives, task brief, both SVGs and other assets, Docker files, internal-only scripts and all unlisted files.
- **Included attribution:** `LICENSE` states MIT terms and `Copyright (c) 2026 YsC-Brandon`. Third-party dependency metadata remains an inventory, not a replacement for upstream licenses.

## Public Validation Boundary

`scripts/check_public.ps1` retains runtime tests and explicitly deselects the two unchanged private-history identity tests in `tests/phase3/test_guards.py`. The separate runtime OAS/sample validator does not compare the excluded frozen contract. Internal checks and acceptance standards remain unchanged locally. CI configuration alone is not proof of a remote pass.

## Exact Paths

```text
.env.example
.github/ISSUE_TEMPLATE/bug_report.yml
.github/PULL_REQUEST_TEMPLATE.md
.github/workflows/local-checks.yml
.gitignore
CONTRIBUTING.md
LICENSE
README.md
SECURITY.md
app/__init__.py
app/__main__.py
app/agent/__init__.py
app/agent/contracts.py
app/agent/graph.py
app/agent/insights.py
app/agent/prompts.py
app/agent/provider.py
app/agent/registry.py
app/agent/service.py
app/cli.py
app/core/__init__.py
app/core/config.py
app/core/exceptions.py
app/http_transport.py
app/local_boundary.py
app/main.py
app/phase3/__init__.py
app/phase3/api.py
app/phase3/budget.py
app/phase3/contracts.py
app/phase3/reports.py
app/phase3/service.py
app/phase3/store.py
app/phase3/workflow.py
app/phase4/__init__.py
app/phase4/api.py
app/phase4/contracts.py
app/phase4/generation.py
app/phase4/http_body.py
app/phase4/integrity.py
app/phase4/maintenance.py
app/phase4/offline_demo.py
app/phase4/ownership.py
app/phase4/schema.py
app/phase4/service.py
app/phase4/store.py
app/resources/sample_campaigns.csv
app/schemas.py
app/server.py
app/services/__init__.py
app/services/campaign_service.py
app/task_cli.py
app/task_wire.py
app/tools/__init__.py
app/tools/analytics_tools.py
app/tools/anomaly_tools.py
app/tools/campaign_tools.py
data/.gitkeep
data/sample_campaigns.csv
docs/dependency-licenses.md
docs/generated/public-review/README.md
docs/generated/public-review/public-file-manifest.md
docs/generated/public-review/reproduction-report.md
docs/generated/public-review/sample-report.md
docs/generated/public-review/security-review.md
docs/release-status.md
pyproject.toml
reports/.gitkeep
requirements-windows-py311.lock
scripts/check_public.ps1
scripts/generate_demo_data.py
scripts/run_recorded.py
scripts/verify_installed_runtime.py
scripts/verify_isolated_install.py
scripts/verify_public_openapi.py
tests/__init__.py
tests/api/test_phase2_api.py
tests/conftest.py
tests/integration/test_agent_workflow.py
tests/integration/test_boundary_raw.py
tests/integration/test_local_boundary.py
tests/integration/test_local_workflow.py
tests/integration/test_packaged_data.py
tests/integration/test_phase2_http_live.py
tests/integration/test_safe_launcher.py
tests/integration/test_task_wire.py
tests/integration/test_transport_security.py
tests/phase3/conftest.py
tests/phase3/test_approval.py
tests/phase3/test_budget.py
tests/phase3/test_guards.py
tests/phase3/test_http_live.py
tests/phase3/test_reports.py
tests/phase3/test_workflow_api.py
tests/phase4/test_acceptance_cli.py
tests/phase4/test_acceptance_failures.py
tests/phase4/test_acceptance_legacy.py
tests/phase4/test_acceptance_security.py
tests/phase4/test_acceptance_state.py
tests/phase4/test_application_store_ownership.py
tests/phase4/test_candidate_store.py
tests/phase4/test_closeout_admission.py
tests/phase4/test_closeout_backup_process.py
tests/phase4/test_closeout_continuation_process.py
tests/phase4/test_closeout_integrity.py
tests/phase4/test_closeout_read_physical.py
tests/phase4/test_closeout_remaining.py
tests/phase4/test_closeout_security.py
tests/phase4/test_final_contract.py
tests/phase4/test_maintenance.py
tests/phase4/test_owner_application_integration.py
tests/phase4/test_reliability_disconnect.py
tests/phase4/test_reliability_faults.py
tests/phase4/test_reliability_generation.py
tests/phase4/test_reliability_inputs.py
tests/phase4/test_reliability_migration.py
tests/phase4/test_reliability_ownership.py
tests/phase4/test_reliability_process.py
tests/phase4/test_reliability_races.py
tests/phase4/test_reliability_sealed_security.py
tests/phase4/test_reliability_semantics.py
tests/phase4/test_reliability_windows_files.py
tests/phase4/test_task_cli.py
tests/phase4/test_task_concurrency.py
tests/phase4/test_task_e2e.py
tests/phase4/test_task_failure.py
tests/transport_support.py
tests/unit/test_analytics.py
tests/unit/test_anomalies.py
tests/unit/test_campaign_data.py
tests/unit/test_config.py
tests/unit/test_provider.py
tests/unit/test_public_openapi.py
```

Total: **133 files**. When this scope changes, repeat index, content, link and source-only collection checks.
