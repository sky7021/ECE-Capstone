Environment Path: C:\Users\Kevin Wu\miniconda3\envs\ECE

# Token and Credit Optimization Rules
- Never scan entire directory trees or inspect multi-file diffs inside the primary context window.
- Whenever a task requires broad codebase search, multi-file inspection, documentation generation, or log/test analysis, always spawn the `scout` subagent (`.agents/agents/scout.md`) via `invoke_subagent`.
- For Gemini, `scout` is configured to use the 3.8 Flash model with high thinking (`gemini-3.8-flash`, thinking: `high`; pass `Model: "flash"` when calling `invoke_subagent`, and register `scout` first with `define_subagent` if not yet defined in the active session).
- Ingest only the structured summary returned by the subagent.
