"""LLM prompt templates for the Phase 2 rerankers and DNA triangulator.

Five templates, formatted at call time by `analysis.recommendation_engine`:

- FUSION_RERANKER_PROMPT     — default vibe / freeform query path
- CONSTRAINT_RERANKER_PROMPT — numeric-constraint query path
- DNA_RERANKER_PROMPT        — Shows Like This / centroid rerank
- NARRATIVE_RERANKER_PROMPT  — plot-pattern / character-archetype path
- TRIANGULATION_PROMPT       — DNA intersection-summary builder

Prompt text is loaded from environment variables at startup (production /
Railway) with a fallback to gitignored local files in
`analysis/prompts_private/` (local development). The prompt text itself
is never committed to the repository.

To set up locally:
  Prompt text files already exist at analysis/prompts_private/*.txt —
  they are gitignored and will not be committed.

To set up on Railway:
  Add five multi-line environment variables:
    FUSION_RERANKER_PROMPT, CONSTRAINT_RERANKER_PROMPT,
    DNA_RERANKER_PROMPT, NARRATIVE_RERANKER_PROMPT, TRIANGULATION_PROMPT
  Paste the full prompt text as the value for each.

The four rerankers deliberately maintain separate prompt bodies rather than
sharing common blocks. Prompt text is load-bearing for LLM behavior, and
DRY'ing risks silently drifting one path when only another is intended.
Any shared refactor should go through the eval regression suite.
"""

import os
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts_private"


def _load_prompt(env_var: str, local_filename: str) -> str:
    """Load a prompt from an env var (production) or a local file (dev).

    Resolution order:
      1. Environment variable ``env_var`` — used in Railway deployments.
      2. ``analysis/prompts_private/<local_filename>`` — gitignored local file.

    Raises RuntimeError if neither source is available, with a clear
    message pointing to the setup instructions above.
    """
    if val := os.environ.get(env_var, "").strip():
        return val
    local_path = _PROMPTS_DIR / local_filename
    if local_path.exists():
        return local_path.read_text(encoding="utf-8")
    raise RuntimeError(
        f"Prompt '{env_var}' not found.\n"
        f"  Production: set the {env_var} environment variable in Railway.\n"
        f"  Local dev:  create analysis/prompts_private/{local_filename} with the prompt text."
    )


FUSION_RERANKER_PROMPT     = _load_prompt("FUSION_RERANKER_PROMPT",     "fusion.txt")
CONSTRAINT_RERANKER_PROMPT = _load_prompt("CONSTRAINT_RERANKER_PROMPT", "constraint.txt")
DNA_RERANKER_PROMPT        = _load_prompt("DNA_RERANKER_PROMPT",        "dna.txt")
NARRATIVE_RERANKER_PROMPT  = _load_prompt("NARRATIVE_RERANKER_PROMPT",  "narrative.txt")
TRIANGULATION_PROMPT       = _load_prompt("TRIANGULATION_PROMPT",       "triangulation.txt")
