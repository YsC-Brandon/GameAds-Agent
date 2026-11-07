# Original-Worktree Windows Reproduction Evidence

Historical recorded status: **PASS** for the bounded local synthetic demo workflow installed from the original worktree. This is not candidate-only clone verification, remote CI, Phase 4 acceptance, power-loss, backup, Docker, or production deployment evidence. No full regression or demo reproduction was repeated in this publication attempt.

## Environment

- Windows, Python 3.11, newly created virtual environment with system site packages disabled.
- Installed the project editable with the declared development extra under `requirements-windows-py311.lock`.
- `pip check`: passed, no broken requirements.
- Static validation in that clean venv: Ruff check passed, Python compilation passed, and Ruff format check passed for 113 application/test/script files.
- Full pytest on the current worktree source: **1,339 passed**, zero reported failures/errors/skips, 513.26 seconds. This run is distinct from the older 1,339-pass snapshot noted in project history; it is local evidence, not formal Phase 4 acceptance.
- OpenAPI validation using the same run's JUnit-captured runtime document and request/response samples: **PASS**, 211 examples, 75 schemas, 14 captured samples, zero errors and zero frozen-contract differences.
- Runtime execution ran outside the source tree in a new temporary working directory, but the editable installation and pytest source were the original worktree, not an allowlist-only copy.
- Existing stdout and JUnit were checked in this session: 1,339 tests, zero failures/errors/skips; stdout duration 513.26 seconds and JUnit suite duration 512.657 seconds. JUnit marks 2026-11-07, while this session's supplied date is 2026-11-05. The host-clock discrepancy is preserved, not backdated or represented as a new run.
- Environment removed inherited `GAMEADS_*` values and explicitly set the LLM key and model name to blank. Model calls: **0**. Advertising network calls: **0**.
- Candidate SQLite database and report directory were temporary synthetic-only artifacts and are not included here.

## Workflow Results

| Step | Result | Exit code |
| --- | --- | ---: |
| Initialize a new empty candidate database | PASS | 0 |
| Start candidate task service on loopback and verify health with model unconfigured | PASS | 0 |
| Create synthetic budget-review task and proposal | PASS | 0 |
| Read proposal and approve its expected version/hash with explicit confirmation | PASS | 0 |
| Run local simulation | PASS | 0 |
| Generate deterministic evidence report and retrieve it | PASS | 0 |
| Query completed task before server stop | PASS | 0 |
| Stop and restart service against the same candidate database | PASS | 0 |
| Query completed task after process restart | PASS | 0 |
| Run candidate database integrity check | PASS | 0 |

Observed completed task status: `completed`. The report stated `simulation` mode, `real_execution=false`, `dataset_modified=false`, and used synthetic campaign `C001`. The reproducibility harness asserted that the report includes the proposal and content. Individual task IDs, report IDs, hashes, loopback port, timestamps, machine paths, and raw logs are intentionally omitted.

This verifies process termination/restart and querying the same local candidate state. It is not a power-loss test and does not prove backup/restore, migration, path ACL, durability ordering, or activation/switch safety. R05 remains BLOCKED; Phase 4 remains NOT ACCEPTED.

## Public Tutorial Delta

The authorized publication attempt created a separate 132-file source-only copy
for **collection only**, reusing the existing venv. It selected 1,344 of 1,346 tests,
deselecting exactly the two private-history byte-identity checks; no original
regression/demo was rerun. Seven of those tests are newly added public-validator
tests, not additions to the historical 1,339-pass result. This is not a clean-install,
full-test or public-clone demonstration pass. Runtime-only validation of the old
JUnit capture passed 36 schemas and 14 samples; it is distinct from the older
211-example / 75-schema target-and-runtime check and makes no frozen comparison.

The tutorial uses PowerShell commands equivalent to the recorded lifecycle. Its fresh-demo database path is outside the source tree; initialization runs exactly once, and restart reuses it. Public CI has a separate script because private historical manifests and the frozen contract are excluded. Remote checkouts and CI results are recorded separately in [release status](../../release-status.md); the original-worktree results must not be relabeled as those results. The standalone tutorial demo was not rerun from a public clone.