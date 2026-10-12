"""The orchestrator: the pipeline compiled as a LangGraph `StateGraph`.

Control flow lives in the graph; everything below the orchestration layer is shared with the rest of
the package — parser, rules engine, the four agents, storage, retrieval, LLM clients and judge
channels:

    START -> classify -> (spam? -> schedule) -> retrieve -> draft -> review -> (revise loop | finalize)
            finalize -> schedule -> persist -> END

The branches are explicit edges:
  * spam short-circuit        (classify -> schedule, no retrieval/draft/review)
  * rewrite loop              (review -> revise -> review, capped by `max_rewrites`)
  * per-email error isolation (any node failure routes straight to END with `status="error"`)

`langgraph` is the one runtime dependency (it pulls `langchain-core`). Export the topology with
`python email-agent.py graph`; the compiled graph is also checked in as `docs/graph.mmd`.

State is plain dictionaries/TypedDict so that checkpointing works (LangGraph serialises state through
msgpack) — the richer dataclasses are reconstructed inside the nodes.
"""
from __future__ import annotations

import operator
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .agents.classifier import Classifier
from .agents.drafter import Drafter
from .agents.reviewer import Reviewer
from .agents.scheduler import Scheduler
from .analytics import RunLogger
from .config import Settings
from .judges import build_judge
from .llm import LLMClient, get_client
from .models import (
    AgentState,
    Classification,
    ContextChunk,
    DraftResponse,
    ParsedEmail,
    ReviewResult,
    ScheduleDecision,
)
from .parser import parse_dir
from .retrieval import KnowledgeBase
from .storage import Repository


class PipelineState(TypedDict, total=False):
    """Graph state: plain dicts + scalars so a checkpointer can serialise it."""

    email: dict[str, Any]
    classification: dict[str, Any]
    context: list[dict[str, Any]]
    draft: dict[str, Any]
    review: dict[str, Any]
    schedule: dict[str, Any]
    now: str
    attempts: int
    feedback: str
    status: str
    error: str | None
    logs: Annotated[list[dict[str, Any]], operator.add]


# --- dataclass <-> dict bridges (the nodes keep working with the real models) ------------------
def _email_to_dict(email: ParsedEmail) -> dict[str, Any]:
    return email.to_dict()


def _email_from_dict(data: dict[str, Any]) -> ParsedEmail:
    payload = dict(data)
    date = payload.get("date")
    if isinstance(date, str):
        payload["date"] = datetime.fromisoformat(date)
    return ParsedEmail(**payload)


def _from_state(state: PipelineState) -> AgentState:
    agent = AgentState(email=_email_from_dict(state["email"]))
    agent.status = state.get("status", "pending")  # type: ignore[assignment]
    agent.error = state.get("error")
    if state.get("classification"):
        agent.classification = Classification(**state["classification"])
    if state.get("context"):
        agent.context = [ContextChunk(**chunk) for chunk in state["context"]]
    if state.get("draft"):
        agent.draft = DraftResponse(**state["draft"])
    if state.get("review"):
        agent.review = ReviewResult(**state["review"])
    if state.get("schedule"):
        payload = dict(state["schedule"])
        send_at = payload.get("send_at")
        if isinstance(send_at, str):
            payload["send_at"] = datetime.fromisoformat(send_at)
        agent.schedule = ScheduleDecision(**payload)
    agent.logs = list(state.get("logs", []))
    return agent


def _dump(agent: AgentState) -> dict[str, Any]:
    """Serialisable view of the mutable parts of an AgentState (for state updates)."""
    out: dict[str, Any] = {"status": agent.status}
    if agent.error:
        out["error"] = agent.error
    if agent.classification:
        out["classification"] = asdict(agent.classification)
    if agent.context:
        out["context"] = [asdict(chunk) for chunk in agent.context]
    if agent.draft:
        out["draft"] = asdict(agent.draft)
    if agent.review:
        out["review"] = asdict(agent.review)
    if agent.schedule:
        payload = asdict(agent.schedule)
        payload["send_at"] = agent.schedule.send_at.isoformat() if agent.schedule.send_at else None
        out["schedule"] = payload
    return out


