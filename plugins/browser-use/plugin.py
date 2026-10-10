"""The browser-use preset — first plugin of the KT Companion runtime.

Registers the `browser-task` action kind: run a natural-language browser task
via https://github.com/browser-use/browser-use, confined to the domains the
*user* configured on the KT action instance (the kind's scope schema) — or
unconfined when they named none.

Two execution paths, chosen at call time:
  * real — ``browser_use`` importable AND an LLM API key in the environment;
  * mock — otherwise: no browser, but the same scope enforcement, content
           shape, and signed usage receipt, so the protocol demo runs anywhere.
"""

from __future__ import annotations

import asyncio
import os
import re
import time

from keeptalking_plugin import CallContext, Plugin

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-5.2",
    # Custom endpoints serve arbitrary model names — no sensible default.
    "openai-compatible": "",
}


def _mentioned_hosts(task: str) -> list[str]:
    return re.findall(r"(?:https?://)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)", task.lower())


def _blocked_hosts(task: str, allowed: list[str] | None) -> list[str]:
    """Hosts named in the task that fall outside the instance's allowlist.
    An empty/None allowlist means *unrestricted* — nothing is blocked. The
    boundary is the grant itself: an instance is inert until the user grants it,
    and having granted "browse the web" they meant the web, not nothing. A
    narrower instance is expressed by listing domains."""
    mentioned = _mentioned_hosts(task)
    if not allowed:
        return []
    normalized = [a.lower().lstrip("*.") for a in allowed]
    return [
        host
        for host in mentioned
        if not any(host == a or host.endswith("." + a) for a in normalized)
    ]


def _normalize_base_url(raw: str) -> tuple[str, str | None]:
    """OpenAI-compatible clients append the route themselves, so this field
    wants the base (`…/v1`), not a full completions URL. Pasting the latter is
    the common mistake and yields 404s on `…/chat/completions/chat/completions`,
    so trim it and say so rather than failing obscurely."""
    url = raw.strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/responses"):
        if url.endswith(suffix):
            return (
                url[: -len(suffix)],
                f"trimmed '{suffix}' — this field takes the base URL",
            )
    return url, None


async def _run_real(
    task: str, allowed: list[str] | None, headless: bool, config: dict, base_url: str
) -> str:
    from browser_use import Agent  # heavyweight import, deferred to call time

    kwargs = {}
    try:
        from browser_use import BrowserSession

        kwargs["browser_session"] = BrowserSession(
            allowed_domains=allowed or None, headless=headless
        )
    except Exception:
        pass  # older/newer API shapes; the agent default still runs

    provider = config.get("llm_provider") or "anthropic"
    api_key = config.get("llm_api_key")
    model = config.get("llm_model") or DEFAULT_MODELS.get(provider, "")

    # `base_url` is what points this at OpenRouter, a gateway, or a local
    # OpenAI-compatible server; passed only when set so stock provider
    # defaults still apply.
    client_kwargs: dict = {"model": model}
    if api_key:
        client_kwargs["api_key"] = api_key
    if base_url:
        client_kwargs["base_url"] = base_url

    llm = None
    if provider == "anthropic":
        try:
            from browser_use import ChatAnthropic

            llm = ChatAnthropic(**client_kwargs)
        except Exception:
            pass
    if llm is None:
        try:
            from browser_use import ChatOpenAI

            llm = ChatOpenAI(**client_kwargs)
        except Exception as error:
            # An OpenAI-compatible endpoint is the whole point of base_url —
            # failing silently here would look like "mock for no reason".
            if base_url:
                raise RuntimeError(
                    f"could not build an OpenAI-compatible client for {base_url}: {error}"
                ) from error

    agent = Agent(task=task, llm=llm, **kwargs) if llm else Agent(task=task, **kwargs)
    history = await agent.run()
    final = getattr(history, "final_result", lambda: None)()
    return final or "browser task finished (no final result text)"


def make_plugin() -> Plugin:
    plugin = Plugin.from_manifest(__file__)

    @plugin.kind("browser-task")
    async def browser_task(args: dict, ctx: CallContext):
        task = args.get("task", "").strip()
        if not task:
            return "missing required argument: task", True

        scope = ctx.scope or {}
        allowed = scope.get("allowedDomains")
        headless = bool(scope.get("headless", True))
        started = time.monotonic()

        # Instance-scope enforcement happens HERE, plugin-side, in addition to
        # the host gating the call — the dual-enforcement contract. An empty
        # allowlist is not a scope of nothing; it is no domain restriction.
        blocked = _blocked_hosts(task, allowed)
        if blocked:
            ctx.report_usage("browser.seconds", 0)
            return (
                f"refused: task names host(s) outside this instance's scope: "
                f"{', '.join(sorted(set(blocked)))} (allowed: {', '.join(allowed)})",
                True,
            )

        config = ctx.config
        force_mock = bool(config.get("force_mock")) or bool(os.environ.get("KT_DEMO_MOCK"))
        provider = config.get("llm_provider") or "anthropic"
        base_url, url_note = _normalize_base_url(config.get("llm_base_url") or "")

        # A custom endpoint is only half a configuration without a model name,
        # and stock providers are only reachable with a key.
        if provider == "openai-compatible" and not base_url and not force_mock:
            ctx.report_usage("browser.seconds", 0)
            return "provider 'openai-compatible' needs an Endpoint URL in the plugin config", True

        # Decide what *blocks* a real run before attempting one, so the mock
        # always reports the true reason. A catch-all "else" here previously
        # claimed "no model configured" for every fallback — including a
        # missing browser-use install, which is the opposite of helpful.
        if force_mock:
            mock_reason = "forced mock"
        elif not config.get("llm_api_key"):
            mock_reason = "no LLM API key configured"
        elif provider == "openai-compatible" and not config.get("llm_model"):
            mock_reason = "no model configured for the custom endpoint"
        else:
            mock_reason = None

        if mock_reason is None:
            try:
                result = await _run_real(task, allowed, headless, config, base_url)
                ctx.report_usage("browser.seconds", int(time.monotonic() - started) + 1)
                return result
            except ImportError:
                mock_reason = (
                    "browser-use is not installed — use Install in the KT Companion "
                    "menu (or: companion.py --provision BrowserUse)"
                )
            except Exception as error:
                ctx.report_usage("browser.seconds", int(time.monotonic() - started) + 1)
                return f"browser-use run failed: {error}", True

        await asyncio.sleep(1)  # stand-in for the browser session
        model = config.get("llm_model") or DEFAULT_MODELS.get(provider) or "unset"
        scope_note = (
            f"within allowed domains {allowed}" if allowed else "with no domain restriction"
        )
        ctx.report_usage("browser.seconds", int(time.monotonic() - started) + 1)
        return (
            f"[mock] {mock_reason} (provider={provider}, model={model}, "
            f"endpoint={base_url or 'provider default'}"
            f"{'; ' + url_note if url_note else ''}). Would run "
            f"headless={headless} {scope_note}: {task!r}"
        )

    return plugin
