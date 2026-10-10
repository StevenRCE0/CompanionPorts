# CompanionPorts

Recipes for KeepTalking actions — plugins, MCP servers, skills and ACP agents.

A port is a recipe, never the software itself. It says what an action is, where
its upstream lives (pinned by hash) and what the installer has to bring.
Upstream code stays where it is; this repo holds manifests and small files.

## Layout

```
INDEX.json                    # built from the manifests, one row per port
schema/manifest.schema.json   # the manifest format
ports/<kind>/<slug>/
  manifest.json               # the port's single source of truth
  …                           # skill files, patches; a plugin's entry if not fetched
tools/build_index.py          # builds and checks INDEX.json
```

`<kind>` is one of `plugin`, `mcp`, `skill`, `acp` — the KeepTalking action
payloads a port can produce. Built-in capabilities (primitives, filesystem,
semantic retrieval, action creation) aren't ports.

## How a client uses it

1. Fetch `INDEX.json`. It carries what the store shows before install: name,
   summary, look, platforms, contributors, the secrets the port will ask for,
   and the port directory's `sha256`.
2. On install, fetch the port directory and check it against that hash.
3. Fetch each `fetch` entry into the port's workdir, checking its `sha256`.
4. Ask the user for the declared `secrets`; `${secret:<key>}` in the manifest
   is replaced on the node, never written back here.
5. Produce the action: a Companion plugin (venv from `requires`, then
   `postInstall`, then `entry`), an MCP bundle, a skill bundle or an ACP bundle.

Nothing in a port is executed to index it or show it. A plugin's `entry` is to
load its own `manifest.json` and bind handlers by kind name, so the code can't
declare a kind the manifest doesn't. (That's `Plugin.from_manifest`, not in the
Python SDK yet; MarkItDown's fetched entry still declares itself.)

Plugin ports need the Companion and so install on macOS. Other platforms reach
them as actions served by a peer.

## Adding a port

1. Create `ports/<kind>/<slug>/manifest.json` (point `$schema` at
   `../../../schema/manifest.schema.json` for editor completion).
2. Pin every upstream file with `fetch` — `https` URLs at a fixed revision,
   with their `sha256`.
3. Declare every credential under `secrets`; never commit one.
4. Run `tools/build_index.py` and commit `INDEX.json` with the port.

CI validates every manifest against the schema and fails if `INDEX.json` is
stale.

## Not yet

- **Signing.** `INDEX.json` is unsigned; trust is the repo's HTTPS origin for
  now. A repo key signing the index comes next, consensus attesters later.
- **Terms and licences.** Ports are free. Terms will name a plugin's `meters`
  and settle on the SDK's action-call path, not here.
