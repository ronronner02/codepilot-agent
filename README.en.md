# CodePilot-Agent

*English summary. The full documentation is in Chinese: [README.md](README.md).*

Point it at a GitHub repository URL and it produces three things: an **architecture report whose every claim traces to a real file**, **single-turn code Q&A** scoped to that repository, and a **code review** covering structure, error handling, and security. The repository-analysis capability is also exposed as an **MCP server**, callable directly from Claude Code, Cursor, and similar clients.

## What makes it different

Every conclusion in the report must resolve to a file path, and that constraint is enforced by code rather than by prompt instructions — cited paths must exist, line numbers must fall within the file's actual length, and any conclusion that fails validation is dropped and counted in the report's gap section. So you never get sentences like "this project uses a layered architecture with clean code structure" that would be true of any repository.

## How it works

```text
repo URL -> clone + language detection -> static analysis (tree-sitter)
                                              |
                    structural skeleton: symbols · dependency graph · module clusters · entry points
                                              |
                 +----------------------------+----------------------------+
                 |                                                         |
        Planner picks deep-dive scope                          Reviewer runs 3 check classes
                 |                                                         |
      +----------+----------+                                      review findings
      |                     |
  module sub-agents     chunking + vector index   <- runs concurrently with fan-out and review
      |                     |
  aggregate + citation validation        single-turn code Q&A · semantic search
      |
  architecture report
```

Nodes drawn on the same layer genuinely run concurrently. LangGraph advances by superstep — same-layer nodes run in parallel while cross-layer nodes necessarily serialize — so which node sits behind which determines what runs alongside what. Indexing is the slowest stage (30.5 minutes measured on `fastapi`), so placing it before the fan-out would leave the user waiting half an hour for the first module analysis.

Deterministic parts use deterministic means: file tree, import dependency graph, module clustering, entry points, and stack detection all come from static analysis without touching an LLM. Only the judgment-bearing parts use agents — Planner decides which modules to dig into, module sub-agents read code through tools, and Reviewer's candidate findings are located by AST before the model selects among them.

## Quick start

Requires a `DEEPSEEK_API_KEY` in `.env`. With Docker:

```bash
cp .env.example .env    # fill in DEEPSEEK_API_KEY at minimum
docker compose up --build
```

For local development, MCP server setup, public deployment hardening, and known boundaries, see the Chinese [README.md](README.md).

## License

See [README.md](README.md) for license details.
