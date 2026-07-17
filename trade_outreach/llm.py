from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request

from .config import Settings
from .prompts import personalization_prompt


def cache_key(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def ollama_generate(url: str, model: str, prompt: str, timeout: int = 90) -> str:
    endpoint = url.rstrip("/") + "/api/generate"
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    return str(data.get("response", "")).strip()


def deepseek_generate(
    settings: Settings,
    prompt: str,
    timeout: int = 90,
    max_tokens: int = 120,
    system_prompt: str = "You write concise, factual B2B outreach personalization for supplier introductions.",
    json_mode: bool = False,
) -> str:
    if not settings.deepseek_api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")

    endpoint = settings.deepseek_base_url.rstrip("/") + "/chat/completions"
    payload_data = {
        "model": settings.deepseek_model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "thinking": {"type": "disabled"},
    }
    if json_mode:
        payload_data["response_format"] = {"type": "json_object"}
    payload = json.dumps(payload_data).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.deepseek_api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"DeepSeek API HTTP {exc.code}: {body[:500]}") from exc
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"DeepSeek returned no choices: {data}")
    content = str((choices[0].get("message") or {}).get("content") or "").strip()
    if not content:
        raise RuntimeError(f"DeepSeek returned empty content: {data}")
    return content


def generate_text(
    settings: Settings,
    prompt: str,
    max_tokens: int = 120,
    system_prompt: str = "You write concise, factual B2B outreach personalization for supplier introductions.",
    json_mode: bool = False,
) -> str:
    if settings.llm_provider == "deepseek":
        return deepseek_generate(
            settings,
            prompt,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            json_mode=json_mode,
        )
    if settings.llm_provider == "ollama":
        return ollama_generate(settings.ollama_url, settings.ollama_model, prompt)
    raise RuntimeError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")
