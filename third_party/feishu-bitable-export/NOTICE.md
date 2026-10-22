# Upstream reader attribution

The original `scripts/export-bitable.mjs` and MIT `LICENSE` in this directory are
unmodified copies from https://github.com/lewishezhu-rgb/feishu-bitable-export
at commit `d00117fd8f744c09055b62d92415f62ed5999157`.

`browser/reader.mjs` is VizSentry's adaptation of its URL parsing, clientvars and
records endpoints, payload decompression and completeness checks. It replaces
Excel output with bounded, paginated RPC data, persists an isolated browser
profile, refuses unverified full-table imports and preserves every attachment
reference. The private web API may change; real Feishu acceptance is required.
