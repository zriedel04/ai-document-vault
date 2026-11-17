# Security

## Status

Pre-alpha. The app runs locally on Windows (import, keyword search and the document viewers), but it is not ready for daily use and nothing is deployed. See the [README](README.md#status).

## Reporting a vulnerability

Do not open a public issue. While the repository is private, contact the maintainer directly. Once it is public, use GitHub private vulnerability reporting.

## Security model

Implemented:

- Local-first: documents stay on the user's machine. Nothing calls a cloud service yet.
- The server binds to a loopback address only, with Host and Origin checks and a per-install token.
- Document text is treated as untrusted input and is never logged; heavy parsing runs in a sandboxed child process with a timeout and a memory cap.

Design commitments, not yet implemented (they arrive with Ask the Vault in Phase 5):

- API keys are stored in Windows Credential Manager, never in files or the repository.
- Cloud AI providers receive only retrieved passages, behind an explicit egress gate.
