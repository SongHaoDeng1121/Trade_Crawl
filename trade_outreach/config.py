from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        clean_key = key.strip()
        if clean_key not in os.environ:
            os.environ[clean_key] = value.strip().strip('"').strip("'")


@dataclass(frozen=True)
class Settings:
    db_path: str
    use_llm: bool
    llm_provider: str
    ollama_url: str
    ollama_model: str
    deepseek_api_key: str
    deepseek_base_url: str
    deepseek_model: str
    target_region: str
    company_name: str
    company_address: str
    company_business_summary: str
    company_facebook_url: str
    company_extra_info: str
    mail_from: str
    mail_reply_to: str
    mail_cc: str
    mail_bcc: str
    smtp_host: str
    smtp_port: int
    smtp_security: str
    send_delay_seconds: float
    send_daily_limit: int
    google_places_api_key: str
    google_places_region_code: str
    google_places_language_code: str
    discovery_target_large: int
    discovery_target_medium: int
    discovery_target_small: int
    discovery_target_micro: int
    discovery_no_new_cooldown_rounds: int
    discovery_cooldown_hours: float
    discovery_cities_file: str
    auto_discovery_interval_seconds: float
    auto_processing_interval_seconds: float
    auto_send_idle_seconds: float
    smtp_user: str
    smtp_password: str


def get_settings() -> Settings:
    load_dotenv()
    return Settings(
        db_path=os.getenv("OUTREACH_DB", "data/outreach.sqlite3"),
        use_llm=os.getenv("USE_LLM", "0").lower() in {"1", "true", "yes"},
        llm_provider=os.getenv("LLM_PROVIDER", "ollama").lower(),
        ollama_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "llama3.1:8b"),
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", ""),
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        target_region=os.getenv("TARGET_REGION", "Europe"),
        company_name=os.getenv("COMPANY_NAME", "Your Company"),
        company_address=os.getenv("COMPANY_ADDRESS", "Your physical mailing address"),
        company_business_summary=os.getenv(
            "COMPANY_BUSINESS_SUMMARY",
            "We supply products for distributors and industrial buyers.",
        ),
        company_facebook_url=os.getenv("COMPANY_FACEBOOK_URL", ""),
        company_extra_info=os.getenv("COMPANY_EXTRA_INFO", ""),
        mail_from=os.getenv("MAIL_FROM", "sales@example.com"),
        mail_reply_to=os.getenv("MAIL_REPLY_TO", "sales@example.com"),
        mail_cc=os.getenv("MAIL_CC", ""),
        mail_bcc=os.getenv("MAIL_BCC", ""),
        smtp_host=os.getenv("SMTP_HOST", ""),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_security=os.getenv("SMTP_SECURITY", "starttls").lower(),
        send_delay_seconds=float(os.getenv("SEND_DELAY_SECONDS", "800")),
        send_daily_limit=int(os.getenv("SEND_DAILY_LIMIT", "100")),
        google_places_api_key=os.getenv("GOOGLE_PLACES_API_KEY", ""),
        google_places_region_code=os.getenv("GOOGLE_PLACES_REGION_CODE", "GB"),
        google_places_language_code=os.getenv("GOOGLE_PLACES_LANGUAGE_CODE", "en"),
        discovery_target_large=int(os.getenv("DISCOVERY_TARGET_LARGE", "60")),
        discovery_target_medium=int(os.getenv("DISCOVERY_TARGET_MEDIUM", "40")),
        discovery_target_small=int(os.getenv("DISCOVERY_TARGET_SMALL", "25")),
        discovery_target_micro=int(os.getenv("DISCOVERY_TARGET_MICRO", "12")),
        discovery_no_new_cooldown_rounds=int(os.getenv("DISCOVERY_NO_NEW_COOLDOWN_ROUNDS", "2")),
        discovery_cooldown_hours=float(os.getenv("DISCOVERY_COOLDOWN_HOURS", "24")),
        discovery_cities_file=os.getenv("DISCOVERY_CITIES_FILE", ""),
        auto_discovery_interval_seconds=float(os.getenv("AUTO_DISCOVERY_INTERVAL_SECONDS", "900")),
        auto_processing_interval_seconds=float(os.getenv("AUTO_PROCESSING_INTERVAL_SECONDS", "60")),
        auto_send_idle_seconds=float(os.getenv("AUTO_SEND_IDLE_SECONDS", "300")),
        smtp_user=os.getenv("SMTP_USER", ""),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
    )