class GraphOrchestrator:
    """The pipeline as a compiled LangGraph state graph (one email per invocation)."""

    def __init__(
        self,
        settings: Settings,
        repo: Repository | None = None,
        logger: RunLogger | None = None,
        checkpointer: bool | None = None,
    ):
        self.settings = settings
        self.repo = repo
        self.logger = logger
        self.kb = KnowledgeBase.load(settings.root / "data" / "knowledge_base")
        self.client: LLMClient | None = get_client(
            settings.llm_provider, settings.llm_api_key, settings.llm_model, settings.llm_base_url
        )
        self.judge = build_judge(settings)
        self.classifier = Classifier(settings, self.client, self.judge)
        self.drafter = Drafter(settings, self.kb, self.client)
        self.reviewer = Reviewer(settings, self.judge)
        self.scheduler = Scheduler(settings)
        self._use_checkpointer = settings.graph_checkpoint if checkpointer is None else checkpointer
        self.checkpointer = None
        if self._use_checkpointer:
            from langgraph.checkpoint.memory import InMemorySaver

            self.checkpointer = InMemorySaver()
        self.graph = self._build()

    # -- graph construction --------------------------------------------------
    def _build(self):
        builder = StateGraph(PipelineState)
        builder.add_node("classify", self._node_classify)
        builder.add_node("retrieve", self._node_retrieve)
        builder.add_node("draft", self._node_draft)
        builder.add_node("review", self._node_review)
        builder.add_node("revise", self._node_revise)
        builder.add_node("finalize_review", self._node_finalize_review)
        builder.add_node("schedule", self._node_schedule)
        builder.add_node("persist", self._node_persist)

        builder.add_edge(START, "classify")
        builder.add_conditional_edges(
            "classify", self._after_classify,
            {"retrieve": "retrieve", "schedule": "schedule", "halt": END},
        )
        builder.add_conditional_edges("retrieve", self._after_retrieve, {"draft": "draft", "halt": END})
        builder.add_conditional_edges("draft", self._after_draft, {"review": "review", "halt": END})
        builder.add_conditional_edges(
            "review", self._after_review,
            {"revise": "revise", "finalize_review": "finalize_review", "halt": END},
        )
        builder.add_conditional_edges("revise", self._after_draft, {"review": "review", "halt": END})
        builder.add_conditional_edges("finalize_review", self._after_finalize, {"schedule": "schedule", "halt": END})
        builder.add_conditional_edges("schedule", self._after_schedule, {"persist": "persist", "halt": END})
        builder.add_edge("persist", END)

        if self.checkpointer is not None:
            return builder.compile(checkpointer=self.checkpointer)
        return builder.compile()

    # -- routers -------------------------------------------------------------
    @staticmethod
    def _halted(state: PipelineState) -> bool:
        return state.get("status") == "error"

    def _after_classify(self, state: PipelineState) -> str:
        if self._halted(state):
            return "halt"
        return "schedule" if "spam" in (state.get("classification", {}).get("flags") or []) else "retrieve"

    def _after_retrieve(self, state: PipelineState) -> str:
        return "halt" if self._halted(state) else "draft"

    def _after_draft(self, state: PipelineState) -> str:
        return "halt" if self._halted(state) else "review"

    def _after_review(self, state: PipelineState) -> str:
        if self._halted(state):
            return "halt"
        review = state.get("review") or {}
        can_revise = self.client is not None
        if review.get("status") == "needs_revision" and can_revise and int(state.get("attempts", 1)) < self.settings.max_rewrites:
            return "revise"
        return "finalize_review"

    def _after_finalize(self, state: PipelineState) -> str:
        return "halt" if self._halted(state) else "schedule"

    def _after_schedule(self, state: PipelineState) -> str:
        return "halt" if self._halted(state) else "persist"

    # -- nodes ---------------------------------------------------------------
    def _guard(self, node_name: str, fn):
        """Per-email error isolation: a failing node records the error and routes to END."""

        def wrapped(state: PipelineState) -> dict[str, Any]:
            started = time.perf_counter()
            try:
                update = fn(state)
            except Exception as exc:  # noqa: BLE001 - isolation is the contract
                return {
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "logs": [{"agent": node_name, "event": "error", "error": str(exc)}],
                }
            update.setdefault("logs", [])
            update["logs"] = [*update["logs"]]
            if update["logs"]:
                update["logs"][0].setdefault("latency_ms", int((time.perf_counter() - started) * 1000))
            return update

        return wrapped

    def _node_classify(self, state: PipelineState) -> dict[str, Any]:
        def run(state: PipelineState) -> dict[str, Any]:
            email = _email_from_dict(state["email"])
            agent = AgentState(email=email)
            agent.classification = self.classifier.run(email)
            agent.status = "classified"
            agent.log("classifier", "completed", priority=agent.classification.priority,
                      confidence=agent.classification.confidence, engine=agent.classification.engine,
                      flags=agent.classification.flags, judge=agent.classification.judge_verdict)
            return _dump(agent)

        return self._guard("classifier", run)(state)

    def _node_retrieve(self, state: PipelineState) -> dict[str, Any]:
        def run(state: PipelineState) -> dict[str, Any]:
            agent = _from_state(state)
            assert agent.classification is not None
            agent.context = self.kb.retrieve(f"{agent.email.subject}\n{agent.email.body_text}", top_k=3)
            agent.log("retrieval", "completed", chunks=[chunk.title for chunk in agent.context])
            return _dump(agent)

        return self._guard("retrieval", run)(state)

    def _node_draft(self, state: PipelineState) -> dict[str, Any]:
        def run(state: PipelineState) -> dict[str, Any]:
            agent = _from_state(state)
            agent.draft = self.drafter.run(agent, feedback=state.get("feedback"))
            agent.status = "drafted"
            agent.log("drafter", "completed", confidence=agent.draft.confidence,
                      language=agent.draft.language, version=agent.draft.version)
            return _dump(agent)

        return self._guard("drafter", run)(state)

    def _node_review(self, state: PipelineState) -> dict[str, Any]:
        def run(state: PipelineState) -> dict[str, Any]:
            agent = _from_state(state)
            agent.review = self.reviewer.run(agent)
            agent.status = "reviewed"
            agent.log("reviewer", "completed", status=agent.review.status, issues=agent.review.issues)
            return _dump(agent)

        return self._guard("reviewer", run)(state)

    def _node_revise(self, state: PipelineState) -> dict[str, Any]:
        def run(state: PipelineState) -> dict[str, Any]:
            agent = _from_state(state)
            assert agent.review is not None
            feedback = agent.review.suggested_changes or "; ".join(agent.review.issues)
            agent.log("reviewer", "revision_requested", attempt=int(state.get("attempts", 1)),
                      issues=agent.review.issues)
            agent.draft = self.drafter.run(agent, feedback=feedback)
            agent.log("drafter", "revised", version=agent.draft.version)
            update = _dump(agent)
            update["attempts"] = int(state.get("attempts", 1)) + 1
            update["feedback"] = feedback
            return update

        return self._guard("drafter", run)(state)

    def _node_finalize_review(self, state: PipelineState) -> dict[str, Any]:
        """The terminal review rule: a draft still needing revision is escalated (or flagged)."""
        def run(state: PipelineState) -> dict[str, Any]:
            agent = _from_state(state)
            assert agent.review is not None
            if agent.review.status == "needs_revision":
                if self.client is not None:
                    agent.review.status = "escalate"
                    agent.review.issues.append("max rewrite attempts reached; escalated to human review")
                else:
                    agent.review.issues.append(
                        "offline template drafter cannot auto-revise; enable the GPT tier or edit the draft manually"
                    )
            agent.log("reviewer", "finalized", status=agent.review.status,
                      attempts=int(state.get("attempts", 1)), issues=agent.review.issues)
            return _dump(agent)

        return self._guard("reviewer", run)(state)

    def _node_schedule(self, state: PipelineState) -> dict[str, Any]:
        def run(state: PipelineState) -> dict[str, Any]:
            agent = _from_state(state)
            assert agent.classification is not None
            now = datetime.fromisoformat(state["now"]) if state.get("now") else datetime.now(timezone.utc)
            agent.schedule = self.scheduler.run(agent, now)
            agent.status = "scheduled"
            agent.log("scheduler", "completed", priority=agent.classification.priority,
                      status=agent.schedule.status,
                      send_at=agent.schedule.send_at.isoformat() if agent.schedule.send_at else None)
            return _dump(agent)

        return self._guard("scheduler", run)(state)

    def _node_persist(self, state: PipelineState) -> dict[str, Any]:
        def run(state: PipelineState) -> dict[str, Any]:
            if self.repo is None:
                return {}
            agent = _from_state(state)
            self.repo.save_state(agent)
            return {}

        return self._guard("storage", run)(state)

    # -- public API (mirrors Orchestrator) -----------------------------------
    def process(self, email: ParsedEmail, now: datetime | None = None) -> AgentState:
        now = now or datetime.now(timezone.utc)
        initial: PipelineState = {
            "email": _email_to_dict(email),
            "now": now.isoformat(),
            "attempts": 1,  # matches the sequential engine: one draft is the first attempt
            "status": "pending",
            "logs": [],
        }
        config = {"configurable": {"thread_id": email.message_id}} if self.checkpointer is not None else None
        final = self.graph.invoke(initial, config=config) if config else self.graph.invoke(initial)
        agent = _from_state(final)
        if self.logger is not None:
            self.logger.log(
                "email_processed",
                engine="graph",
                message_id=agent.email.message_id,
                subject=agent.email.subject,
                status=agent.status,
                error=agent.error,
                priority=agent.classification.priority if agent.classification else None,
                confidence=agent.classification.confidence if agent.classification else None,
                review_status=agent.review.status if agent.review else None,
                schedule_status=agent.schedule.status if agent.schedule else None,
                send_at=agent.schedule.send_at.isoformat() if agent.schedule and agent.schedule.send_at else None,
            )
        return agent

    def run_batch(
        self, input_dir: str | Path, now: datetime | None = None, force: bool = False
    ) -> tuple[list[AgentState], list[tuple[str, str]], list[str]]:
        parsed, failures = parse_dir(input_dir)
        processed = self.repo.processed_message_ids() if self.repo else set()
        skipped: list[str] = []
        states: list[AgentState] = []
        for email in parsed:
            if not force and email.message_id in processed:
                skipped.append(email.message_id)
                continue
            states.append(self.process(email, now))
        return states, failures, skipped

    # -- extras that only a graph engine can offer ---------------------------
    def diagram(self) -> str:
        """Mermaid source of the compiled graph (docs, README, review)."""
        return self.graph.get_graph().draw_mermaid()

    def snapshot(self, message_id: str) -> dict[str, Any] | None:
        """Durable state for a given thread (requires `EMAIL_AGENT_GRAPH_CHECKPOINT=1`)."""
        if self.checkpointer is None:
            return None
        state = self.graph.get_state({"configurable": {"thread_id": message_id}})
        return state.values if state else None
