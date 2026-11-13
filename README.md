# NovaPay agents

A governance agent (FinOps lead) and an on-call agent for a fictional fintech, NovaPay, built for the AI Agents course final projects. Everything runs on a seeded mock environment with planted problems and an answer key, so results can be measured. Mock prices are not AWS list prices.

Work in progress: the mock data generator is done; the MCP server and agents are next.

```bash
uv run python -m novapay_agents.generate
uv run python -m pytest
```
