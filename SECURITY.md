# Security policy

**Matryca Plumber** (developed by Marco Porcellato · [Matryca.ai](https://matryca.ai)) is a **local-first autonomous background AI daemon** (with an optional MCP sidecar for tool hosts) that can **read and write Markdown files** under your Logseq graph path and uses **credentials** (for example `LOGSEQ_API_TOKEN` and any LLM provider keys in your environment or host). Treating those surfaces carefully is a top priority for the maintainers.

## Please do not file public issues for vulnerabilities

**Do not** open a regular GitHub issue to report a security vulnerability. Public issues disclose the problem before a fix is available and put all users at risk.

## How to report a vulnerability privately

1. **Preferred:** Use [GitHub Security Advisories](https://github.com/MarcoPorcellato/matryca-plumber/security/advisories/new) for this repository and submit a **private** security advisory with reproduction steps, impact, and versions affected.  
2. If you cannot use GitHub advisories, contact the repository maintainers through a **private** channel (for example email) and include the same detail you would put in an advisory. Do not paste secrets or real tokens in the report; use redacted examples.

We aim to acknowledge valid reports in a reasonable timeframe and coordinate disclosure after a fix or mitigation is available.

## Scope we care about

Examples include unauthorized file access outside the intended graph, credential leakage via logs or error messages, unsafe defaults that enable destructive writes without user intent, and protocol or transport issues in the daemon, HTTP surfaces, or optional MCP sidecar that could harm the host machine.

## Operator hardening (built-in)

Matryca Plumber ships several **defense-in-depth** controls you should configure via `.env` (see `.env.example`):

| Control | Variable | Default | Purpose |
|---------|----------|---------|---------|
| MCP stdio gate | `MATRYCA_MCP_ENABLED` | `false` | Prevents accidental FastMCP startup unless you explicitly enable Claude Desktop / Cursor integration. |
| UI bearer token | `MATRYCA_UI_TOKEN` | auto per process | Set a long random value on any shared or multi-user host; loopback clients can read an auto token via `/api/auth/session`. |
| Require explicit UI token | `MATRYCA_UI_REQUIRE_EXPLICIT_TOKEN` | `false` | When `true`, refuse UI startup unless `MATRYCA_UI_TOKEN` is set. |
| LAN UI bind | `MATRYCA_UI_ALLOW_LAN` | `false` | Refuses `0.0.0.0` unless set; `/api/auth/session` stays loopback-only even when LAN is on. |
| LLM SSRF guard | `LLM_BASE_URL` / `MATRYCA_LM_BASE_URL` | localhost | Shared validation in UI and daemon blocks cloud metadata and arbitrary egress URLs. |
| Graph path allowlist | `MATRYCA_ALLOWED_GRAPH_ROOTS` | (optional) | Settings UI cannot point `LOGSEQ_GRAPH_PATH` outside home, repo, temp, or declared roots. |
| UI rate limits | `MATRYCA_UI_RATE_LIMIT_*` | 120 / 30 | Separate budgets for authenticated vs probing traffic on `/api/*`. |
| Log redaction | `MATRYCA_PLUMBER_LOG_REDACT_SECRETS` (alias `MATRYCA_LOG_REDACT_SECRETS`) | `true` | Masks Bearer tokens, `sk-*` keys, and JWT-shaped material in ops logs. |
| MCP error sanitization | `MATRYCA_DEBUG` | `false` | When off, MCP tools do not return raw OS paths in error strings. |
| CPU sandbox | `MATRYCA_CPU_SANDBOX` / `MATRYCA_PLUMBER_NICE_LEVEL` | `true` / `19` | Low-priority scheduling and optional affinity (`[edge]` extra for `psutil`). |

**Recommended local setup:** loopback UI only, set `MATRYCA_UI_TOKEN` on shared machines (or `MATRYCA_UI_REQUIRE_EXPLICIT_TOKEN=true`), enable `MATRYCA_MCP_ENABLED=true` only on machines where you trust the MCP host (Cursor/Claude Desktop — full graph read/write, no stdio authentication), test graphs via `LOGSEQ_GRAPH_PATH` clones, and never commit `.env`.

### MCP trust boundary

The optional FastMCP stdio sidecar (`matryca-plumber` with no CLI arguments when `MATRYCA_MCP_ENABLED=true`) exposes the same mutation tools as the daemon. The MCP **host process** is the security boundary: treat it like shell access to your graph directory. Do not enable MCP when the host may run untrusted extensions or shared automation.

## Non-goals

Misconfiguration on your machine (for example sharing your `.env`, running untrusted optional MCP hosts, or exposing the daemon to untrusted networks) is outside the scope of this policy, though documentation improvements to reduce that risk are welcome as regular contributions.
