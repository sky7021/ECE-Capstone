Environment Path: .\.conda (Use `.\.conda\python.exe` for all Python/test/lint commands; never scan or search inside `.conda/`)

# Token and Credit Optimization Rules
- Never scan entire directory trees or inspect multi-file diffs inside the primary context window.
- Whenever a task requires broad codebase search, multi-file inspection, documentation generation, or log/test analysis, always spawn the `scout` subagent (`.agents/agents/scout.md`) via `invoke_subagent`.
- For Gemini, `scout` is configured to use the 3.8 Flash model with high thinking (`gemini-3.8-flash`, thinking: `high`; pass `Model: "flash"` when calling `invoke_subagent`, and register `scout` first with `define_subagent` if not yet defined in the active session).
- Ingest only the structured summary returned by the subagent.

# Division of Responsibilities: Main Model vs. `scout` Subagent
- **`scout` is a factual summarizer and atomic task executor—never an architect.** Do not delegate architectural decisions, design trade-offs, or open-ended evaluations (e.g., *"which parts of the project architecture can be simplified?"*) to `scout`.
- **Two-Stage Discovery & Execution Workflow:**
  1. **Discovery / Indexing (`scout`):** Ask `scout` to scan the codebase and return factual summaries, relevant file paths, symbol names, call sites, and exact line ranges where a task lives.
  2. **Reasoning & Decision-Making (Main Model):** The main model reads the targeted files/line ranges identified by `scout` (or asks `scout` for specific follow-up details) and performs all architectural analysis and design decisions itself.
  3. **Atomic Execution (`scout`):** Once the main model has determined the exact changes required, it may delegate concrete, well-specified atomic edits, documentation updates, or test runs back to `scout`.
