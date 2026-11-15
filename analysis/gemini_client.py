"""Single chokepoint for all Gemini SDK calls.

Wraps `client.models.generate_content`, `client.aio.models.generate_content`,
and `client.models.embed_content` to capture real token counts from
`usage_metadata` and write a per-call ledger row to Supabase's `gemini_calls`
table. Origin attribution flows from explicit param > ANIME_INTEL_CALL_ORIGIN
env var > 'production' (the safe default for unconfigured callers).

Phase A findings from PR #85 that shape this module:

- `generate_content` responses carry FOUR token fields, not two:
  prompt_token_count, cached_content_token_count, candidates_token_count,
  and thoughts_token_count. Thoughts are billed at the OUTPUT rate, not
  a separate one — invisible cost in the pre-PR-#85 dashboard.
- `embed_content` responses have NO usage_metadata. Tokens are estimated
  from input text length (~5-10% error margin; acceptable since embeddings
  are <5% of total spend).
"""
import inspect
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

from google import genai

logger = logging.getLogger(__name__)


# Per-model billing rates from Google's pricing page (validated 2026-06).
# Thoughts tokens are billed at the OUTPUT rate, NOT a separate rate —
# they're folded into the output_per_m calculation below.
#
# When updating: keep this dict the single source of truth. The dashboard
# reads aggregated cost from gemini_calls rows, which were computed using
# these rates at write time. Rate changes only affect FUTURE calls.
RATES: dict[str, dict[str, float]] = {
    "gemini-2.5-flash": {
        "input_per_m": 0.30,
        "cached_input_per_m": 0.075,
        "output_per_m": 2.50,
    },
    "gemini-2.5-flash-lite": {
        "input_per_m": 0.10,
        "cached_input_per_m": 0.025,
        "output_per_m": 0.40,
    },
    "gemini-embedding-001": {
        "input_per_m": 0.15,
    },
}


VALID_ORIGINS = frozenset({
    "production",
    "ingestion",
    "dev",
    "test",
    "probe",
    "regression",
})

DEFAULT_ORIGIN = "production"

# Char-per-token heuristic used by `_calculate_embed_cost`. Google's embed
# API doesn't return usage_metadata, so we estimate from input text length.
# 4 is the widely-cited rule of thumb for English text; ±5-10% error margin
# is acceptable since embeddings are <5% of total spend.
_EMBED_CHARS_PER_TOKEN = 4


def make_client(api_key: str) -> genai.Client:
    """Construct a Gemini client pinned to the v1beta API.

    Structured output (`response_schema` + `response_mime_type:
    application/json`) is only supported on the v1beta endpoint. The
    `google-genai` SDK's default endpoint flipped from v1beta to v1 in a
    post-1.x major-version bump, which breaks every reranker + distiller
    call site at once. This helper pins the choice in one place so a
    future SDK upgrade can't silently re-break production.
    """
    return genai.Client(
        api_key=api_key,
        http_options={"api_version": "v1beta"},
    )


def _resolve_origin(explicit: str | None) -> str:
    """Resolve origin: explicit > env > default.

    An invalid `explicit` value RAISES ValueError — caller-provided
    origins are code paths, not runtime config, and a typo there is a
    bug the developer should see immediately. An invalid env-var value
    only logs a warning and falls back to 'production' — env config is
    ambient, and crashing the whole app on a mistyped var would be
    worse than misattributing a batch of calls.
    """
    if explicit is not None:
        if explicit not in VALID_ORIGINS:
            raise ValueError(
                f"Invalid origin '{explicit}'. Must be one of {sorted(VALID_ORIGINS)}."
            )
        return explicit

    env_origin = os.environ.get("ANIME_INTEL_CALL_ORIGIN")
    if env_origin:
        if env_origin in VALID_ORIGINS:
            return env_origin
        logger.warning(
            f"ANIME_INTEL_CALL_ORIGIN='{env_origin}' is not a valid origin; "
            f"falling back to '{DEFAULT_ORIGIN}'. Valid: {sorted(VALID_ORIGINS)}"
        )

    return DEFAULT_ORIGIN


