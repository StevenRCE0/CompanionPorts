# CompanionPorts

Where KeepTalking finds actions to install: Companion plugins, MCP servers,
skills and ACP agents.

## Branches

- **`main`** is the scaffold: schemas, the index generator, CI. No content.
- **`builtin`** is KeepTalking's own instance, the one the Companion reads by
  default. It replaces the plugins that used to ship inside the Companion
  runtime.

An instance is any branch (or fork) that adds content on top of `main` and
merges `main` again to pick up tooling changes. The two never edit the same
paths, so those merges don't conflict:

| Path | Owned by |
|---|---|
| `schema/`, `tools/`, `tests/`, `.github/`, this README | `main` |
| `plugins/<slug>/`, `discovery/<slug>/`, `curation.json` | the instance |
| `plugins/index.json`, `discovery/index.json` | CI — generated, never edited |

## Layout of an instance

```
discovery/
  index.json              # every action the store shows, of any type
  <slug>/action.json      # an MCP server, skill or ACP agent (+ its files)
plugins/
  index.json              # what to fetch to install each plugin
  <slug>/manifest.json    # the plugin's declaration
  <slug>/plugin.py        # the plugin, as in the Companion runtime
curation.json             # featured actions and collections
```

**Plugins.** A plugin folder is a Companion plugin as it is today, plus
`manifest.json`. The manifest holds everything the plugin declares — name,
look, requirements, permissions, meters, config and kinds — and the plugin
loads it rather than repeating it in code, binding handlers to the kinds by
name. So the store and the indexes never run plugin code.

**Discovery.** `discovery/index.json` lists every plugin and every
`discovery/<slug>/` action, whatever its type. An action folder holds
`action.json` and anything the action needs, such as a skill's files.
Credentials are declared under `secrets` and referenced as `${secret:<key>}`;
the installing node fills them in, and they never enter the repo.

**Curation.** Everything listed shows. `curation.json` only features
actions and groups them into collections, by slug.

## Indexes

On every push to an instance branch, CI validates the content, runs
`tools/generate.py` and commits the indexes if they changed. Each row carries
the folder's `sha256`, which the installer checks after fetching it.

Locally:

```bash
python3 tools/generate.py --validate
```

## Adding an action

1. Add `plugins/<slug>/` or `discovery/<slug>/` on the instance branch. Point
   `$schema` at `../../schema/plugin.schema.json` or
   `../../schema/action.schema.json` for editor completion.
2. Feature it in `curation.json` if you want it to stand out.
3. Push. CI regenerates the indexes.

## Not yet

- **Signing.** Indexes are unsigned; trust is the repo's HTTPS origin.
- **Terms and licences.** Everything is free. Terms will name a plugin's
  `meters` and settle on the SDK's action-call path.
