"""Apple Mail — the user's mail in Apple Mail, read only, through apple-mail-mcp.

Registers the `apple-mail` action kind: a read-only slice of apple-mail-mcp
(https://github.com/patrickfreyer/apple-mail-mcp, MIT), which drives Mail.app
through AppleScript. Mail's Automation grant attaches to KT Companion, whose
process chain runs this plugin; nothing reads ~/Library/Mail, so no Full Disk
Access is involved.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import json
import os
import re
import sys
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path

from keeptalking_mcp import McpError, McpStdioClient, kt_content
from keeptalking_plugin import (
    CallContext,
    Plugin,
    ResourceError,
    ScopeOptionsRequest,
    scope_option,
)

CALL_TIMEOUT = 150.0
SOURCE_CAP = 64 * 1024 * 1024
TRUNCATION_MARKER = "[... truncated: original size"
DEFAULT_ROWS = 20
MAX_ROWS = 200
DEFAULT_BODY_CHARS = 20000

FORWARDED = {
    "list_accounts": "Listing Mail accounts",
    "list_mailboxes": "Listing mailboxes",
    "get_mailbox_unread_counts": "Counting unread mail",
    "list_inbox_emails": "Listing the inbox",
    "search_emails": "Searching mail",
    "get_email_thread": "Reading a conversation",
}
COMPOSED = {
    "read_email": "Reading a message",
    "get_email_attachment": "Saving an attachment",
}
JSON_ROWS = {"list_inbox_emails", "search_emails"}

_MANIFEST = json.loads(Path(__file__).with_name("manifest.json").read_text())
SCHEMAS: dict[str, dict] = {
    tool["name"]: tool["inputSchema"]
    for kind in _MANIFEST["kinds"] if kind["name"] == "apple-mail"
    for tool in kind["subTools"]
}
assert set(SCHEMAS) == set(FORWARDED) | set(COMPOSED), "manifest subTools and handlers disagree"


def _allowed_accounts(ctx: CallContext) -> list[str]:
    return [str(a).strip() for a in (ctx.scope or {}).get("accounts") or [] if str(a).strip()]


def _coerce(name: str, arguments: dict) -> tuple[dict, str | None]:
    schema = SCHEMAS[name]
    properties = schema.get("properties") or {}
    forwarded: dict = {}
    for key, value in arguments.items():
        if key not in properties or value is None or value == "":
            continue
        kind = properties[key].get("type")
        if kind == "integer":
            try:
                value = int(value)
            except (TypeError, ValueError):
                return {}, f"{key} must be an integer"
        elif kind == "boolean" and isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes")
        elif kind == "string":
            value = str(value).strip()
        forwarded[key] = value
    missing = [key for key in schema.get("required") or [] if forwarded.get(key) in (None, "")]
    if missing:
        return {}, f"{name} needs: {', '.join(missing)}"
    return forwarded, None


class ServerUnavailable(RuntimeError):
    pass


NOT_AUTHORIZED = (
    "KT Companion isn't allowed to control Mail. Allow it in System Settings → "
    "Privacy & Security → Automation → KeepTalkingCompanion → Mail, then try again.")
NO_ANSWER = (
    "Mail didn't answer in time. If macOS is asking whether KT Companion may "
    "control Mail, answer it; otherwise check Mail is responding, then try again.")


def _problem(text: str) -> str | None:
    lowered = text.lower()
    if "-1743" in text or "not authorized to send apple events" in lowered or "not allowed to send" in lowered:
        return NOT_AUTHORIZED
    if "-1712" in text or "timed out" in lowered:
        return NO_ANSWER
    return None


def _text(result: dict) -> str:
    return "\n".join(
        block.get("text", "") for block in result.get("content") or []
        if block.get("type") == "text")


def _json(result: dict):
    try:
        return json.loads(_text(result))
    except json.JSONDecodeError:
        return None


class Server:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.log = state_dir / "server.log"
        self.files = state_dir / "server-files"
        self.client: McpStdioClient | None = None
        self._lock = asyncio.Lock()

    def _env(self) -> dict[str, str]:
        return {
            key: value for key, value in os.environ.items()
            if key != "USER_EMAIL_PREFERENCES" and not key.startswith("APPLE_MAIL_MCP_")
        }

    async def ensure(self) -> McpStdioClient:
        async with self._lock:
            if self.client is not None and self.client.running:
                return self.client
            if self.client is not None:
                await self.client.close()
                self.client = None
            self.files.mkdir(parents=True, exist_ok=True)
            self.log.write_bytes(b"")
            client = McpStdioClient(
                [sys.executable, "-I", "-m", "apple_mail_mcp", "--read-only",
                 "--write-dir", str(self.files), "--attach-dir", str(self.files)],
                env=self._env(), stderr_path=self.log, client_name="keeptalking-apple-mail",
            )
            try:
                await client.start()
            except McpError:
                await client.close()
                tail = self.log.read_text(errors="replace").strip()[-400:]
                if "No module named" in tail:
                    raise ServerUnavailable(
                        "apple-mail-mcp isn't installed — use Install for Apple Mail in "
                        "the KT Companion (or: companion.py --provision 'Apple Mail')")
                raise ServerUnavailable(f"apple-mail-mcp did not start: {tail or 'no output'}")
            self.client = client
            return client

    async def call(self, route: str, arguments: dict) -> dict:
        client = await self.ensure()
        result = await client.call_tool(route, arguments, timeout=CALL_TIMEOUT)
        if result.get("isError") and (problem := _problem(_text(result))):
            raise ServerUnavailable(problem)
        return result

    async def accounts(self) -> list[str]:
        result = await self.call("list_accounts", {})
        payload = _json(result)
        if isinstance(payload, dict):
            payload = payload.get("result")
        if result.get("isError") or not isinstance(payload, list):
            raise ServerUnavailable("Couldn't list Mail accounts: " + (_text(result)[:400] or "unexpected response"))
        return [str(name) for name in payload if isinstance(name, str)]

    async def source(self, account: str, mailbox: str, message_id: str) -> EmailMessage:
        result = await self.call("get_email_source", {
            "account": account, "mailbox": mailbox,
            "message_id": message_id.strip().strip("<>"), "max_bytes": SOURCE_CAP,
        })
        text = _text(result)
        if result.get("isError") or text.startswith("Error:"):
            raise LookupError(text[:400] or "the message couldn't be read")
        if TRUNCATION_MARKER in text[-200:]:
            raise LookupError(f"the message is larger than {SOURCE_CAP // (1024 * 1024)} MB")
        return email.message_from_string(text, policy=email.policy.default)


class _TextOfHTML(HTMLParser):
    BLOCKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href and href.startswith(("http://", "https://", "mailto:")):
                self.parts.append(f" <{href}> ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _html_text(html: str) -> str:
    parser = _TextOfHTML()
    parser.feed(html)
    text = re.sub(r"[ \t ]+", " ", "".join(parser.parts))
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def _attachments(message: EmailMessage) -> list[EmailMessage]:
    return [
        part for part in message.walk()
        if not part.is_multipart() and (part.is_attachment() or (
            part.get_filename() and part.get_content_maintype() != "text"))
    ]


def _payload(part: EmailMessage) -> bytes:
    return part.get_payload(decode=True) or b""


def _body(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, ValueError):
        content = _payload(part).decode("utf-8", errors="replace")
    if not isinstance(content, str):
        return ""
    return _html_text(content) if part.get_content_subtype() == "html" else content.strip()


_FILENAME_UNSAFE = re.compile(r"[^\w.\- ()+,]")


def _safe_filename(name: str, fallback: str) -> str:
    cleaned = _FILENAME_UNSAFE.sub("_", Path(name or "").name).strip(" .")
    return cleaned[:180] or fallback


def _read(message: EmailMessage, max_chars: int) -> dict:
    body = _body(message)
    truncated = len(body) > max_chars
    return {
        "subject": str(message.get("Subject") or ""),
        "from": str(message.get("From") or ""),
        "to": str(message.get("To") or ""),
        "cc": str(message.get("Cc") or ""),
        "reply_to": str(message.get("Reply-To") or ""),
        "date": str(message.get("Date") or ""),
        "internet_message_id": str(message.get("Message-ID") or "").strip().strip("<>"),
        "in_reply_to": str(message.get("In-Reply-To") or ""),
        "body": body[:max_chars],
        "body_truncated": truncated,
        "body_length": len(body),
        "attachments": [
            {
                "number": number,
                "name": part.get_filename() or f"attachment-{number}",
                "type": part.get_content_type(),
                "size": len(_payload(part)),
                "inline": part.get_content_disposition() == "inline",
            }
            for number, part in enumerate(_attachments(message), start=1)
        ],
    }


def _pick(message: EmailMessage, choice: str) -> tuple[int, EmailMessage] | str:
    parts = _attachments(message)
    if not parts:
        return "this message has no attachments"
    if choice.isdigit():
        number = int(choice)
        if 1 <= number <= len(parts):
            return number, parts[number - 1]
        return f"attachment {number} doesn't exist; this message has {len(parts)}"
    named = [(n, p) for n, p in enumerate(parts, start=1) if (p.get_filename() or "") == choice]
    if not named:
        named = [(n, p) for n, p in enumerate(parts, start=1)
                 if (p.get_filename() or "").lower() == choice.lower()]
    if len(named) == 1:
        return named[0]
    if named:
        return f"{len(named)} attachments are called {choice!r}; pass its number from read_email"
    return f"no attachment called {choice!r}; read_email lists them"


def make_plugin() -> Plugin:
    plugin = Plugin.from_manifest(__file__)
    server = Server(plugin.state_dir)

    @plugin.kind("apple-mail")
    async def apple_mail(args: dict, ctx: CallContext):
        arguments = dict(args)
        tool = ctx.tool or arguments.pop("tool", None)
        arguments.pop("tool", None)
        if tool not in SCHEMAS:
            return (f"unknown tool {tool!r}; choose one of: {', '.join(SCHEMAS)}", True)
        forwarded, problem = _coerce(tool, arguments)
        if problem:
            return problem, True

        allowed = _allowed_accounts(ctx)
        if allowed and tool != "list_accounts":
            account = forwarded.get("account")
            if not account:
                return (f"This instance may only read these Mail accounts: {', '.join(allowed)}. "
                        "Pass one of them as `account`.", True)
            if account not in allowed:
                return f"Denied by instance scope: account {account!r} is outside this instance.", True

        ctx.elucidate((FORWARDED | COMPOSED)[tool])
        try:
            if tool == "list_accounts":
                names = await server.accounts()
                result = [name for name in names if not allowed or name in allowed]
            elif tool in FORWARDED:
                if tool == "list_inbox_emails":
                    forwarded["max_emails"] = max(1, min(forwarded.get("max_emails") or DEFAULT_ROWS, MAX_ROWS))
                if tool == "search_emails":
                    forwarded["limit"] = max(1, min(forwarded.get("limit") or DEFAULT_ROWS, MAX_ROWS))
                if tool in JSON_ROWS:
                    forwarded["output_format"] = "json"
                response = await server.call(tool, forwarded)
                ctx.report_usage("mail.reads", 1)
                if response.get("isError"):
                    return kt_content(response) or [{"type": "text", "text": f"{tool} failed"}], True
                return kt_content(response)
            else:
                mailbox = forwarded.get("mailbox") or "INBOX"
                message = await server.source(forwarded["account"], mailbox, forwarded["message_id"])
                if tool == "read_email":
                    result = _read(message, max(1, forwarded.get("max_chars") or DEFAULT_BODY_CHARS))
                else:
                    picked = _pick(message, forwarded["attachment"])
                    if isinstance(picked, str):
                        return picked, True
                    number, part = picked
                    try:
                        slot = ctx.resources.output("attachment")
                    except ResourceError as error:
                        return (f"get_email_attachment needs an output slot: {error}. Request "
                                "outputs=[{name: \"attachment\", persistence: …}] on the call.", True)
                    filename = _safe_filename(part.get_filename() or "", f"attachment-{number}")
                    data = _payload(part)
                    try:
                        slot.write_bytes(data, child=filename if slot.is_directory else None)
                    except (OSError, ResourceError) as error:
                        return f"get_email_attachment couldn't fill the `attachment` output: {error}", True
                    ctx.report_usage("mail.attachment.bytes", len(data))
                    result = (f"Saved {filename} ({part.get_content_type()}, {len(data)} bytes) "
                              f"into the `attachment` output ({slot.handle}).")
        except ServerUnavailable as error:
            return str(error), True
        except LookupError as error:
            return f"{tool}: {error}", True
        except McpError as error:
            return f"Apple Mail MCP error during {tool}: {error}", True

        ctx.report_usage("mail.reads", 1)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)

    @plugin.scope_options("apple-mail", "accounts")
    async def account_choices(request: ScopeOptionsRequest) -> list[dict]:
        try:
            names = await server.accounts()
        except ServerUnavailable as error:
            raise RuntimeError(str(error))
        query = (request.query or "").lower()
        return [
            scope_option(name, name, symbol="envelope")
            for name in sorted(names, key=str.lower)
            if not query or query in name.lower()
        ]

    return plugin


if __name__ == "__main__":
    make_plugin().run()
