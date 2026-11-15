"""Utilities for cleaning MAL-sourced synopsis text.

MAL embeds trailing attribution tags on ~79% of synopses in the vault
across 399 distinct variants, dominated by `[Written by MAL Rewrite]`
(3,112) and `(Source: ...)` (~1,900 across 300+ source variants). The
tags are presentation debris — the global footer attribution to
MyAnimeList already covers the data-source story, and per-synopsis
trailing tags fight the clean reading surface on the full-profile page.

Strategy: strict keyword whitelist regex. Only strip trailing tags
whose opener matches `Source` / `Written by` / `Note` (case-insensitive).
Legitimate parenthetical content endings — subtitles, context notes,
runtime like `(3:07)`, translations — are preserved because they don't
start with those keywords. In a 5,033-sample probe, this strategy had
zero false positives on the 15 non-attribution trailing tags observed.

Iterates to handle the 7 vault cases where two attribution tags are
chained at the end (e.g., `(Source: ANN) [Written by MAL Rewrite]`).

Typo cases (`Soure`, `Sorce`, `Souce`, `Writtten`, orphan `(Official
Site, translated)` — 5 shows total) are handled separately via a
one-time targeted UPDATE; see `tools/strip_synopsis_typo_tails.py`.
"""

import re

_ATTRIBUTION_RE = re.compile(
    r"\s*[\[\(]\s*(?:Source|Written\s+by|Note)\b[^\[\(\]\)]*[\]\)]\s*$",
    re.IGNORECASE,
)


def strip_mal_attribution(synopsis):
    """Strip trailing MAL attribution tags from a synopsis string.

    Preserves None and empty inputs unchanged. Iterates until stable so
    chained attribution tags both get removed. Non-attribution trailing
    parentheticals (subtitles, context notes, etc.) are preserved
    because the regex requires one of the keyword openers.
    """
    if not synopsis:
        return synopsis
    prev = None
    while prev != synopsis:
        prev = synopsis
        synopsis = _ATTRIBUTION_RE.sub("", synopsis).rstrip()
    return synopsis
