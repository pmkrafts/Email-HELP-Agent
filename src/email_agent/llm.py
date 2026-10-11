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


@dataclass
class SystemOneClient:
    """Jev / System One evaluator: POST state + typed questions, get typed answers back.

    Endpoint: `{base_url}/systemone` (OpenCode Zen: https://opencode.ai/zen/v1, key from OpenCode;
    TypeSafe direct: https://api.typesafe.ai/v1). Not an OpenAI-compatible chat API — it evaluates
    questions in parallel and returns values, probabilities and confidences.
    """

    api_key: str
    model: str = "jev-1.13"
    base_url: str = "https://opencode.ai/zen/v1"
    timeout: float = 30.0
    max_retries: int = 2

    def evaluate(self, state: str, questions: dict) -> dict:
        payload = {"model": self.model, "state": state, "questions": questions}
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/systemone",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:200]
                except Exception:  # noqa: BLE001
                    pass
                last_error = LLMError(f"HTTP {exc.code}: {detail}")
                if exc.code in (400, 401, 403, 404, 422):
                    break
            except (urllib.error.URLError, ValueError, TimeoutError) as exc:
                last_error = exc
            time.sleep(1.5 * (attempt + 1))
        raise LLMError(f"System One call failed after {self.max_retries + 1} attempts: {last_error}")


def load_prompt(prompts_dir: str | Path, name: str) -> str:
    path = Path(prompts_dir) / f"{name}.md"
    if not path.exists():
        raise LLMError(f"missing prompt template: {path}")
    return path.read_text(encoding="utf-8")


def get_client(provider: str, api_key: str, model: str, base_url: str) -> LLMClient | None:
    """Primary channel: any named provider works as long as it is OpenAI-compatible.

    Examples: `openai` (api.openai.com), `jev` (the jev-1.13 gateway), a self-hosted gateway name.
    """
    if provider and provider != "none" and api_key and base_url:
        return LLMClient(api_key=api_key, model=model, base_url=base_url)
    return None