def _resolve_caller(explicit: str | None) -> str:
    """Stack-walk to find the calling function's qualified name.

    Walks up the stack and returns the first frame whose file is NOT
    gemini_client.py — that's the user code we want to attribute. Handles
    every entry point (generate_content, generate_content_async,
    embed_content, direct _resolve_caller test calls) consistently
    regardless of how many internal helper frames sit between.

    Falls back to 'unknown' if the walk runs out of frames.
    """
    if explicit:
        return explicit

    this_file = __file__
    frame = inspect.currentframe()
    try:
        while frame is not None:
            frame = frame.f_back
            if frame is None:
                return "unknown"
            if frame.f_code.co_filename != this_file:
                return frame.f_code.co_qualname
        return "unknown"
    finally:
        del frame


def _calculate_generate_cost(model: str, usage_metadata: Any) -> float:
    """Compute USD cost from a generate_content response's usage_metadata.

    Cached portion of input is billed at the discounted cached rate;
    remainder of input bills at standard rate. Candidates AND thoughts
    both bill at the output rate (Google quirk — discovered in Phase A
    probe).
    """
    rates = RATES.get(model)
    if not rates:
        logger.warning(f"No rate dict for model '{model}'; cost=0")
        return 0.0

    prompt = usage_metadata.prompt_token_count or 0
    cached = usage_metadata.cached_content_token_count or 0
    candidates = usage_metadata.candidates_token_count or 0
    thoughts = usage_metadata.thoughts_token_count or 0

    non_cached_input = max(0, prompt - cached)
    input_cost = non_cached_input * rates["input_per_m"] / 1_000_000
    cached_cost = cached * rates["cached_input_per_m"] / 1_000_000

    output_tokens = candidates + thoughts
    output_cost = output_tokens * rates["output_per_m"] / 1_000_000

    return input_cost + cached_cost + output_cost


def _calculate_embed_cost(model: str, contents: list[str] | str) -> tuple[int, float]:
    """Estimate cost for embed_content (no usage_metadata in response).

    Returns (estimated_tokens, cost_usd) so both can be logged. Token
    estimation uses `_EMBED_CHARS_PER_TOKEN` (see module constants).
    """
    rates = RATES.get(model)
    if not rates:
        logger.warning(f"No rate dict for model '{model}'; cost=0")
        return 0, 0.0

    if isinstance(contents, str):
        total_chars = len(contents)
    else:
        total_chars = sum(len(s) for s in contents)

    estimated_tokens = total_chars // _EMBED_CHARS_PER_TOKEN
    cost = estimated_tokens * rates["input_per_m"] / 1_000_000
    return estimated_tokens, cost


def _log_call(
    model: str,
    caller: str,
    origin: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    search_id: str | None,
) -> None:
    """Daemon-thread write to Supabase gemini_calls. Best-effort, never blocks.

    Skips silently when TESTING=1 (keeps prod analytics clean of CI traffic)
    or when SUPABASE_URL/SUPABASE_SERVICE_KEY are missing (CI without
    secrets degrades gracefully). Same write semantics as PR #76's search
    journal migration.
    """
    if os.environ.get("TESTING") == "1":
        return
    if not os.environ.get("SUPABASE_URL") or not os.environ.get("SUPABASE_SERVICE_KEY"):
        return

    payload = {
        "event_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "caller": caller,
        "origin": origin,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd_estimate": round(cost_usd, 6),
        "search_id": search_id,
    }

    def _write() -> None:
        try:
            # Late import: keeps analysis.gemini_client importable in
            # contexts (tests, CLI tools) that don't have the api package
            # wired up.
            from api.supabase_client import supabase_post
            supabase_post("gemini_calls", payload)
        except Exception as e:  # noqa: BLE001 — daemon-thread telemetry write must never surface an exception to the caller
            logger.warning(f"Failed to log gemini call: {type(e).__name__}: {e}")

    threading.Thread(target=_write, daemon=True).start()


def _capture_generate_usage(
    response: Any,
    model: str,
    origin: str | None,
    caller: str | None,
    search_id: str | None,
) -> None:
    """Shared post-call accounting for sync + async generate_content."""
    try:
        resolved_origin = _resolve_origin(origin)
        resolved_caller = _resolve_caller(caller)

        usage = response.usage_metadata
        if usage is None:
            return

        input_tokens = usage.prompt_token_count or 0
        output_tokens = (usage.candidates_token_count or 0) + (
            usage.thoughts_token_count or 0
        )
        cost = _calculate_generate_cost(model, usage)
        _log_call(
            model=model,
            caller=resolved_caller,
            origin=resolved_origin,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            search_id=search_id,
        )
    except Exception as e:  # noqa: BLE001 — cost tracking must never block the SDK response from reaching the caller
        logger.warning(f"Cost tracking failed for {model}: {type(e).__name__}: {e}")


