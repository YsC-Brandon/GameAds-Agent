# Security Policy

## Supported Boundary

GameAds-Agent is a trusted, Windows-local, single-user demonstration using synthetic data. It is not an authenticated service, production deployment, remote approval system, or advertising integration. The approver label is self-reported and is not an identity. The project is released under the [MIT License](LICENSE), copyright (c) 2026 YsC-Brandon.

- Launch HTTP only with `python -m app.server`. It binds to `127.0.0.1`; do not expose it through a proxy, container port, network interface, or public host.
- Keep model key and model name blank for the deterministic demo. Existing optional model routes can make external provider calls when configured.
- Do not put secrets, personal information, real business data, or advertising credentials into requests, reports, labels, issues, or logs.
- Do not commit `.env`, keys, SQLite databases, backups, generated reports, or machine-specific evidence.
- Local peer/Host/Origin checks and cooperative file ownership are not isolation from other programs running as the same user.

## Known Limitations

Phase 4 is **NOT ACCEPTED**. R05 remains **BLOCKED**; the frozen r4 switch protocol is unresolved and candidate activation/switch is blocked. Power-loss durability is **UNPROVEN**. Docker is **NOT VERIFIED**. The earlier 1,339-pass acceptance snapshot does not match final source; the later 1,339-pass original-worktree run is distinct local evidence, not public-clone or remote CI verification. See [current status](docs/release-status.md).

The dependency review is bounded evidence, not a complete current vulnerability assessment or legal clearance. A dependency license inventory does not establish rights to publish project code, data, or assets.

## Reporting a Vulnerability

No public security contact or response SLA has been authorized. Do not publish secrets, personal data, or exploit details in a public issue. Use an already-established private channel to contact the repository owner. Do not assume a response time or that this review candidate is monitored.