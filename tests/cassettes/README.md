# Cassettes

Real Messages API exchanges, recorded once and replayed offline in CI.

A cassette maps each request's digest to the response the API gave. Replay fails with
`CassetteMiss` when the gateway would now send anything different: a prompt, a tool, a
schema, a beta, a model id. That makes each cassette a byte-exact regression test of what
the gateway sends, checked against a request the real API accepted.

## `live/ping.json`

`msa llm ping` is a two-turn tool round trip. Its second request replays the first turn's
thinking under `block_binding: error`, so a recorded 200 proves the transcript is
append-only against the real API.

Record it with your own key; nothing else is sent anywhere:

```bash
ANTHROPIC_API_KEY=... uv run msa llm ping --record tests/cassettes/live/ping.json
```

Or dispatch the **record cassettes** workflow (it needs the repository secret
`ANTHROPIC_API_KEY`) and commit the artifact it uploads.

Re-record whenever the replay test reports a miss. Cassettes hold requests and responses
only, never headers, so no credential is ever written; a test enforces that.
