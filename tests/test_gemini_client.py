"""
Unit tests for analysis/gemini_client.py — Initiative 5 (PR #85) wrapper.

Phase B coverage:
  - Cost math: validates the rate × token math against known fixtures.
    Includes the thoughts_token_count line that PR #85 Phase A surfaced
    as a hidden cost driver (billed at output rate, not separately).
  - Origin resolution: explicit > env > default precedence.
  - Caller resolution: stack inspection finds the right frame.

Phases C-F add the dual-write, integration, and live-verify tests.
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


# =====================================================================
# COST CALCULATION — generate_content
# =====================================================================

class TestGenerateContentCost:
    """The cost math is the load-bearing accuracy of the dashboard.
    These fixtures pin the formula against hand-computed expectations."""

    def _fixture(self, **overrides) -> SimpleNamespace:
        """Build a usage_metadata stand-in with default zeros."""
        defaults = {
            "prompt_token_count": 0,
            "cached_content_token_count": 0,
            "candidates_token_count": 0,
            "thoughts_token_count": 0,
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_input_only_cost(self):
        """1M input tokens on Flash 2.5 at $0.30/M = $0.30 exact."""
        from analysis.gemini_client import _calculate_generate_cost
        usage = self._fixture(prompt_token_count=1_000_000)
        cost = _calculate_generate_cost("gemini-2.5-flash", usage)
        assert cost == pytest.approx(0.30, abs=1e-9)

    def test_output_only_cost(self):
        """1M output tokens on Flash 2.5 at $2.50/M = $2.50 exact."""
        from analysis.gemini_client import _calculate_generate_cost
        usage = self._fixture(candidates_token_count=1_000_000)
        cost = _calculate_generate_cost("gemini-2.5-flash", usage)
        assert cost == pytest.approx(2.50, abs=1e-9)

    def test_thoughts_bill_at_output_rate(self):
        """thoughts_token_count is billed at OUTPUT rate — the hidden cost
        line that PR #85 Phase A surfaced. 1M thoughts = same $2.50 as 1M
        candidates."""
        from analysis.gemini_client import _calculate_generate_cost
        usage = self._fixture(thoughts_token_count=1_000_000)
        cost = _calculate_generate_cost("gemini-2.5-flash", usage)
        assert cost == pytest.approx(2.50, abs=1e-9)

    def test_thoughts_plus_candidates_sum_correctly(self):
        """500K candidates + 500K thoughts = 1M output total at $2.50/M."""
        from analysis.gemini_client import _calculate_generate_cost
        usage = self._fixture(
            candidates_token_count=500_000,
            thoughts_token_count=500_000,
        )
        cost = _calculate_generate_cost("gemini-2.5-flash", usage)
        assert cost == pytest.approx(2.50, abs=1e-9)

    def test_cached_input_billed_at_discounted_rate(self):
        """500K of a 1M prompt is cached → 500K @ $0.30/M + 500K @ $0.075/M.
        = $0.15 + $0.0375 = $0.1875 exact."""
        from analysis.gemini_client import _calculate_generate_cost
        usage = self._fixture(
            prompt_token_count=1_000_000,
            cached_content_token_count=500_000,
        )
        cost = _calculate_generate_cost("gemini-2.5-flash", usage)
        assert cost == pytest.approx(0.1875, abs=1e-9)

    def test_realistic_rerank_call(self):
        """Models a typical FUSION rerank — ~5,700 input + ~7,500 candidates
        + ~3,000 thoughts based on Phase A probe extrapolation. The dashboard's
        pre-PR-#85 estimate of $0.0005/call vs the real number."""
        from analysis.gemini_client import _calculate_generate_cost
        usage = self._fixture(
            prompt_token_count=5_700,
            candidates_token_count=7_500,
            thoughts_token_count=3_000,
        )
        cost = _calculate_generate_cost("gemini-2.5-flash", usage)
        # input: 5700 * 0.30/M = 0.00171
        # output: (7500 + 3000) * 2.50/M = 0.02625
        # total: 0.02796
        assert cost == pytest.approx(0.02796, abs=1e-6)

    def test_unknown_model_returns_zero_with_warning(self):
        """Unknown models log a warning and return $0 rather than crashing —
        safer than blowing up the engine on a typo."""
        from analysis.gemini_client import _calculate_generate_cost
        usage = self._fixture(prompt_token_count=1000)
        cost = _calculate_generate_cost("gemini-9.99-imaginary", usage)
        assert cost == 0.0


