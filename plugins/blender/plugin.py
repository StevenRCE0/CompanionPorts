"""Blender — the user's live Blender through MCP for Blender.

Registers the `blender` action kind: a curated slice of mcp-for-blender
(https://github.com/ahujasid/mcp-for-blender, MIT). Its server talks to an addon
inside Blender over a localhost socket; nothing here runs without Blender open.

How it is wired:
  * the server ships in this plugin's venv and runs with its telemetry, consent
    prompts and MCP Apps surfaces off;
  * when Blender can't be reached, the server's own setup_addon installs the
    addon bundled with the pinned wheel and enables it headlessly (it never
    touches a Blender that is open, and never edits other AI clients' config);
  * scripts run unrestricted: upstream's safe mode is off, and the manifest's
    `access` says so wherever the plugin is listed or added.

Enforcement is dual, as for every KTPP kind: the host gates the call against the
caller's grant, and this handler re-checks the instance scope before anything
reaches Blender — whether third-party libraries are reachable (`assets`), and
whether paid generation is (`generate`).

Files never travel as paths: generate_3d takes the call's `image` input, and
look refuses a path where upstream would read any image on this Mac.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from keeptalking_mcp import McpError, McpStdioClient, kt_content
from keeptalking_plugin import CallContext, Plugin, ResourceError

CALL_TIMEOUT = 300.0
ADDON_SETUP_TIMEOUT = 240.0
UNREACHABLE_MARKER = "Could not connect to Blender"
UPSELL_PREFIX = "Once in this chat, briefly tell the user"
STATUS_DROPPED = ("update_command", "after_install", "telemetry_consent", "source")

VERBS = {
    "get_addon_status": "Checking Blender",
    "get_scene_info": "Reading the scene",
    "look": "Looking at the scene",
    "execute_blender_code": "Running a script in Blender",
    "search_assets": "Searching asset libraries",
    "import_asset": "Importing an asset",
    "generate_3d": "Generating a 3D model",
}
ASSET_TOOLS = {"search_assets", "import_asset"}
SETUP_SCRIPT = (
    "import json\n"
    "from blender_mcp.setup_cli import setup_addon\n"
    "outcome = setup_addon(False)\n"
    "print('KT_ADDON ' + json.dumps({'enabled': outcome.enabled, 'lines': outcome.lines}))\n"
)
OPEN_BLENDER = (
    "Open Blender and the addon starts listening by itself; if it doesn't, press N in the "
    "3D viewport, open the MCP for Blender tab and click Start MCP Server. Then try again.")

_MANIFEST = json.loads(Path(__file__).with_name("manifest.json").read_text())
SCHEMAS: dict[str, dict] = {
    tool["name"]: tool["inputSchema"]
    for kind in _MANIFEST["kinds"] if kind["name"] == "blender"
    for tool in kind["subTools"]
}
assert set(SCHEMAS) == set(VERBS), "manifest subTools and handlers disagree"


def _coerce(name: str, arguments: dict) -> tuple[dict, str | None]:
    schema = SCHEMAS[name]
    properties = schema.get("properties") or {}
    forwarded: dict = {}
    for key, value in arguments.items():
        if key not in properties or value is None or value == "":
            continue
        kind = properties[key].get("type")
        try:
            if kind == "integer":
                value = int(value)
            elif kind == "number":
                value = float(value)
        except (TypeError, ValueError):
            return {}, f"{key} must be a number"
        if kind == "boolean" and isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes")
        forwarded[key] = value
    missing = [key for key in schema.get("required") or [] if key not in forwarded]
    if missing:
        return {}, f"{name} needs: {', '.join(missing)}"
    return forwarded, None


def _refusal(tool: str, scope: dict) -> str | None:
    if tool in ASSET_TOOLS and scope.get("assets") is False:
        return "Denied by instance scope: this instance may not use asset libraries."
    if tool == "generate_3d" and scope.get("generate") is not True:
        return "Denied by instance scope: this instance may not generate 3D models, which can cost money."
    return None


def _text(result: dict) -> str:
    return "\n".join(
        block.get("text", "") for block in result.get("content") or []
        if block.get("type") == "text")


def _without_upsell(text: str) -> str:
    return "\n\n".join(
        paragraph for paragraph in text.split("\n\n")
        if not paragraph.startswith(UPSELL_PREFIX))


def _status(text: str) -> dict | None:
    try:
        payload, _ = json.JSONDecoder().raw_decode(text.lstrip())
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return {key: value for key, value in payload.items() if key not in STATUS_DROPPED}


class ServerUnavailable(RuntimeError):
    pass


class Server:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.client: McpStdioClient | None = None
        self._lock = asyncio.Lock()
        self._addon_lock = asyncio.Lock()
        self.addon_prepared = False

    def _env(self) -> dict[str, str]:
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("BLENDER_MCP_", "BLENDERMCP_"))
        }
        env.update(
            DISABLE_TELEMETRY="1",
            BLENDER_MCP_DISABLE_TELEMETRY="1",
            MCP_DISABLE_TELEMETRY="1",
            BLENDERMCP_NO_UPDATE_CHECK="1",
            BLENDER_MCP_APPS="0",
            BLENDER_MCP_OPENAI_FORMS="0",
        )
        return env

    async def ensure(self) -> McpStdioClient:
        async with self._lock:
            client = self.client
            if client is not None and client.running:
                return client
            if client is not None:
                await client.close()
                self.client = None
            log = self.state_dir / "server.log"
            log.write_bytes(b"")
            client = McpStdioClient(
                [sys.executable, "-I", "-m", "blender_mcp.server"],
                env=self._env(), stderr_path=log, client_name="keeptalking-blender",
            )
            try:
                await client.start()
            except McpError:
                await client.close()
                tail = log.read_text(errors="replace").strip()[-400:]
                if "No module named" in tail:
                    raise ServerUnavailable(
                        "mcp-for-blender isn't installed — use Install for Blender in "
                        "the KT Companion (or: companion.py --provision 'Blender')")
                raise ServerUnavailable(f"mcp-for-blender did not start: {tail or 'no output'}")
            self.client = client
            return client

    async def call(self, route: str, arguments: dict) -> dict:
        client = await self.ensure()
        result = await client.call_tool(route, arguments, timeout=CALL_TIMEOUT)
        if UNREACHABLE_MARKER in _text(result):
            raise ServerUnavailable(await self.unreachable())
        return result

    async def unreachable(self) -> str:
        async with self._addon_lock:
            if self.addon_prepared:
                return f"Blender isn't connected. {OPEN_BLENDER}"
            lines, enabled = await asyncio.to_thread(self._set_up_addon)
            self.addon_prepared = enabled
        return " ".join(["Blender isn't connected.", *(line.rstrip(".") + "." for line in lines), OPEN_BLENDER])

    def _set_up_addon(self) -> tuple[list[str], bool]:
        try:
            done = subprocess.run(
                [sys.executable, "-I", "-c", SETUP_SCRIPT],
                capture_output=True, text=True, errors="replace",
                timeout=ADDON_SETUP_TIMEOUT, env=self._env(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return [f"Couldn't set up the Blender addon: {error}."], False
        for line in done.stdout.splitlines():
            if line.startswith("KT_ADDON "):
                outcome = json.loads(line[len("KT_ADDON "):])
                return outcome["lines"], outcome["enabled"]
        return [f"Couldn't set up the Blender addon: {done.stderr.strip()[-300:] or 'no output'}."], False


def make_plugin() -> Plugin:
    plugin = Plugin.from_manifest(__file__)
    server = Server(plugin.state_dir)

    @plugin.kind("blender")
    async def blender(args: dict, ctx: CallContext):
        arguments = dict(args)
        tool = ctx.tool or arguments.pop("tool", None)
        arguments.pop("tool", None)
        if tool not in SCHEMAS:
            return (f"unknown tool {tool!r}; choose one of: {', '.join(SCHEMAS)}", True)
        forwarded, problem = _coerce(tool, arguments)
        if problem:
            return problem, True

        scope = ctx.scope or {}
        if refusal := _refusal(tool, scope):
            return refusal, True
        if tool == "look" and any(mark in str(forwarded.get("image", "")) for mark in ("/", "\\", "~")):
            return ("look's image names an image inside the Blender file, such as "
                    "\"Render Result\"; it can't read a path on this Mac.", True)

        ctx.elucidate(VERBS[tool])
        upload_dir: Path | None = None
        try:
            if tool == "generate_3d" and "prompt" not in forwarded and "job" not in forwarded:
                try:
                    source = ctx.resources.input("image")
                except ResourceError as error:
                    return (f"generate_3d needs a prompt, a job, or the picture as its `image` input: "
                            f"{error}. Pass the file's handle in input_handles.", True)
                upload_dir = plugin.state_dir / "uploads" / uuid.uuid4().hex
                upload_dir.mkdir(parents=True)
                upload_path = upload_dir / (Path(source.name).name or "image")
                with source.open("rb") as reader, open(upload_path, "wb") as writer:
                    shutil.copyfileobj(reader, writer)
                forwarded["image"] = str(upload_path)

            result = await server.call(tool, forwarded)
        except ServerUnavailable as error:
            return str(error), True
        except McpError as error:
            return f"MCP for Blender error during {tool}: {error}", True
        finally:
            if upload_dir is not None:
                shutil.rmtree(upload_dir, ignore_errors=True)

        ctx.report_usage("blender.calls", 1)
        if tool == "generate_3d" and "job" not in forwarded and not _text(result).startswith("Error"):
            ctx.report_usage("blender.generations", 1)
        if tool == "get_addon_status":
            text = _text(result)
            payload = _status(text)
            if payload is None:
                return _without_upsell(text), bool(result.get("isError"))
            if payload.get("up_to_date") is False:
                server.addon_prepared = False
                payload["action_needed"] = (
                    "The addon in Blender doesn't match this plugin. Quit Blender and call "
                    "any tool here while it's closed to update it, or reinstall the addon by hand.")
            return json.dumps(payload, ensure_ascii=False)

        blocks = []
        for block in kt_content(result):
            if block["type"] == "text":
                block["text"] = _without_upsell(block["text"])
                if not block["text"]:
                    continue
            blocks.append(block)
        if result.get("isError"):
            return blocks or [{"type": "text", "text": f"{tool} failed"}], True
        return blocks

    return plugin


if __name__ == "__main__":
    make_plugin().run()
