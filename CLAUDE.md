@AGENTS.md

# Claude Code specifics

The shared project rules, workflow, review process and commands are in `AGENTS.md`, imported above. This file adds only what is specific to Claude Code.

## Responding to review

- Evaluate each Codex finding on its merits. Fix the legitimate ones and explain any you disagree with in the PR. Never accept a suggestion automatically, and never dismiss one without checking it.
- Do not merge a pull request, whatever its review or CI state. The owner merges after explicitly approving.

## Keeping documents current

- Update `AGENTS.md`, this file, ADRs, tests and architecture docs in the same PR as the change they describe.
- A rule that applies to every AI tool belongs in `AGENTS.md`. Put it here only if it is meaningful to Claude Code alone.
