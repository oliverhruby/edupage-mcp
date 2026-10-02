# Security Policy

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue.
Use GitHub's private reporting for this repository
("Security" → "Report a vulnerability"), which opens a private advisory
visible only to the maintainer.

Please include: the affected tool or endpoint, a description of the impact,
and reproduction steps if you have them. You can expect an acknowledgement
within a few days. There is no bug-bounty programme.

## Scope

This server is a **thin client** for the third-party EduPage portal
(`edupage-api`). It holds your EduPage session cookie and acts on your
behalf; it does not itself store school data.

In scope:

- credential or session-cookie handling in this repository
- the HTTP transport's authentication (`MCP_API_KEY`)
- anything in the published container image or PyPI package
- the read/write behaviour of the MCP tools — in particular a tool doing
  something other than what its description says it does

Out of scope:

- vulnerabilities in [`edupage-api`](https://github.com/EdupageAPI/edupage-api)
  (please report those upstream) or in `mcp`
- EduPage portal vulnerabilities
- weak or reused EduPage passwords, or accounts without 2FA enabled

## Security model

**Credentials are never written to disk by this server.** `EDUPAGE_USERNAME`,
`EDUPAGE_PASSWORD` and `EDUPAGE_SUBDOMAINS` are read from the environment
(they may instead be passed to the `login` tool at runtime). Session cookies
are held in memory for the process lifetime and are scoped to the school's
own domain.

**Tool calls can act on your account.** This is a real account, not a
read-only replica. Ordering a meal, signing one off, sending a message to a
teacher, or switching the active student are all state-changing. Review
`TOOL_ANNOTATIONS` before granting an agent broad access: `readOnlyHint`
distinguishes inspection from mutation, and `destructiveHint` marks the
tools that remove or overwrite state.

**HTTP mode is unauthenticated unless you set `MCP_API_KEY`.** With
`MCP_TRANSPORT=streamable-http` or `sse`, an unset `MCP_API_KEY` means the
endpoint accepts unauthenticated callers, and anyone who can reach it has
your EduPage session. Always set `MCP_API_KEY` when exposing the container
to a network, and prefer `stdio` where the client supports it.

**`EDUPAGE_SUBDOMAINS` is a strict allowlist.** When set, logins to any
other school are refused. Leave it unset only for a single-school account.

**`custom_request` and `download_attachment` can issue arbitrary
requests** to the school domain using your session — that is their purpose.
The former accepts any EduPage path, so a hostile prompt could induce a
request you did not intend. There is deliberately no host allowlist, because
EduPage may serve attachments from a separate host; treat these two tools as
privileged when deciding what an agent may call.

## Test data and CI

The repository is public, so its CI must never see real student data. The
end-to-end suite runs against two approved test schools, asserts only
one-way SHA-256 fingerprints of child identities, and never commits names
or person ids. Do not add real data to tests, logs, issues, or PRs. The
Docker image scans on every build (Trivy) and dependency CVEs are checked by
`pip-audit`; accepted findings live in `.trivyignore` with a removal date and
must not be added to casually.
