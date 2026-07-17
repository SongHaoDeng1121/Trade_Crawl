from __future__ import annotations


def personalization_prompt(company_name: str, page_text: str, target_market: str, offer_summary: str = "") -> str:
    snippet = page_text[:2500]
    offer = offer_summary.strip() or "products for distributors and industrial buyers"
    return f"""
You write one concise first sentence for a B2B email.
Target company: {company_name or "Unknown company"}
Target company market/region: {target_market}
Our offer/business summary: {offer}
Their website text:
{snippet}

Rules:
- One sentence only.
- Mention a concrete business signal from their site if available.
- No hype.
- Do not promise outcomes.
- Do not say we can help them sell, move inventory, or grow faster.
- Frame the sentence around supplier fit, sourcing, quoting, or vendor opportunities.
- Use our actual offer/business summary. Do not replace it with another product category.
- Never use placeholders.
- Never output square brackets.
- Never write phrases like "[your product]", "[product/service]", "[specific category]", or "e.g.".
- Do not invent facts.
- Output only the sentence.
"""
