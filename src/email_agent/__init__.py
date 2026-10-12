"""Agentic email automation — core package.

The pipeline is a LangGraph state graph (`email_agent.graph`); parser, rules engine, agents,
storage, retrieval, reporting and the CLI are stdlib-only. Optional LLM refinement runs through
`email_agent.llm` when an API key is configured.
"""

__version__ = "0.1.0"
