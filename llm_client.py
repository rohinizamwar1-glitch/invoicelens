"""Thin client for any OpenAI-compatible chat endpoint (Gemini free tier by default).

Standard library only. All settings come from environment variables / Replit Secrets.
"""
import json
import os
import urllib.request

PROVIDERS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "gemini-flash-latest"),
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile"),
    "openai": ("https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
}


def config():
    provider = os.getenv("LLM_PROVIDER", "gemini").lower()
    url, model = PROVIDERS.get(provider, PROVIDERS["gemini"])
    return {
        "provider": provider,
        "url": os.getenv("LLM_BASE_URL", url),
        "model": os.getenv("LLM_MODEL", model),
        "key": os.getenv("LLM_API_KEY", ""),
    }


def call_llm(system, user, cfg=None, timeout=25):
    """Returns (reply_text, usage_dict). Raises urllib errors on failure."""
    cfg = cfg or config()
    body = json.dumps({
        "model": cfg["model"],
        "temperature": 0,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(cfg["url"], data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['key']}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"], data.get("usage") or {}
