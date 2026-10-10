# CompanionPorts

Plugins for the KeepTalking Companion.

## Branches

- **`main`** is the scaffold: the manifest schema, the index generator, CI.
  No plugins.
- **`builtin`** is KeepTalking's own instance, the repository the Companion
  reads by default. It replaces the plugins that used to ship inside the
  Companion runtime.

An instance is any branch (or fork) that adds plugins on top of `main` and
merges `main` again to pick up tooling changes. The two never edit the same
paths, so those merges don't conflict:

| Path | Owned by |
|---|---|
| `schema/`, `tools/`, `tests/`, `.github/`, this README | `main` |
| `plugins/<slug>/` | the instance |
| `plugins/index.json` | CI — generated, never edited |

## A plugin

```
plugins/
  index.json              # generated: every plugin, its listing and files
  <slug>/manifest.json    # the plugin's declaration
  <slug>/plugin.py        # the plugin, as in the Companion runtime
```

A plugin folder is a Companion plugin plus `manifest.json`. The manifest
holds everything the plugin declares — name, look, requirements, permissions,
meters, config and kinds — and the plugin loads it rather than repeating it in
code, binding handlers to the kinds by name:

```python
def make_plugin() -> Plugin:
    plugin = Plugin.from_manifest(__file__)

    @plugin.kind("markitdown-convert")
    async def convert(args, ctx): ...

    return plugin
```

So the store and the index never run plugin code.

## The index

On every push to an instance branch, CI validates the plugins, runs
`tools/generate.py` and commits `plugins/index.json` if it changed. Each row
carries what the store shows (name, summary, look, contributors…), the files
with their sha256 and size, and the folder's `sha256`: the sha256 of
`<path>\0<file sha256>\n` for every file, in path order. A client installs a
plugin by fetching each file from `<repository>/plugins/<slug>/<path>`,
checking every file hash and then the folder hash.

A repository URL is the base those paths hang off — for the built-in one,
`https://raw.githubusercontent.com/StevenRCE0/CompanionPorts/builtin/`.

Locally:

```bash
python3 tools/generate.py --validate
```

## Adding a plugin

1. Add `plugins/<slug>/` on the instance branch. Point `$schema` at
   `../../schema/plugin.schema.json` for editor completion.
2. Push. CI regenerates the index.

## Not here

- **Discovery.** Featuring, curating and searching across MCP servers,
  skills and other actions needs state — what people install, rate and report
  — so it belongs to a server, not to static files. This repository only
  lists plugins.
- **Signing.** The index is unsigned; trust is the repository's HTTPS origin.
- **Terms and licences.** Everything is free. Terms will name a plugin's
  `meters` and settle on the SDK's action-call path.
