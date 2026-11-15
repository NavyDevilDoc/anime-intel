"""Ingestion-time review distiller.

Takes a show's reviews + synopsis and produces the structured
`ConsensusData` blob (thematic vibe tags, pros/cons, controversy score,
consensus summary) that the ingestor writes to `anime_info.consensus_json`.
Companion to `analysis.narrative_distiller`, which extracts plot/character
signal from episode synopses.
"""
import json
import logging

from pydantic import BaseModel, Field

from analysis import gemini_client

logger = logging.getLogger(__name__)

# --- SCHEMA DEFINITION ---
class ConsensusData(BaseModel):
    thematic_vibe: str = Field(description="Extract 3 specific tags (e.g., 'Grimdark', 'Philosophical').")
    pros: list[str] = Field(description="Identify high-signal positive critiques about production and writing.")
    cons: list[str] = Field(description="Identify high-signal negative critiques about production and writing.")
    controversy_score: int = Field(description="Scale 1-10 (how polarized is the audience?).")
    consensus_summary: str = Field(description="A 2-4 sentence executive summary of the audience mood.")

class ReviewDistiller:
    def __init__(self, api_key):
        """Initialize the review distiller with a Gemini client."""
        self.client = gemini_client.make_client(api_key)
        self.model_id = "gemini-2.5-flash-lite"  # PR #87: 2.0-flash retired (404 NOT_FOUND); lite is ~3x cheaper than 2.5-flash

    async def distill_sentiment(self, context_data):
        """Distill a show's reviews + synopsis into `ConsensusData`.

        Receives `context_data` with title / synopsis / reviews, packs it
        into a corpus, calls Gemini with the ConsensusData schema, and
        returns the parsed dict (or None on distillation failure).
        """
        title = context_data.get('title', 'Unknown')
        synopsis = context_data.get('synopsis', 'No synopsis available.')
        reviews = context_data.get('reviews', [])

        # Build a dense 'Intelligence Packet' for Gemini
        corpus = f"--- SHOW: {title} ---\nSYNOPSIS: {synopsis}\n\nREVIEWS:\n"
        for i, review in enumerate(reviews):
            # Expanded truncation to 5000 chars to maximize the 1M token window
            corpus += f"Review {i+1}: {review[:5000]}\n\n"

        prompt = f"""
        Act as a Media Intelligence Analyst. Analyze the audience corpus for: {title}.
        Focus on identifying the 'Emotional Signature' and 'Narrative Quality'.

        CORPUS:
        {corpus}

        TASK:
        1. thematic_vibe: Extract exactly 3 comma-separated genre/mood tags (e.g., 'Grimdark, Philosophical, Coming-of-Age'). Use standard genre vocabulary when possible (Action, Romance, Mecha, Isekai, Slice of Life, Psychological, Horror, Comedy, Drama, Sports, Fantasy, Sci-Fi, Mystery, Thriller, Supernatural).
        2. pros/cons: Identify high-signal critiques about production and writing.
        3. controversy_score: Scale 1-10 calibrated as follows:
           - 1-2: Near-universal agreement (e.g., widely beloved classics or universally panned shows)
           - 3-4: Mild disagreement on secondary aspects (e.g., pacing complaints in an otherwise praised show)
           - 5-6: Genuine split opinions on core elements (e.g., divisive ending, love-it-or-hate-it art style)
           - 7-8: Strong polarization where fans and detractors are both vocal (e.g., controversial plot twists, tone shifts)
           - 9-10: Culture-war level divide where the show itself is a litmus test (e.g., Redo of Healer, School Days)
        4. consensus_summary: A 2-4 sentence executive summary of the audience mood.
        """

        try:
            # Native SDK Structured Output using True Async
            response = await gemini_client.generate_content_async(
                self.client,
                model=self.model_id,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": ConsensusData,
                }
            )

            # The SDK safely parses the Pydantic object
            if response.parsed:
                return response.parsed.model_dump()
            # Fallback to standard json loading if parsed is somehow empty but text isn't
            return json.loads(response.text.strip())

        except Exception as e:  # noqa: BLE001 — ingestion-time helper; per-show distillation failure logs and returns None so the ingestor skips this show without crashing the batch
            logger.error(f"AI Distillation Error for {title}: {e}")
            return None

    async def distill_sentiment_rolling(self, context_data, prior_consensus):
        """Weekly in-season re-distillation with rolling context.

        Like distill_sentiment(), but includes last week's consensus as
        context so Gemini reasons about *trajectory*, not just the current
        review corpus. The consensus_summary MUST reference the direction
        of change — this output is written to airing_sentiment_log and
        surfaced on the Season Tracker, never to anime_info.consensus_json
        (which keeps the neutral ingest-time record for the Encyclopedia).

        prior_consensus: dict with keys thematic_vibe / consensus_summary /
            controversy_score from the most recent airing_sentiment_log row,
            or current anime_info.consensus_json. If None, falls back to
            the standard distill_sentiment() prompt (first-week behaviour).
        """
        title = context_data.get("title", "Unknown")
        synopsis = context_data.get("synopsis", "No synopsis available.")
        reviews = context_data.get("reviews", [])

        corpus = f"--- SHOW: {title} ---\nSYNOPSIS: {synopsis}\n\nREVIEWS:\n"
        for i, review in enumerate(reviews):
            corpus += f"Review {i+1}: {review[:5000]}\n\n"

        if prior_consensus:
            prior_vibe = prior_consensus.get("thematic_vibe", "Unknown")
            prior_summary = prior_consensus.get("consensus_summary", "No prior summary.")
            prior_controversy = prior_consensus.get("controversy_score", "N/A")
            prior_block = f"""PRIOR WEEK CONTEXT:
  Thematic vibe: {prior_vibe}
  Overall mood: {prior_summary}
  Controversy score: {prior_controversy}/10

"""
            trajectory_instruction = (
                "Your consensus_summary MUST reference the trajectory — e.g. "
                "'Sentiment is rising as episode 6 delivered on its promises...' "
                "or 'Reception has cooled following a divisive plot twist in "
                "episode 8...' Do not write a neutral timeless summary; "
                "this is a weekly trajectory snapshot."
            )
        else:
            prior_block = ""
            trajectory_instruction = (
                "This is the first weekly snapshot for this show. Write a "
                "consensus_summary that captures the current audience mood "
                "based on these early reviews."
            )

        prompt = f"""
Act as a Media Intelligence Analyst tracking in-season sentiment evolution.

{prior_block}CURRENT REVIEWS:
{corpus}

TASK — Analyze community reception for: {title}
1. thematic_vibe: 3 comma-separated genre/mood tags. Keep consistent with
   prior week unless the show has fundamentally shifted tone.
2. pros/cons: Based on this week's reviews.
3. controversy_score: Updated 1-10 scale (same calibration as before).
4. consensus_summary: 2-4 sentences. {trajectory_instruction}
"""

        try:
            response = await gemini_client.generate_content_async(
                self.client,
                model=self.model_id,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": ConsensusData,
                },
            )
            if response.parsed:
                return response.parsed.model_dump()
            return json.loads(response.text.strip())

        except Exception as e:  # noqa: BLE001 — same contract as distill_sentiment
            logger.error(f"Rolling distillation error for {title}: {e}")
            return None