# =====================================================================
# COST CALCULATION — embed_content
# =====================================================================

class TestEmbedContentCost:
    """Embeddings have no usage_metadata, so we estimate from input length.
    Pin the heuristic so it stays at len(text) // 4."""

    def test_single_string_estimation(self):
        """40 chars → 10 tokens (40 // 4)."""
        from analysis.gemini_client import _calculate_embed_cost
        tokens, cost = _calculate_embed_cost("gemini-embedding-001", "a" * 40)
        assert tokens == 10
        # 10 tokens * 0.15/M = 0.0000015
        assert cost == pytest.approx(1.5e-6, abs=1e-12)

    def test_list_of_strings_sums_correctly(self):
        """3 strings of 8 chars each = 24 chars total → 6 tokens."""
        from analysis.gemini_client import _calculate_embed_cost
        tokens, cost = _calculate_embed_cost(
            "gemini-embedding-001", ["a" * 8, "b" * 8, "c" * 8]
        )
        assert tokens == 6
        assert cost == pytest.approx(6 * 0.15 / 1_000_000, abs=1e-12)

    def test_unknown_model_returns_zero(self):
        from analysis.gemini_client import _calculate_embed_cost
        tokens, cost = _calculate_embed_cost("unknown-embedding", "hello world")
        assert tokens == 0
        assert cost == 0.0


# =====================================================================
# ORIGIN RESOLUTION — explicit > env > default
# =====================================================================

class TestOriginResolution:
    """The six-origin enum is load-bearing for the dashboard panel."""

    def test_default_is_production(self):
        from analysis.gemini_client import _resolve_origin
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANIME_INTEL_CALL_ORIGIN", None)
            assert _resolve_origin(None) == "production"

    def test_env_var_overrides_default(self):
        from analysis.gemini_client import _resolve_origin
        with patch.dict(os.environ, {"ANIME_INTEL_CALL_ORIGIN": "regression"}):
            assert _resolve_origin(None) == "regression"

    def test_explicit_param_overrides_env(self):
        from analysis.gemini_client import _resolve_origin
        with patch.dict(os.environ, {"ANIME_INTEL_CALL_ORIGIN": "regression"}):
            assert _resolve_origin("probe") == "probe"

    @pytest.mark.parametrize(
        "origin",
        ["production", "ingestion", "dev", "test", "probe", "regression"],
    )
    def test_all_six_valid_origins_accepted(self, origin):
        from analysis.gemini_client import _resolve_origin
        assert _resolve_origin(origin) == origin

    def test_invalid_explicit_origin_raises(self):
        """Explicit invalid origin is a developer bug — fail loudly."""
        from analysis.gemini_client import _resolve_origin
        with pytest.raises(ValueError, match="Invalid origin"):
            _resolve_origin("staging")

    def test_invalid_env_value_falls_back_to_default(self):
        """Invalid env values fall back rather than crash — production
        safety beats developer ergonomics here. Warning logged."""
        from analysis.gemini_client import _resolve_origin
        with patch.dict(os.environ, {"ANIME_INTEL_CALL_ORIGIN": "develop"}):
            assert _resolve_origin(None) == "production"


# =====================================================================
# CALLER RESOLUTION — stack inspection
# =====================================================================

class TestCallerResolution:
    """Caller is logged so the dashboard can attribute cost back to
    specific call sites without explicit per-call instrumentation."""

    def test_explicit_caller_overrides_stack(self):
        from analysis.gemini_client import _resolve_caller
        assert _resolve_caller("my.module.func") == "my.module.func"

    def test_stack_inspection_finds_test_function(self):
        """When no explicit caller is given, the stack-walker should find
        the test method's qualified name."""
        from analysis.gemini_client import _resolve_caller
        result = _resolve_caller(None)
        # The caller of _resolve_caller is this test method.
        assert "test_stack_inspection_finds_test_function" in result


# =====================================================================
# THINKING BUDGET PLUMBING — PR #87 Phase C
# =====================================================================

