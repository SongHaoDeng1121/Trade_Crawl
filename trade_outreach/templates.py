from __future__ import annotations

from .config import Settings


def subject_for(company_name: str | None) -> str:
    if company_name:
        return f"Business inquiry for {company_name}"
    return "Business inquiry"


def draft_body(settings: Settings, company_name: str | None, opening: str, unsubscribe_email: str) -> str:
    greeting = "Hello,"
    intro = opening.strip() if opening else "I saw your company works in a relevant industrial or distribution market."
    company_intro = settings.company_name.rstrip(".")
    business_summary = settings.company_business_summary.strip() or "We supply products for distributors and industrial buyers."
    extra_info = settings.company_extra_info.strip()
    facebook_line = f"Facebook: {settings.company_facebook_url}\n" if settings.company_facebook_url else ""
    extra_line = f"{extra_info}\n" if extra_info else ""
    info_block = "\n".join(line for line in [facebook_line.rstrip(), extra_line.rstrip()] if line)
    info_block = f"\n{info_block}\n" if info_block else "\n"
    return f"""{greeting}

{intro}

I am reaching out from {company_intro}. {business_summary}

Would you be the right person to contact about vendor opportunities or current sourcing needs?

Best regards,
{settings.company_name}
{info_block}
Mailing address:
{settings.company_address}

To opt out, reply with "unsubscribe" or email {unsubscribe_email}.
"""
