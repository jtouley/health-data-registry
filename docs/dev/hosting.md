# Hosting: the read-only connector on a Hugging Face Space

The hosted server is the connector clinicians add in Claude (claude.ai, Claude Projects, the desktop app) by URL,
with no sign-in. It is the same MCP server as `health-data-registry serve` (same tools, same plain-words
refusals), served over Streamable HTTP by `health-data-registry serve --http`, with the hosting rules below in
[`hosted.py`](../../src/health_data_registry/hosted.py) and [`deadline.py`](../../src/health_data_registry/deadline.py).

## How it runs

```text
Claude (Anthropic's servers) --HTTPS 443--> <owner>-<space>.hf.space (Hugging Face proxy)
    --> container: uvicorn on 0.0.0.0:7860 --> /mcp (Streamable HTTP, stateless), /healthz, /
        --> hub releases (hf://datasets/jtouley/health-data-registry@release-<date>), cached on disk
        --> restricted sources (WHO GHO) read live per question, memory only (live.py)
```

- **Transport:** Streamable HTTP at `/mcp`, the MCP Python SDK's (2.3) `stateless_http` mode. No session state
  lives on the server, so a restart or a Space waking from sleep never breaks a client. The 2026-07-28 protocol
  is sessionless in the SDK anyway.
- **Data:** only an immutable release of the open data hub: `HDR_HUB`, default `hf://datasets/jtouley/health-data-registry`,
  resolved once to its newest `release-<date>` tag (or pinned with `@release-<date>`), checked against the
  release manifest's sha256 and contract versions ([hub.py](../../src/health_data_registry/hub.py)). Never a
  local lake. Restricted sources are read live by their rules. The private tier (`HDR_PRIVATE_HF_TOKEN`, a Space
  secret) is read only from the environment; its reader is not on this branch yet (see Open items).
- **Every tool is read-only** (`readOnlyHint`), as on stdio.
- **Warm start:** at start-up a background thread resolves the release, reads its manifest and opens the
  engine, so the first question doesn't wait for that. `/healthz` answers at once with
  `{"status": "ok", "poisoned": false, "poisoned_since": null, "poisoned_for_s": null, "poisoned_reasons": [],
  "restarts_after_s": 900, "warm": true|false, "release": "release-<date>", "query_isolation": {...}}`;
  while the engine is poisoned (see Query isolation) it answers HTTP 503 with `"status": "poisoned"`, the cause
  and since when, so the image's `HEALTHCHECK` reports the Space unhealthy.
- **Cache:** `HDR_CACHE_DIR`; else `/data/health-data-registry` when storage is attached at `/data` and
  writable; else `/tmp/health-data-registry` (lost on restart). Bounded by `HDR_CACHE_MAX_GB` (default 5),
  least recently used files evicted first.

## Limits

