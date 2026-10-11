"""LLM client seam: OpenAI-compatible chat completions over stdlib urllib.

Disabled by default; enable with `--llm openai` (or EMAIL_AGENT_LLM=openai) plus OPENAI_API_KEY.
The deterministic rules pipeline never requires this module — it exists so classifications and
drafts can be *refined* by a model without changing the pipeline shape.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class LLMError(RuntimeError):
    pass


@dataclass
class LLMClient:
    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout: float = 45.0
    max_retries: int = 2

    def chat_json(self, system: str, user: str, temperature: float = 0.2) -> dict:
        payload = {
            "model": self.model,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                content = data["choices"][0]["message"]["content"]
                return json.loads(content)
            except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError, TimeoutError) as exc:
                last_error = exc
                if isinstance(exc, urllib.error.HTTPError) and exc.code in (400, 401, 403, 404):
                    break
                time.sleep(1.5 * (attempt + 1))
        raise LLMError(f"LLM call failed after {self.max_retries + 1} attempts: {last_error}")


def load_prompt(prompts_dir: str | Path, name: str) -> str:
    path = Path(prompts_dir) / f"{name}.md"
    if not path.exists():
        raise LLMError(f"missing prompt template: {path}")
    return path.read_text(encoding="utf-8")


def get_client(provider: str, api_key: str, model: str, base_url: str) -> LLMClient | None:
    if provider == "openai" and api_key:
        return LLMClient(api_key=api_key, model=model, base_url=base_url)
    return None


def get_judge_client(provider: str, api_key: str, model: str, base_url: str) -> LLMClient | None:
    """JEV / cheap-judge channel: any OpenAI-compatible endpoint (JEV, local model, cheap API tier).

    Kept deliberately separate from the primary client so the two tiers can differ in provider,
    model and key — the whole point of the cost ladder.
    """
    if provider and provider != "none" and api_key and base_url:
        return LLMClient(api_key=api_key, model=model or "jev", base_url=base_url, max_retries=1, timeout=20.0)
    return None
