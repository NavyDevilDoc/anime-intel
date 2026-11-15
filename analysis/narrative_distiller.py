"""Ingestion-time narrative distiller (Level 3 signal).

Takes a show's episode synopses and produces the structured `NarrativeData`
blob (plot arcs, character dynamics, archetypes, emotional trajectory,
key scenarios, tone shifts) that the ingestor writes to
`anime_info.narrative_json`.

Companion to `analysis.sentiment_distiller`, which extracts thematic vibes
and audience consensus from reviews. The two distillers produce
complementary, non-overlapping signal — one is about what happens in the
plot, the other is about how the audience felt about it.
"""
import json
import logging

from pydantic import BaseModel, Field

from analysis import gemini_client

logger = logging.getLogger(__name__)


# --- SCHEMA DEFINITION ---

class NarrativeData(BaseModel):
    narrative_arcs: list[str] = Field(
        description="3-5 key plot progressions described in plain, "
        "conversational English (e.g., 'protagonist is rejected by love "
        "interest and decides to move on', 'former love interest becomes "
        "obsessively fixated on winning protagonist back')."
    )
    character_dynamics: list[str] = Field(
        description="2-4 relationship patterns between characters "
        "(e.g., 'unrequited love reversal', 'mentor betrayal', "
        "'rivals who develop mutual respect')."
    )
    character_archetypes: list[str] = Field(
        description="2-3 protagonist and key character personality types "
        "described in plain language (e.g., 'cynical loner who sees through "
        "social facades', 'outwardly intimidating but secretly kind-hearted', "
        "'overpowered protagonist who hides their strength')."
    )
    emotional_trajectory: str = Field(
        description="The show's emotional arc as a sequence of emotions "
        "(e.g., 'painful -> hopeful -> tense -> cathartic'). "
        "Use emotion words, NOT genre labels."
    )
    key_scenarios: list[str] = Field(
        description="2-4 specific plot setups or situations that drive "
        "the story (e.g., 'school transfer after rejection', 'tournament "
        "with life-or-death stakes', 'time loop where each cycle reveals "
        "new information')."
    )
    tone_shifts: str = Field(
        description="Major tonal changes if any "
        "(e.g., 'lighthearted comedy -> psychological thriller', "
        "'none - consistently dark throughout')."
    )


class NarrativeDistiller:
    def __init__(self, api_key):
        """Initialize the narrative distiller with a Gemini client."""
        self.client = gemini_client.make_client(api_key)
        self.model_id = "gemini-2.5-flash-lite"  # PR #87: 2.0-flash retired (404 NOT_FOUND); lite is ~3x cheaper than 2.5-flash

    def distill_narrative(self, title, episode_synopses, mal_synopsis=None):
        """
        Distills structured narrative data from episode synopses.

        Args:
            title: Show title (for context/logging)
            episode_synopses: List of dicts with 'number', 'title', 'synopsis'
            mal_synopsis: Optional series-level synopsis (background context only)

        Returns:
            dict with NarrativeData fields, or None on failure
        """
        # Build the two-tier input
        primary_source = self._build_primary_source(episode_synopses)

        if not primary_source.strip():
            logger.warning(f"No episode synopsis text for {title}, skipping.")
            return None

        background = ""
        if mal_synopsis and mal_synopsis.strip():
            background = (
                f"\nBACKGROUND CONTEXT ONLY (use for framing, "
                f"NOT as a source of narrative detail):\n"
                f"Series premise: {mal_synopsis.strip()}\n"
            )

        prompt = f"""Act as a Narrative Intelligence Analyst. Extract the plot structure and character dynamics for: {title}.

PRIMARY SOURCE (extract all narrative detail from this):
{primary_source}
{background}
TASK — Extract the following from the PRIMARY SOURCE:

1. narrative_arcs: 3-5 key plot progressions. Describe what HAPPENS, not what the show FEELS LIKE.
   GOOD: "protagonist is rejected by childhood friend and transfers to a new school"
   BAD: "a bittersweet coming-of-age journey" (this is a theme, not a plot event)

2. character_dynamics: 2-4 relationship patterns between characters.
   GOOD: "rivals who secretly push each other to improve"
   BAD: "complex interpersonal relationships" (too vague)

3. character_archetypes: 2-3 personality types for the protagonist and key characters.
   Describe WHO they are as people, not their role in the story.
   GOOD: "cynical loner who sees through social facades but secretly craves connection"
   BAD: "male protagonist" or "shounen hero" (these are roles, not personalities)

4. emotional_trajectory: The emotional arc as a sequence of EMOTIONS, not genre labels.
   GOOD: "hopeful -> anxious -> devastating -> quietly triumphant"
   BAD: "Drama, Tragedy, Romance" (these are genres, not emotions)

5. key_scenarios: 2-4 specific plot setups that drive the story.
   GOOD: "trapped in a death game where players must betray allies to survive"
   BAD: "action-packed adventure" (too generic)

6. tone_shifts: Major tonal changes, or "none" if consistent throughout.
   GOOD: "lighthearted school comedy -> psychological survival horror after episode 3"
   BAD: "dramatic" (not a shift)

Write everything in plain, conversational English — as if describing the plot to a friend deciding whether to watch.
Do NOT reference genres, themes, or audience reception. Focus ONLY on what happens in the story and who the characters are."""

        try:
            response = gemini_client.generate_content(
                self.client,
                model=self.model_id,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": NarrativeData,
                },
            )

            if response.parsed:
                return response.parsed.model_dump()
            return json.loads(response.text.strip())

        except Exception as e:  # noqa: BLE001 — ingestion-time helper; per-show distillation failure logs and returns None so the ingestor skips this show without crashing the batch
            logger.error(f"Narrative Distillation Error for {title}: {e}")
            return None

    def _build_primary_source(self, episode_synopses):
        """Format episode synopses into the primary source block."""
        lines = []
        for ep in episode_synopses:
            synopsis = ep.get("synopsis", "").strip()
            if not synopsis:
                continue
            ep_num = ep.get("number", "?")
            ep_title = ep.get("title", "")
            header = f"Episode {ep_num}"
            if ep_title:
                header += f" - {ep_title}"
            lines.append(f"{header}: {synopsis}")

        return "\n".join(lines)
