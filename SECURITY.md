# Security

## Status

Pre-alpha (Phase 0). There is no application yet, so nothing is deployed.

## Reporting a vulnerability

Do not open a public issue. While the repository is private, contact the maintainer directly. Once it is public, use GitHub private vulnerability reporting.

## Security model

These are design commitments for the application, not yet implemented features:

- Local-first: documents stay on the user's machine.
- The server binds to `127.0.0.1` only, with Host and Origin checks and a per-install token.
- API keys are stored in Windows Credential Manager, never in files or the repository.
- Cloud AI providers receive only retrieved passages, behind an explicit egress gate.
- Document text is treated as untrusted input and is never logged.