| Limit | Setting (default) | Why |
|---|---|---|
| One tool call | `HDR_TOOL_TIMEOUT_S` (200 s), plus a 10 s grace | claude.ai waits 240 s per tool call |
| One page of an answer | `HDR_PAGE_CHARS` (120,000) | keeps a page, both copies counted, well under the cap below |
| One tool result (backstop) | `HDR_MAX_RESULT_CHARS` (140,000) | claude.ai takes about 150,000 characters per result |
| Tool calls per MCP session | `HDR_RATE_PER_SESSION` (30 a minute) | one conversation can't crowd out others |
| Tool calls overall | `HDR_RATE_GLOBAL` (300 a minute) | the free hardware: 2 vCPU, 16 GB RAM |
| Request body | 4 MiB (the SDK's default) | |
| Memory of one query | `HDR_QUERY_MEMORY_MB` (4,096 MiB of address space, at least 1,024) | DuckDB's 2 GB `memory_limit` doesn't count every allocation |
| Reply of one query | `HDR_QUERY_RESULT_MB` (64 MiB) | a child can't fill the server's memory with its reply |
| Poisoned before the server restarts itself | `HDR_POISONED_EXIT_S` (900 s) | only a restart clears work that never ends |

Every limit must be a positive number: `0`, a negative number, `inf`, `nan` or text never means "no limit", and
the server refuses to start with it (unset the variable to use the default). `HDR_PAGE_CHARS` is a whole number of
at least 2,000, and the server also refuses to start when it is above `HDR_MAX_RESULT_CHARS`, since every long
answer would then be dropped instead of paged.

- **Deadline:** one budget per call caps every fetch, retry and hub download inside it. Without it a live WHO
  read could retry 4 times at up to 180 s each (`crawl.RETRIES`, `crawl.FETCH_TIMEOUT_S`), per page. Each
  attempt is shortened to what is left, no retry starts once its back-off would pass the deadline, and the
  clinician gets "This question needed more time than the hosted server allows…" with a next step. If a library
  call ignores the deadline, the guard answers anyway after the grace, and the engine is poisoned until that
  call ends (see Query isolation).
- **Query isolation** ([isolation.py](../../src/health_data_registry/isolation.py), issue #64). Every query,
  `query_sql` and compiled metric SQL alike, runs in a child process forked from a preloaded forkserver: 40 to
  80 ms more per query than in-process (measured on the synthetic lake, Linux; 78 ms on a 4-vCPU container). The child gets only the tables
  its query reads, clears its environment (no Space secret reaches it), sends its stdout and stderr to
  `/dev/null`, attaches the snapshot files read-only, sets `RLIMIT_AS` (`HDR_QUERY_MEMORY_MB`), `RLIMIT_FSIZE` 0
  and `RLIMIT_CORE` 0, opens DuckDB with 2 threads, and sends one JSON reply (never pickle) over a pipe, capped
  at `HDR_QUERY_RESULT_MB` and checked again as the server reads it. `RLIMIT_FSIZE` 0 stops a file growing, not
  being opened, so verified hub files are also made read-only in the cache. At the deadline the child is
  SIGKILLed (through a pidfd on Linux), whatever it is doing: one long `lpad` or `repeat` call that
  `con.interrupt()` can't stop is killed too. A wait that runs out is reported as too slow; a SIGKILL or SIGABRT
  the server didn't send as needing more memory than one question may use; any other death (a crash) as the
  engine stopping; a reply that can't be decoded, or a child that can't be started, in plain words too. The
  server itself is untouched.
- **Poisoned engine.** Anything that outlives its call (a child that SIGKILL didn't reap, a tool body stuck in a
  library call, a hub download still running after its call gave up) poisons the engine until it ends: every
  new call gets "The hosted server is still stopping an earlier question…" at once, before it touches the
  engine, and `/healthz` answers 503. So nothing a timed-out worker does can change shared state after the next
  call starts. The onset is logged once with its cause, and `/healthz` shows the cause and since when. It
  clears itself when the stuck work ends. Work that never ends (a thread stuck for good, a child in the kernel)
  only a restart clears: after `HDR_POISONED_EXIT_S` (900 s) still poisoned the server exits (code 70), so a
  platform that restarts exited containers starts a fresh one. Whether a Hugging Face Space does that on its own
  isn't verified yet; until it is, an operator who sees `poisoned_for_s` growing on `/healthz` restarts the
  Space from its Settings page.
- **Platforms.** Linux (the Space) gets all of the above. macOS doesn't enforce `RLIMIT_AS`, and Windows has no
  `resource` module and spawns each child (slower); there the kill and the reply cap still hold, but memory is
  bounded only by DuckDB's `memory_limit` (`query_isolation.memory_limit_enforced` is false on `/healthz`: each
  child checks the limit took effect and says so, and a warm-start query measures it first). The
  local stdio server keeps running queries in-process: one clinician on their own computer, where a runaway query
  costs only their own session, and where the child would mostly add time without the memory limit.
- **One engine user at a time:** `query.Engine` holds one DuckDB connection and isn't thread safe, so calls
  queue; a call that can't start within its budget gets a plain "busy, try again in a minute".
- **Result size: pages, not refusals.** Measured as the text block plus the SDK's structured copy of it (the SDK
  sends both). Paging is in the tools layer (`paging.py`), so stdio, HTTP and the CLI page alike: an answer over
  `HDR_PAGE_CHARS` comes back as page 1 with real rows, a `Page 1 of N: rows 1–k of T` header, a Next line with
  the exact call for page 2 (same arguments plus `page` and `pin`), the guidance to fetch every page and save
  the rows to a CSV file or artifact, and provenance on every page. `HDR_PAGE_CHARS` must not be above
  `HDR_MAX_RESULT_CHARS` (checked at start). Pages are cut from estimated row sizes, then every page is measured
  and re-cut when one is over, so no page goes over `HDR_PAGE_CHARS`; a single row too long for any page is
  refused with "One row of this answer is too long to send in a reply…". The cap stays as a backstop only.
  `HDR_MAX_RESULT_CHARS` is a whole number of at least 2,000.