def _capture_embed_usage(
    contents: list[str] | str,
    model: str,
    origin: str | None,
    caller: str | None,
    search_id: str | None,
) -> None:
    """Post-call accounting for embed_content. Symmetric with the
    generate variant, but works from input text length instead of
    response.usage_metadata (which the embed API doesn't populate).
    """
    try:
        resolved_origin = _resolve_origin(origin)
        resolved_caller = _resolve_caller(caller)
        estimated_tokens, cost = _calculate_embed_cost(model, contents)
        _log_call(
            model=model,
            caller=resolved_caller,
            origin=resolved_origin,
            input_tokens=estimated_tokens,
            output_tokens=0,
            cost_usd=cost,
            search_id=search_id,
        )
    except Exception as e:  # noqa: BLE001 — cost tracking must never block the SDK response from reaching the caller
        logger.warning(f"Cost tracking failed for {model}: {type(e).__name__}: {e}")


def _inject_thinking_budget(kwargs: dict, thinking_budget: int | None) -> None:
    """Mutate kwargs in place to set config.thinking_config.thinking_budget.

    PR #87 Phase C — gives Phase D call sites a clean kwarg interface
    instead of duplicating `{"thinking_config": {"thinking_budget": N}}`
    dict scaffolding across the 5 reranker entry points.

    Behavior:
    - `thinking_budget=None`: no-op. Existing config passes through
      untouched and the SDK applies its default thinking (~3,800-5,000
      thoughts per call on gemini-2.5-flash per PR #87 Phase A probe).
    - `thinking_budget=0`: disable thinking entirely. Must be injected,
      not skipped as if-falsy — Phase D's FUSION/CONSTRAINT constants
      depend on this.
    - `thinking_budget=N` (N>0): cap thinking at N tokens.

    If the caller already set `config["thinking_config"]`, the explicit
    kwarg wins — treated as the more recent intent. Caller's config dict
    is shallow-copied before mutation so a re-used config object isn't
    accidentally mutated across calls.
    """
    if thinking_budget is None:
        return
    config = dict(kwargs.get("config") or {})
    config["thinking_config"] = {"thinking_budget": thinking_budget}
    kwargs["config"] = config


def generate_content(
    client: genai.Client,
    model: str,
    contents: Any,
    *,
    origin: str | None = None,
    caller: str | None = None,
    search_id: str | None = None,
    thinking_budget: int | None = None,
    **kwargs: Any,
) -> Any:
    """Wrap client.models.generate_content with cost tracking.

    Returns the SDK response object unchanged — callers don't need to
    change response handling.

    `thinking_budget` (PR #87 Phase C) controls Gemini 2.5 Flash's
    internal reasoning tokens. None preserves SDK default behavior;
    pass 0 to disable thinking, or N>0 to cap at N tokens. See
    `_inject_thinking_budget` for the precise contract.
    """
    _inject_thinking_budget(kwargs, thinking_budget)
    response = client.models.generate_content(model=model, contents=contents, **kwargs)
    _capture_generate_usage(response, model, origin, caller, search_id)
    return response


async def generate_content_async(
    client: genai.Client,
    model: str,
    contents: Any,
    *,
    origin: str | None = None,
    caller: str | None = None,
    search_id: str | None = None,
    thinking_budget: int | None = None,
    **kwargs: Any,
) -> Any:
    """Async variant for callers using client.aio.models.generate_content.

    Used by sentiment_distiller.py. Accepts the same `thinking_budget`
    kwarg as the sync path (PR #87 Phase C).
    """
    _inject_thinking_budget(kwargs, thinking_budget)
    response = await client.aio.models.generate_content(
        model=model, contents=contents, **kwargs
    )
    _capture_generate_usage(response, model, origin, caller, search_id)
    return response


def embed_content(
    client: genai.Client,
    model: str,
    contents: list[str] | str,
    *,
    origin: str | None = None,
    caller: str | None = None,
    search_id: str | None = None,
    **kwargs: Any,
) -> Any:
    """Wrap client.models.embed_content with cost tracking.

    Tokens are estimated from input text length since the embed response
    has no usage_metadata (Phase A finding).
    """
    response = client.models.embed_content(model=model, contents=contents, **kwargs)
    _capture_embed_usage(contents, model, origin, caller, search_id)
    return response
