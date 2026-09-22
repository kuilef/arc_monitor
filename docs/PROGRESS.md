# Implementation ledger

- Repository read: empty. Latest confirmed write attempt: HTTP 403, Resource not accessible by integration.
- Source brief available in full. No previous source code was found in the mounted workspace;
  only a dashboard screenshot existed, which is not evidence of a working implementation.
- Ruling: SQLite default for a zero-setup demo; PostgreSQL optional. See DESIGN.md.
- Ruling: all volume denominated in USDC for verified USDC pairs; USD is not silently assumed.
- Ruling: current-day market facts are verified against primary API documentation, not inferred
  from the pasted report. Vendor credentials were not provided.

## Completed implementation and evidence
- Event-store TDD: missing implementation -> 13 passing tests.
- Analytics TDD: missing engine -> 32 passing tests including storage.
- Providers: initial missing modules; fixed GraphQL construction at the brace-serialization root cause -> 58 passing.
- Runnable application: missing modules -> 67 passing; deterministic seed = 1123 events / 590 trades / 7 tokens.
- Review fixes: funder groups now drive buyer components; extreme decimals rejected; RPC response IDs checked;
  future coverage rejected; common-funder concentration contributes a disclosed heuristic risk penalty.
- Final suite: 80 passing tests. See artifacts/final-tests.txt and coverage.json for the actual result.
- Browser: managed Chromium blocked direct localhost navigation (ERR_BLOCKED_BY_ADMINISTRATOR).
  Ruling: separately exercise the real HTTP server, then run offline Chromium using a bridge to that real API;
  label this explicitly, do not claim ordinary browser-network end-to-end validation.
- Wheel built from package sources. Initial isolated-venv import lacked dependencies because nesting venvs does
  not inherit the parent venv's site-packages. Ruling: reuse preinstalled dependencies via a .pth, verify the
  actual arc_monitor import is from the installed wheel. No fresh dependency-download claim.
- DNS prevented installing extra lint/parser packages. No passing Ruff or authenticated live-API claim.
- GitHub push remains unavailable to the integration (actual create_file returned 403); preserve the full
  source ZIP and local Git bundle instead of representing a local commit as a remote one.