- **Consistent pages across releases.** The pin names each source's snapshot id and, from the hub, its release
  tag, then the whole answer's row count and a hash of its row keys, then an HMAC-SHA256 signature
  (`toy_cases:<snapshot>@release-<date>~<rows>~<hash>~<signature>`). The key is the Space secret
  `HDR_SIGNING_KEY`, else a random per-process key (pins then stop working after a restart, and the clinician is
  told to start again from page 1). A pin the server didn't sign is refused, so a forged pin can't point the
  server at another hub commit; a pin may name only a `release-<date>` tag, never a bare commit. A later page
  whose rows differ from page 1's in number or keys is refused, never mixed; a call that runs out of time is
  refused whole, never answered with the sources it managed to read. The hosted server resolves the newest release once per process,
  so a restart can move it to a newer release between two pages; a page whose pin names the older tag is then
  read from that tag (a second engine on `…@release-<date>`, at most two kept, in the same bounded cache). If
  the tag is gone, or a restricted source's in-memory read has expired (those are never kept), the page is
  refused in plain words with "start again from page 1". Page boundaries depend only on the rows and the notes,
  so the same pinned data always cuts into the same pages. A `query_sql` query with its own `ORDER BY` gets
  every output column added after its keys (or `COLUMNS(*)` for a star), so ties never reorder between pages.
- **Download links.** A long answer from a public-domain source read from a hub release also carries
  `https://huggingface.co/datasets/<org>/<name>/resolve/<tag>/<source>/data/<source>.parquet` and the `hf://`
  form, with the file's sha256, licence and citation. Links are built only from the hub URL, the release tag and
  the source id: never from a question or an argument, and never for a source with `redistribution: false`.
- **Rate limits are never per IP address:** every claude.ai user reaches the server from Anthropic's published
  outbound range. The server hands out an opaque `MCP-Session-Id` on `initialize` (the spec allows a server
  to; clients must send it back): a random nonce and its HMAC signature (`HDR_SIGNING_KEY`, else a per-process
  key), and keys the per-session limit on it. Requests without a session id the server signed (the sessionless
  2026-07-28 protocol, or a made-up id) share one bucket at the per-session rate, so a client can't skip the
  limit by leaving the header out or changing it. Both buckets are checked before either is spent, a refused
  call stores nothing, and the bucket map never holds more than 10,000 keys. Over a limit: HTTP 429,
  `Retry-After`, and a JSON-RPC error whose message is plain words.
- **Request bodies** over 1 MiB are refused with HTTP 413 and a plain JSON-RPC error, from `Content-Length`
  when it is sent and otherwise as soon as the bytes read pass the cap; nothing past it is held in memory.
