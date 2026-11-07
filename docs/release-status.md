# GitHub Publication Execution Status

Public release execution was authorized by the owner on **2026-11-05**, including a public `GameAds-Agent` repository, MIT licensing of original code with confirmed rights, commit/push, and Windows/Python 3.11 remote CI. The owner confirmed the project copyright holder as `YsC-Brandon`, copyright year 2026, and confirmed rights to publish the original code, documentation, generator and synthetic CSVs under MIT.

## Engineering Status

- Phase 4: **NOT ACCEPTED**.
- R05: **BLOCKED**; the frozen r4 switch protocol remains unresolved and candidate activation/switch is blocked.
- Power-loss durability: **UNPROVEN**.
- Docker: **NOT VERIFIED**.
- Historical full regression: 1,339 passed, but its tested source snapshot does not match the final source (`source_matches_tested_snapshot=false`). The distinct 45-test focused run is not a final full-suite result.
- Previous original-worktree candidate validation: 1,339 passed in 513.26 seconds; Ruff check/format and compilation passed; OpenAPI target/runtime passed with 211 examples, 75 schemas, 14 samples, and no errors or differences. Existing stdout, JUnit and OpenAPI output were reread, not rerun. JUnit suite duration is 512.657 seconds. Its observed date is 2026-11-07, differing from the supplied session date 2026-11-05; it is not backdated. This is local evidence, not Phase 4 acceptance or a public-clone test.
- Support promise: Windows-local, trusted single-user, synthetic-data demonstration only.

## Rights and Review Boundaries

- MIT is the owner's authorized license choice for the confirmed original project material. `LICENSE` states `Copyright (c) 2026 YsC-Brandon`; package metadata names the same author. The Git author is configured at repository scope as `YsC-Brandon` with the account's GitHub noreply address.

The two CSV copies were compared in memory to `generate_records()` and match its 240 deterministic synthetic rows. This establishes reproducible content, not redistribution authority. The task brief, both SVGs, internal contracts, historical evidence and all other unlisted assets are excluded. Original code and generated data remain rights-conditional and will not be uploaded before identification is resolved.

The [dependency inventory](dependency-licenses.md) records all 68 pinned distributions from existing installed metadata and confirms version equality. It is not a current vulnerability rescan, legal clearance or native-component inventory. No dependencies, wheels, binaries or third-party assets are vendored; no blanket MIT or fabricated root NOTICE is applied to them.

## Evidence

The prior original-worktree reproduction result is recorded in [reproduction-report.md](generated/public-review/reproduction-report.md). It used a new venv but not an allowlist-only source install. Neither that demo nor the 1,339-test regression was repeated. Only sanitized conclusions belong in the reviewed scope; raw logs, paths, temporary databases and internal archives are excluded.

The current allowlist and review disposition are in [public-file-manifest.md](generated/public-review/public-file-manifest.md) and [security-review.md](generated/public-review/security-review.md). Files marked conditional or excluded must not be added to a public repository until their corresponding review/authorization is complete.

## Publication Outcome

| Deliverable | Actual result |
| --- | --- |
| Public repository URL | https://github.com/YsC-Brandon/GameAds-Agent |
| GitHub CLI | Official portable v2.103.3 downloaded outside the repository; SHA-256 matched the upstream checksum manifest |
| Authentication/permissions | `gh auth status` passed for `YsC-Brandon`; repository admin/push permissions verified |
| Repository name collision | Checked before creation; no existing repository overwritten |
| Initial commit / push | Pending final staged-byte review and commit |
| Local Git index | Exactly 133 reviewed text/empty-placeholder files staged; all unlisted/private files excluded |
| Project license | MIT; see `LICENSE` and package metadata |
| Remote CI | NOT RUN; no run URL or success claim |
| Public CI configuration | Windows, Python 3.11, `main` pushes, pull requests and manual dispatch; read-only permissions, pinned action commits, no persisted checkout credential |
| New validator checks | Seven new focused tests passed; existing JUnit runtime data validates 36 schemas / 14 samples; not a fresh application run |
| Candidate-only collection | 133-file copy, with private files absent: 1,344 / 1,346 tests collected, exactly two private-history tests deselected; no original test bodies run and no clean reinstall claimed |
| Static checks | Existing environment pip check, Ruff lint/format, Python parsing, workflow/Issue YAML, PowerShell parsing and candidate-relative Markdown links passed |

Only the two existing private-history byte-identity tests are deselected by
`scripts/check_public.ps1`; all other collected tests remain selected. Runtime
OpenAPI validation is a separate public check and explicitly reports frozen
comparison NOT RUN. `scripts/check_local.ps1`, `scripts/verify_openapi.py`, all
existing tests and both frozen contract files remain unchanged locally and their
internal acceptance standards are not weakened. Public CI is not formal acceptance.

## Concrete Unblock Requirements

1. Complete final staged-byte and allowlist review before the first commit; any changed bytes require renewed review.
2. Push the reviewed initial commit to `main` and verify the remote workflow result.

No application behavior, dependency versions, Phase 4 contract or acceptance standard
was changed for publication. No repository, release, upload, or remote run is implied
by a configured workflow or locally reviewed file list.