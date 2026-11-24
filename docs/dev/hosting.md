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
  `{"status": "ok", "warm": true|false, "release": "release-<date>"}`.
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

- **Deadline:** one budget per call caps every fetch, retry and hub download inside it. Without it a live WHO
  read could retry 4 times at up to 180 s each (`crawl.RETRIES`, `crawl.FETCH_TIMEOUT_S`), per page. Each
  attempt is shortened to what is left, no retry starts once its back-off would pass the deadline, and the
  clinician gets "This question needed more time than the hosted server allows…" with a next step. If a library
  call ignores the deadline, the guard answers anyway after the grace and interrupts DuckDB.
- **One engine user at a time:** `query.Engine` holds one DuckDB connection and isn't thread safe, so calls
  queue; a call that can't start within its budget gets a plain "busy, try again in a minute".
- **Result size: pages, not refusals.** Measured as the text block plus the SDK's structured copy of it (the SDK
  sends both). Paging is in the tools layer (`paging.py`), so stdio, HTTP and the CLI page alike: an answer over
  `HDR_PAGE_CHARS` comes back as page 1 with real rows, a `Page 1 of N: rows 1–k of T` header, a Next line with
  the exact call for page 2 (same arguments plus `page` and `pin`), the guidance to fetch every page and save
  the rows to a CSV file or artifact, and provenance on every page. Keep `HDR_PAGE_CHARS` below
  `HDR_MAX_RESULT_CHARS`. The cap stays as a backstop only (a single row too long for any page): such a result
  is replaced by "This answer is too long to send… even one page at a time…".
- **Consistent pages across releases.** The pin names each source's snapshot id and, from the hub, its release
  tag (`toy_cases:<snapshot>@release-<date>`). The hosted server resolves the newest release once per process,
  so a restart can move it to a newer release between two pages; a page whose pin names the older tag is then
  read from that tag (a second engine on `…@release-<date>`, at most two kept, in the same bounded cache). If
  the tag is gone, or a restricted source's in-memory read has expired (those are never kept), the page is
  refused in plain words with "start again from page 1". Page boundaries depend only on the rows and the notes,
  so the same pinned data always cuts into the same pages.
- **Download links.** A long answer from a public-domain source read from a hub release also carries
  `https://huggingface.co/datasets/<org>/<name>/resolve/<tag>/<source>/data/<source>.parquet` and the `hf://`
  form, with the file's sha256, licence and citation. Links are built only from the hub URL, the release tag and
  the source id: never from a question or an argument, and never for a source with `redistribution: false`.
- **Rate limits are never per IP address:** every claude.ai user reaches the server from Anthropic's published
  outbound range. The server hands out an opaque random `MCP-Session-Id` on `initialize` (the spec allows a
  server to; clients must send it back) and keys the per-session limit on it; requests without one (the
  sessionless 2026-07-28 protocol) count only against the overall limit. Over a limit: HTTP 429, `Retry-After`,
  and a JSON-RPC error whose message is plain words.

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

Nothing here deploys; the deployment is a separate task. Once:

1. Create a Space (Docker SDK, CPU Basic). Hugging Face's docs say creating a Docker Space now needs a paid plan
   (PRO for a personal account), though CPU Basic hardware has no hourly cost.
2. Put the repository at a tagged commit in the Space repo with `space/Dockerfile` as `Dockerfile`, and
   `space/README.md` and `space/PRIVACY.md` at the root (the Space README's front matter sets `sdk: docker` and
   `app_port: 7860`).
3. Optional: attach a Storage Bucket as a volume at `/data`, so the cache survives restarts.
4. Settings, Variables and secrets: `HDR_HUB` only to pin a release; the secret `HDR_PRIVATE_HF_TOKEN`
   (read-only) once the private tier ships. Never put a token in a file.
5. Add `https://<owner>-<space>.hf.space/mcp` as a custom connector in Claude and run one real call. Then set
   `HDR_PUBLIC_URL` in `pyproject.toml` and run `make docs`, so the plugin points at it.

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