- **The engine is never held past a deadline.** Hub downloads and the release lookup run within what is left of
  the call's budget. A query is stopped by its child's own kill at the deadline. When a call overruns anyway
  (stuck outside a query), the guard interrupts the engine connections, which stops only work in the server's
  own process, takes the engine lock back, answers, and keeps the engine poisoned until the overrunning call ends.
  At most 8 calls queue for the engine; the rest are told the server is busy at once.
- **Host checks.** `serve --http` binds to 127.0.0.1 unless `--host` says otherwise, and the Host and Origin
  headers are checked (the SDK's DNS-rebinding protection) whenever the address isn't the explicitly public
  `0.0.0.0` or `::`. The Space passes `--host 0.0.0.0` behind Hugging Face's proxy.
- **No egress proxy unless it enforces the publisher allowlist.** `HDR_HTTPS_PROXY` sends every fetch through an
  HTTP CONNECT proxy, which looks up the publisher's address itself, so the fetcher's refusal of private or
  loopback addresses becomes advisory (`fetch.py`). `serve --http` therefore refuses to start while
  `HDR_HTTPS_PROXY` is set, with a plain message that names the next step. The opt-in is
  `HDR_PROXY_ENFORCES_ALLOWLIST=1`: set it only when the proxy itself allows nothing but the catalogued publisher
  hosts on public addresses. While `HDR_HTTPS_PROXY` is set, its only accepted value is `1`; any other value
  (`0`, `false`, `yes`) stops the server just as an unset opt-in does, so a typo never turns the check off. With
  no proxy the opt-in is ignored, and the server logs a warning that it has nothing to vouch for, so a stale one
  can be removed before a proxy is added later. With the opt-in, the proxy address must look like
  `http://host:port` with no credentials, or the server doesn't start; it then logs one warning that names the
  proxy's host and port. The opt-in also means `HDR_EXTRA_CA_FILE`, if set, is trusted for every publisher, as it
  is in any proxy mode, so set that file only for a proxy that re-terminates TLS and that you control. Local use
  (`serve` on stdio, `refresh`, `fetch`) is unchanged: it still fetches through the proxy and warns. The Space
  sets no proxy.

## Privacy

From day one, and tested (`tests/test_hosted.py`):

- No request body, tool argument, query string, prompt, session id or IP address is ever logged. uvicorn's
  access log is off and uvicorn doesn't set up logging of its own (`log_config=None`).
- Every library log record (the MCP SDK, uvicorn, httpx, huggingface_hub) is cut to its logger name, level and
  exception type, because a library message or exception can quote its input (a validation error does).
  Records below WARNING are dropped.
- The only log lines with content are aggregate counters, once an hour (`HDR_METRICS_LOG_S`):
  `tool|status|latency bucket: count`, for example `get_series|ok|<1s: 12`.
- No analytics or telemetry: `HF_HUB_DISABLE_TELEMETRY=1` and `DO_NOT_TRACK=1` in the image.
- Hugging Face, which runs the proxy in front of the Space, records visit information such as IP addresses (its
  [privacy policy](https://huggingface.co/privacy)); for claude.ai traffic the address is Anthropic's. The plain
  note for clinicians is [space/PRIVACY.md](../../space/PRIVACY.md).

## Run it locally

```bash
uv run health-data-registry serve --http --host 127.0.0.1 --port 7860
curl -s http://127.0.0.1:7860/healthz
```

Point `HDR_HUB` at a release (`hf://datasets/<org>/<name>@release-<date>`) or a local release directory made by
`export-public`. Until the hub has a tagged release, every data tool answers that the hub has no release yet.

## What the maintainer does

Deploys come from `.github/workflows/deploy-space.yml` on a release tag ([automation.md](automation.md)). Once:

1. Create a Space (Docker SDK, CPU Basic). Hugging Face's docs say creating a Docker Space now needs a paid plan
   (PRO for a personal account), though CPU Basic hardware has no hourly cost.
2. Upload `space/` as the Space repo's root, and nothing else (`Dockerfile`, `README.md`, `PRIVACY.md`; the
   README's front matter sets `sdk: docker` and `app_port: 7860`). The Dockerfile builds only from a release:
   `HDR_TAG` is a `vX.Y.Z` tag that `release.yml` built from a commit on main, and `HDR_COMMIT` is the commit that
   tag names, checked at build time, so a deploy is reproducible and a moved tag fails the build. Both are empty
   in git and a build without them stops. `deploy-space.yml` fills in both from the release it deploys and
   uploads `space/`. To deploy newer code, cut a release; never point them at a branch.
3. Optional: attach a Storage Bucket as a volume at `/data`, so the cache survives restarts.
4. Settings, Variables and secrets: the secret `HDR_SIGNING_KEY` (32 or more random characters, for example
   `python -c "import secrets; print(secrets.token_hex(32))"`), so pins and session ids survive a restart;
   `HDR_HUB` only to pin a release; the secret `HDR_PRIVATE_HF_TOKEN` (read-only) once the private tier ships.
   Never put a token or key in a file.
5. Add `https://jtouley-health-data-registry-mcp.hf.space/mcp` as a custom connector in Claude and run one real
   call. `HDR_PUBLIC_URL` in `pyproject.toml` already names it, so the plugin points at it; if the Space moves,
   change it there and run `make docs`.

Ongoing: publish a new hub release with `export-public`; restart the Space (or pin `HDR_HUB`) to move to it, since a
running server keeps the release it resolved at start. Read the hourly counters in the Space's logs for refusals,
timeouts and rate limits. A free Space sleeps after 48 hours without traffic and wakes on the next request; the
first request after waking waits for the container to start.

## Facts checked, with sources (24 Nov 2026)

- Claude connectors: Streamable HTTP; no authentication is supported by default; about 150,000 characters per
  tool result and 240 s per tool call on claude.ai and Desktop ([building](https://claude.com/docs/connectors/building.md),
  [authentication](https://claude.com/docs/connectors/building/authentication.md)). Claude reaches the server over
  the public internet from Anthropic's outbound range, IPv4 only, and every resolved address must be globally
  routable ([troubleshooting](https://claude.com/docs/connectors/building/troubleshooting.md),
  [IP addresses](https://platform.claude.com/docs/en/api/ip-addresses)).
- MCP sessions: a server MAY assign `MCP-Session-Id` on initialize, clients MUST send it back, and a server MAY
  answer DELETE with 405 ([transports, 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)).
- MCP Python SDK 2.3.0, read from the installed source: `stateless_http` gives each request a fresh transport and
  issues no session id; sync tools run on worker threads (`anyio.to_thread`); results carry text plus
  `structuredContent`.
- Hugging Face: Docker Spaces listen on 7860 unless `app_port` says otherwise; the container runs as uid 1000;
  secrets reach the container as environment variables; disk is lost on restart unless a Storage Bucket is
  attached as a volume; CPU Basic is 2 vCPU, 16 GB RAM, 50 GB ephemeral disk, and sleeps after 48 h idle
  ([Docker Spaces](https://huggingface.co/docs/hub/spaces-sdks-docker),
  [storage](https://huggingface.co/docs/hub/spaces-storage),
  [hardware and sleep](https://huggingface.co/docs/hub/spaces-gpus),
  [overview](https://huggingface.co/docs/hub/spaces-overview),
  [config reference](https://huggingface.co/docs/hub/spaces-config-reference)).

## Open items

- Not documented by Hugging Face: whether its proxy logs request paths or bodies, and its proxy's idle timeout
  for a long tool call.
- Not documented by Anthropic: whether the ~150,000-character limit counts the text block, the structured copy
  or both; the cap and the page budget assume both.
- `license` in the Space front matter is the Hub's general card field; the Spaces config reference doesn't list it.
- The private-tier reader (`private.py`) is on another branch; when it lands, the hosted server must read its
  hub copies under the cache, never a local lake.
