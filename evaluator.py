"""
LLM-based listing evaluator.

Uses Claude's vision capabilities to evaluate listing photos and descriptions
against user-defined aesthetic and quality criteria.
"""

import base64
import io
import json
import logging
from dataclasses import dataclass

import anthropic
import requests

from scraper import Listing

logger = logging.getLogger(__name__)


@dataclass
class EvaluationResult:
    """Result of evaluating a listing against all criteria."""

    scores: dict[str, int]  # criterion name -> score (1-10)
    reasoning: dict[str, str]  # criterion name -> explanation
    passes: bool  # whether all minimum scores are met
    summary: str  # overall summary


def _download_image_as_base64(url: str, max_size_mb: float = 5.0) -> tuple[str, str] | None:
    """Download an image and return (base64_data, media_type) or None on failure."""
    try:
        resp = requests.get(url, timeout=10, stream=True)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "image/jpeg")
        if "jpeg" in content_type or "jpg" in content_type:
            media_type = "image/jpeg"
        elif "png" in content_type:
            media_type = "image/png"
        elif "webp" in content_type:
            media_type = "image/webp"
        elif "gif" in content_type:
            media_type = "image/gif"
        else:
            media_type = "image/jpeg"

        # Read with size limit
        max_bytes = int(max_size_mb * 1024 * 1024)
        data = b""
        for chunk in resp.iter_content(chunk_size=8192):
            data += chunk
            if len(data) > max_bytes:
                logger.debug("Image too large, skipping: %s", url)
                return None

        b64 = base64.standard_b64encode(data).decode("utf-8")
        return b64, media_type

    except Exception:
        logger.debug("Failed to download image: %s", url, exc_info=True)
        return None


def _build_evaluation_prompt(criteria: dict) -> str:
    """Build the evaluation prompt from criteria config."""
    criteria_text = ""
    for name, criterion in criteria.items():
        criteria_text += f"\n### {name.title()}\n"
        criteria_text += f"What to look for: {criterion['description'].strip()}\n"
        criteria_text += f"Minimum acceptable score: {criterion['min_score']}/10\n"

    return f"""You are a real estate listing evaluator. Analyze the listing photos and description below, then score the property on each criterion.

## Evaluation Criteria
{criteria_text}

## Instructions
1. Examine ALL provided photos carefully.
2. Read the listing description for additional context.
3. Score each criterion from 1-10 based on what you can see/infer.
4. If a criterion cannot be evaluated from the available photos (e.g., no yard photos), score it 5 (neutral) and note that it couldn't be assessed.
5. Be honest and specific in your reasoning.

## Response Format
Respond with a JSON object (no markdown code fences) exactly like this:
{{
  "scores": {{
    "criterion_name": <score 1-10>,
    ...
  }},
  "reasoning": {{
    "criterion_name": "<1-2 sentence explanation>",
    ...
  }},
  "summary": "<2-3 sentence overall assessment>"
}}

Only include the criteria listed above. Use the exact criterion names as keys."""


def evaluate_listing(
    listing: Listing,
    config: dict,
) -> EvaluationResult | None:
    """
    Evaluate a single listing using Claude's vision capabilities.

    Args:
        listing: The listing to evaluate.
        config: The full app config dict.

    Returns:
        EvaluationResult or None if evaluation fails.
    """
    eval_config = config.get("evaluation", {})
    if not eval_config.get("enabled", False):
        logger.debug("LLM evaluation disabled, skipping")
        return None

    criteria = eval_config.get("criteria", {})
    if not criteria:
        logger.warning("No evaluation criteria defined")
        return None

    model = eval_config.get("model", "claude-sonnet-4-6")
    max_photos = eval_config.get("max_photos", 6)

    # Build message content with photos
    content: list[dict] = []

    # Add listing text info
    listing_info = (
        f"**Address:** {listing.address}\n"
        f"**Price:** ${listing.price:,}\n"
        f"**Beds/Baths:** {listing.beds}bd / {listing.baths}ba\n"
        f"**Sqft:** {listing.sqft or 'N/A'}\n"
        f"**Year Built:** {listing.year_built or 'N/A'}\n"
        f"**Home Type:** {listing.home_type}\n"
    )
    if listing.description:
        listing_info += f"\n**Description:** {listing.description[:2000]}"

    content.append({"type": "text", "text": f"## Listing Information\n{listing_info}"})

    # Add photos
    photo_urls = listing.photo_urls[:max_photos]
    photos_added = 0

    for url in photo_urls:
        result = _download_image_as_base64(url)
        if result:
            b64_data, media_type = result
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": b64_data,
                },
            })
            photos_added += 1

    if photos_added == 0:
        logger.warning("No photos available for listing %s, skipping evaluation", listing.zpid)
        # Return neutral scores
        return EvaluationResult(
            scores={name: 5 for name in criteria},
            reasoning={name: "No photos available for evaluation" for name in criteria},
            passes=all(5 >= c["min_score"] for c in criteria.values()),
            summary="Could not evaluate - no photos available.",
        )

    logger.info(
        "Evaluating listing %s (%s) with %d photos",
        listing.zpid, listing.address, photos_added,
    )

    # Call Claude API
    client = anthropic.Anthropic()
    prompt = _build_evaluation_prompt(criteria)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        *content,
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )

        response_text = response.content[0].text.strip()

        # Parse JSON response
        # Handle potential markdown code fences
        if response_text.startswith("```"):
            response_text = response_text.split("\n", 1)[1]
            response_text = response_text.rsplit("```", 1)[0]

        result = json.loads(response_text)

        scores = result.get("scores", {})
        reasoning = result.get("reasoning", {})
        summary = result.get("summary", "")

        # Check if all minimum scores are met
        passes = True
        for name, criterion in criteria.items():
            score = scores.get(name, 0)
            if score < criterion["min_score"]:
                passes = False
                break

        return EvaluationResult(
            scores=scores,
            reasoning=reasoning,
            passes=passes,
            summary=summary,
        )

    except json.JSONDecodeError:
        logger.error("Failed to parse LLM response as JSON for listing %s", listing.zpid)
        return None
    except anthropic.APIError:
        logger.error("Anthropic API error evaluating listing %s", listing.zpid, exc_info=True)
        return None


def evaluate_listings(
    listings: list[Listing],
    config: dict,
) -> list[tuple[Listing, EvaluationResult | None]]:
    """
    Evaluate multiple listings.

    Returns list of (listing, evaluation_result) tuples.
    Only listings that pass evaluation (or have no evaluation) are included.
    """
    eval_config = config.get("evaluation", {})
    if not eval_config.get("enabled", False):
        return [(listing, None) for listing in listings]

    results = []
    for listing in listings:
        evaluation = evaluate_listing(listing, config)
        if evaluation is None:
            # Evaluation failed, include listing anyway
            results.append((listing, None))
        elif evaluation.passes:
            results.append((listing, evaluation))
        else:
            logger.info(
                "Listing %s (%s) filtered out by LLM evaluation: scores=%s",
                listing.zpid, listing.address, evaluation.scores,
            )

    return results
