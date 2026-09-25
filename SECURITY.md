# Security Policy

## Supported Versions

Pre-1.0: only the latest published release (and `main`) receive security
fixes. There is no long-term support branch yet.

| Version | Supported |
| ------- | --------- |
| 0.1.x   | ✅        |

## Reporting a Vulnerability

Please **do not** open a public issue for a suspected vulnerability.

Use [GitHub's private vulnerability reporting](https://github.com/rkuma238/skillward/security/advisories/new)
("Security" tab → "Report a vulnerability") so the report and any discussion
stay private until a fix is available. If that's not accessible to you,
open a regular issue asking a maintainer to open a private channel — don't
include exploit details in it.

Please include:
- The component affected (`registry_server`, `skillward`, `envoy/`,
  `partner_service`/`labs_service`, `dashboard`, or the protocol spec
  itself).
- Whether the issue is in the reference implementation (a code bug) or the
  protocol design itself (e.g. a gap in the auth/authorization model
  described in `spec/SPEC.md`) — these get triaged differently.
- Steps to reproduce, and the impact as you understand it.

We aim to acknowledge reports within 5 business days.

## Scope notes specific to this project

Some things are **known, documented limitations**, not vulnerabilities to
report — see `spec/SPEC.md`'s "Sandboxing" section and this repo's README
"Security model" section before filing:

- `SubprocessSandboxRunner` (the default execution backend) is explicitly
  documented as process isolation, not a hard security boundary against a
  deliberately malicious skill. Reports proposing a stronger `SandboxRunner`
  (container- or Wasm-based) are very welcome as contributions, not just
  reports.
- The reference registry's account/API-key store has no rate limiting or
  key rotation by design (it's a reference implementation, not a hardened
  auth service).

Reports about the actual security boundary that *is* claimed — checksum
verification, capability enforcement, the visibility/ACL check in
`skillward_common/authz.py`, or the gateway's `ext_authz` enforcement — are all
in scope and taken seriously.