class TestThinkingBudgetPlumbing:
    """The thinking_budget kwarg threads through to the SDK's config dict
    as `{"thinking_config": {"thinking_budget": N}}` so Phase D call
    sites can opt in to specific budgets via a named module-level
    constant rather than duplicating dict scaffolding."""

    @staticmethod
    def _mock_response():
        # usage_metadata=None tells _capture_generate_usage to short-circuit
        # without touching the rates dict or trying to write a ledger row.
        return SimpleNamespace(usage_metadata=None)

    @classmethod
    def _mock_client(cls):
        from unittest.mock import MagicMock
        client = MagicMock()
        client.models.generate_content.return_value = cls._mock_response()
        return client

    @classmethod
    def _mock_async_client(cls):
        from unittest.mock import AsyncMock, MagicMock
        client = MagicMock()
        client.aio.models.generate_content = AsyncMock(return_value=cls._mock_response())
        return client

    def test_generate_content_threads_thinking_budget_into_config(self):
        """thinking_budget=128 → SDK call's config gains thinking_config."""
        from analysis.gemini_client import generate_content
        client = self._mock_client()
        generate_content(
            client,
            model="gemini-2.5-flash",
            contents="hi",
            config={"response_mime_type": "application/json"},
            thinking_budget=128,
        )
        called = client.models.generate_content.call_args
        assert called.kwargs["config"]["thinking_config"] == {"thinking_budget": 128}
        # Existing config keys are preserved alongside the injection.
        assert called.kwargs["config"]["response_mime_type"] == "application/json"

    def test_generate_content_none_passthrough_leaves_config_unchanged(self):
        """thinking_budget=None (default) means do not touch config —
        SDK's default thinking applies. Critical for backward compat
        with the embed_content path and any call site Phase D leaves
        unmigrated."""
        from analysis.gemini_client import generate_content
        client = self._mock_client()
        original = {"response_mime_type": "application/json"}
        generate_content(
            client,
            model="gemini-2.5-flash",
            contents="hi",
            config=original,
        )
        called = client.models.generate_content.call_args
        assert "thinking_config" not in called.kwargs["config"]
        # The exact dict identity should also pass through unmodified
        # — proves we didn't shallow-copy unnecessarily on the None path.
        assert called.kwargs["config"] is original

    def test_thinking_budget_zero_is_injected_not_skipped(self):
        """thinking_budget=0 means 'disable thinking entirely' — must be
        injected, not skipped as if-falsy. Phase D's FUSION/CONSTRAINT
        constants depend on this; without it, those budgets would
        silently fall back to SDK default (~3,800-5,000 thoughts/call,
        per Phase A probe) and the cost mitigation would not land."""
        from analysis.gemini_client import generate_content
        client = self._mock_client()
        generate_content(
            client,
            model="gemini-2.5-flash",
            contents="hi",
            config={},
            thinking_budget=0,
        )
        called = client.models.generate_content.call_args
        assert called.kwargs["config"]["thinking_config"] == {"thinking_budget": 0}

    def test_explicit_kwarg_wins_over_config_dict_thinking_config(self):
        """If a caller has both `thinking_budget=X` kwarg AND a
        `thinking_config` already inside the config dict, the explicit
        kwarg wins — treated as the more recent intent. Documented
        precedence; locked here so a future refactor can't silently
        flip it."""
        from analysis.gemini_client import generate_content
        client = self._mock_client()
        generate_content(
            client,
            model="gemini-2.5-flash",
            contents="hi",
            config={"thinking_config": {"thinking_budget": 1024}},
            thinking_budget=128,
        )
        called = client.models.generate_content.call_args
        assert called.kwargs["config"]["thinking_config"] == {"thinking_budget": 128}

    def test_generate_content_async_threads_thinking_budget(self):
        """Async path parity with the sync path."""
        import asyncio
        from analysis.gemini_client import generate_content_async
        client = self._mock_async_client()
        asyncio.run(generate_content_async(
            client,
            model="gemini-2.5-flash",
            contents="hi",
            config={"response_mime_type": "application/json"},
            thinking_budget=512,
        ))
        called = client.aio.models.generate_content.call_args
        assert called.kwargs["config"]["thinking_config"] == {"thinking_budget": 512}
        assert called.kwargs["config"]["response_mime_type"] == "application/json"


# =====================================================================
# CLIENT CONSTRUCTION — api_version pinning
# =====================================================================


class TestMakeClient:
    """`make_client` must pin the API version to v1beta.

    Structured output (response_schema + response_mime_type:
    application/json) is only supported on v1beta. A regression here
    silently breaks every reranker + distiller call site at once —
    the exact failure mode that prompted this helper.
    """

    def test_make_client_pins_v1beta(self):
        from analysis.gemini_client import make_client
        client = make_client("test-key")
        assert client._api_client._http_options.api_version == "v1beta"
