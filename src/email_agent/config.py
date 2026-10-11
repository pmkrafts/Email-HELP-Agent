"""Settings: env + CLI flags + rules config loading."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"


@dataclass
class Settings:
    root: Path
    db_path: Path
    rules: dict[str, Any]
    prompts_dir: Path
    llm_provider: str = "none"  # none | openai
    llm_model: str = DEFAULT_MODEL
    llm_base_url: str = DEFAULT_BASE_URL
    llm_api_key: str = field(default="", repr=False)
    max_rewrites: int = 2

    @property
    def review_confidence_threshold(self) -> float:
        return float(self.rules["thresholds"]["human_review_confidence"])


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_rules(path: Path | None = None) -> dict[str, Any]:
    rules_path = path or project_root() / "config" / "rules.json"
    with rules_path.open(encoding="utf-8") as fh:
        return json.load(fh)


def load_settings(args: Any | None = None) -> Settings:
    root = project_root()
    rules = load_rules()
    llm_provider = getattr(args, "llm", None) or os.environ.get("EMAIL_AGENT_LLM", "none")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if llm_provider == "openai" and not api_key:
        llm_provider = "none"
    settings = Settings(
        root=root,
        db_path=Path(getattr(args, "db", None) or root / "storage" / "agent.db"),
        rules=rules,
        prompts_dir=root / "config" / "prompts",
        llm_provider=llm_provider,
        llm_model=os.environ.get("EMAIL_AGENT_MODEL", DEFAULT_MODEL),
        llm_base_url=os.environ.get("EMAIL_AGENT_BASE_URL", DEFAULT_BASE_URL),
        llm_api_key=api_key,
        max_rewrites=int(rules["thresholds"]["max_rewrite_loops"]),
    )
    return settings
