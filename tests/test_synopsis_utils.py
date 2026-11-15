"""Tests for analysis/synopsis_utils.py.

Coverage targets the three populations from the probe findings:
- 5,033 trailing attribution tags that MUST strip (dominated by
  `[Written by MAL Rewrite]` and `(Source: ...)` variants)
- 15 legitimate non-attribution parentheticals that MUST survive
  (subtitles, context notes, runtime, translations)
- 7 chained-tag cases that need iterative stripping
"""

import os
import sys

import pytest

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from analysis.synopsis_utils import strip_mal_attribution


class TestStripsAttributionTags:
    """Every trailing MAL attribution tag seen in the vault probe
    should strip cleanly."""

    @pytest.mark.parametrize(
        "trailing",
        [
            "[Written by MAL Rewrite]",
            "(Source: ANN)",
            "(Source: AniDB)",
            "(Source: Wikipedia)",
            "(Source: MAL News)",
            "(Source: Crunchyroll)",
            "(Source: AnimeNfo)",
            "(Source: Sentai Filmworks)",
            "(Source: Bilibili, translated)",
            "(Source: ANN, edited)",
            "(Source: Official site)",
            "(Source: VIZ Media)",
            "(Source: J-Novel Club)",
            "(Note: This show aired without subtitles)",
            "[Note: Promotional preview]",
        ],
    )
    def test_strips_known_attribution_variant(self, trailing):
        body = "A compelling tale of adventure and intrigue."
        result = strip_mal_attribution(f"{body} {trailing}")
        assert result == body

    def test_strips_tag_when_separated_by_newlines(self):
        """MAL frequently places the attribution on its own line."""
        body = "A sprawling family saga."
        result = strip_mal_attribution(f"{body}\n\n(Source: ANN)")
        assert result == body

    def test_strips_tag_with_only_whitespace_separator(self):
        body = "Short piece."
        result = strip_mal_attribution(f"{body}   [Written by MAL Rewrite]")
        assert result == body

    def test_case_insensitive_opener(self):
        """Case variants observed in the probe."""
        body = "A story about courage."
        assert strip_mal_attribution(f"{body} (SOURCE: ANN)") == body
        assert strip_mal_attribution(f"{body} (source: ann)") == body
        assert strip_mal_attribution(f"{body} [WRITTEN BY MAL REWRITE]") == body


class TestPreservesLegitimateContent:
    """The 15 non-attribution trailing parentheticals observed in the
    probe (plus a few additional defensive cases). None should strip."""

    @pytest.mark.parametrize(
        "trailing",
        [
            "(aired after Episode 53)",
            "(aired after Episode 174)",
            "(Future)",
            "(ii ne!)",
            "(DVD Episode 9.5)",
            "( /www.productionig.com )",
            "(Summary found on Veoh.com)",
            "(Yet Another Man's Battlefield)",
            "(バタフライ伯爵夫人の優雅な一日 最終章)",
            "(Yuki Asobi)",
            "(second volume)",
            '(a combination of "mobile" and "beauty.")',
            "(3:07)",
            "(Oozora Subaru, Minato Aqua, Usada Pekora)",
            "(Sisters)",
        ],
    )
    def test_preserves_legitimate_trailing_parenthetical(self, trailing):
        text = f"A story (featuring something interesting). {trailing}"
        assert strip_mal_attribution(text) == text

    def test_preserves_mid_sentence_attribution_shaped_text(self):
        """The regex anchors at end-of-string. Mid-text attribution-
        shaped content should never strip."""
        text = "The show (Source: ANN) was reviewed widely before airing."
        assert strip_mal_attribution(text) == text

    def test_preserves_year_parenthetical(self):
        """A story that genuinely ends with a year in parens."""
        text = "A cult favorite from the mid-90s (1996)."
        assert strip_mal_attribution(text) == text


class TestIterativeChainedStripping:
    """7 vault cases chain two attribution tags at the end. Both must
    strip in a single call."""

    def test_strips_two_chained_attributions(self):
        body = "The crew faces a new enemy."
        text = f"{body} (Source: ANN) [Written by MAL Rewrite]"
        assert strip_mal_attribution(text) == body

    def test_strips_two_chained_with_newline_between(self):
        """Observed in the probe: mal_id 1094, 1238."""
        body = "A complex ending with multiple arcs."
        text = f"{body}\n\n(Source: Wikipedia)\n\n[Written by MAL Rewrite]"
        assert strip_mal_attribution(text) == body

    def test_strips_three_chained_attributions(self):
        """Not observed, but iteration should handle any chain depth."""
        body = "A short synopsis."
        text = f"{body} (Note: Preview) (Source: ANN) [Written by MAL Rewrite]"
        assert strip_mal_attribution(text) == body

    def test_preserves_legitimate_after_stripping_attribution(self):
        """Chained but mixed: strip attribution, keep legitimate.
        Observed pattern: `(aired after Episode 53)\n\n(Source: Wikipedia)`
        should become `...(aired after Episode 53)` after one strip."""
        body = "A sweeping storyline."
        text = f"{body} (aired after Episode 53)\n\n(Source: Wikipedia)"
        expected = f"{body} (aired after Episode 53)"
        assert strip_mal_attribution(text) == expected


class TestEdgeCases:
    """Null safety and boundary behavior."""

    def test_none_input_returns_none(self):
        assert strip_mal_attribution(None) is None

    def test_empty_string_returns_empty_string(self):
        assert strip_mal_attribution("") == ""

    def test_only_whitespace_normalized_to_empty(self):
        """Whitespace-only input normalizes to empty — trailing-whitespace
        trim is part of the display-ready contract. (Degenerate in
        practice; no vault synopsis is whitespace-only.)"""
        assert strip_mal_attribution("   ") == ""

    def test_only_attribution_tag_returns_empty(self):
        """Degenerate: synopsis is literally just the tag."""
        assert strip_mal_attribution("[Written by MAL Rewrite]") == ""

    def test_clean_synopsis_unchanged(self):
        text = "A clean synopsis with no trailing attribution of any kind."
        assert strip_mal_attribution(text) == text

    def test_strips_trailing_whitespace_after_removal(self):
        """After the tag is removed, any residual trailing whitespace
        from the original separator should also be gone — the result
        should be ready to display."""
        result = strip_mal_attribution("Body text.  \t\n  [Written by MAL Rewrite]")
        assert result == "Body text."
