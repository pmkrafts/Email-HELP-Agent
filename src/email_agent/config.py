"""Settings: env + CLI flags + rules config loading."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_JUDGE_MODEL = "jev-1.13"  # the JEV model id (display name "Jev 1.13")
DEFAULT_JUDGE_BASE_URL = "https://opencode.ai/zen/v1"  # OpenCode Zen gateway; Jev lives at /systemone


@dataclass
class Settings:
    root: Path
    db_path: Path
    rules: dict[str, Any]
    prompts_dir: Path
    llm_provider: str = "none"  # none | openai (primary/refinement model)
    llm_model: str = DEFAULT_MODEL
    llm_base_url: str = DEFAULT_BASE_URL
    llm_api_key: str = field(default="", repr=False)
    judge_provider: str = "none"  # none | jev | any OpenAI-compatible endpoint
    judge_api: str = "auto"  # auto | systemone | chat
    judge_model: str = ""
    judge_base_url: str = ""
    judge_api_key: str = field(default="", repr=False)
    refine_mode: str = "always"  # always | escalations_only (primary reserved for adjudication)
    max_rewrites: int = 2

    @property
    def review_confidence_threshold(self) -> float:
        return float(self.rules["thresholds"]["human_review_confidence"])


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """Minimal `.env` loader (stdlib only). Real environment variables always win.

    Handles `KEY=VALUE`, comments (`#` full-line or trailing ` #`), quoted values, CRLF and blanks.
    """
    env_path = path or project_root() / ".env"
    applied: dict[str, str] = {}
    if not env_path.exists():
        return applied
    for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.split(" #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


def load_rules(path: Path | None = None) -> dict[str, Any]:
    rules_path = path or project_root() / "config" / "rules.json"
    with rules_path.open(encoding="utf-8") as fh:
        return json.load(fh)


def load_settings(args: Any | None = None) -> Settings:
    root = project_root()
    # `.env` keys are applied unless already exported; EMAIL_AGENT_ENV_FILE overrides the path
    # (point it at a non-existent file to disable .env loading entirely, e.g. in tests/CI).
    env_override = os.environ.get("EMAIL_AGENT_ENV_FILE")
    load_dotenv(Path(env_override) if env_override else root / ".env")
    rules = load_rules()
    llm_provider = getattr(args, "llm", None) or os.environ.get("EMAIL_AGENT_LLM", "none") or "none"
    api_key = os.environ.get("EMAIL_AGENT_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    if llm_provider != "none" and not api_key:
        llm_provider = "none"  # any provider (openai / jev / gateway name) needs a key
    judge_provider = (
        getattr(args, "judge", None)
        or os.environ.get("EMAIL_AGENT_JUDGE_LLM", os.environ.get("EMAIL_AGENT_JUDGE", "none"))
        or "none"
    )
    judge_base_url = os.environ.get("EMAIL_AGENT_JUDGE_BASE_URL") or DEFAULT_JUDGE_BASE_URL
    judge_api_key = (
        os.environ.get("EMAIL_AGENT_JUDGE_API_KEY")  # explicit judge key
        or os.environ.get("OPENCODE_API_KEY")        # OpenCode Zen key (Jev gateway)
        or api_key                                   # shared gateway key
    )
    judge_api = os.environ.get("EMAIL_AGENT_JUDGE_API", "auto")
    if judge_api not in {"auto", "systemone", "chat"}:
        judge_api = "auto"
    if judge_provider != "none" and not (judge_base_url and judge_api_key):
        judge_provider = "none"  # judge needs an endpoint + key; silently stays off otherwise
    refine_mode = os.environ.get("EMAIL_AGENT_REFINE_MODE", "always")
    if refine_mode not in {"always", "escalations_only"}:
        refine_mode = "always"
    settings = Settings(
        root=root,
        db_path=Path(getattr(args, "db", None) or root / "storage" / "agent.db"),
        rules=rules,
        prompts_dir=root / "config" / "prompts",
        llm_provider=llm_provider,
        llm_model=os.environ.get("EMAIL_AGENT_MODEL", DEFAULT_MODEL),
        llm_base_url=os.environ.get("EMAIL_AGENT_BASE_URL", DEFAULT_BASE_URL),
        llm_api_key=api_key,
        judge_provider=judge_provider,
        judge_api=judge_api,
        judge_model=os.environ.get("EMAIL_AGENT_JUDGE_MODEL", DEFAULT_JUDGE_MODEL),
        judge_base_url=judge_base_url,
        judge_api_key=judge_api_key,
        refine_mode=refine_mode,
        max_rewrites=int(rules["thresholds"]["max_rewrite_loops"]),
    )
    return settings
