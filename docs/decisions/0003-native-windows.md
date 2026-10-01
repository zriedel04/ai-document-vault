# ADR 0003: Native Windows without Docker or WSL2

- Status: Accepted
- Date: 2026-10-01
- Hardware-dependent: no
- Plan reference: docs/architecture.md, section 4 and section 22 (decision 2)

## Context

The vault lives on a Windows PC, next to the documents it indexes. The app needs direct file access, reliable folder-change events for a later watched inbox, access to a GPU if there is one for embeddings and OCR, and Windows Credential Manager for API keys.

## Decision

Run natively on Windows. Python and all packages are pinned with `uv` and a lockfile, the frontend is built with Node, and a single PowerShell script (`vault.ps1`) installs, runs, stops, updates and backs up the app. Code uses `pathlib` everywhere and never shells out to Unix tools. CI includes a Windows runner so path, locking and encoding bugs show up early.

## Alternatives considered

- **Docker Desktop**: adds a Linux VM, several GB of RAM and file-sharing layers. Folder-change notifications from Windows into containers are unreliable and GPU passthrough adds setup. It solves "many services on many machines", which this app does not have.
- **WSL2 as the runtime**: files on `C:\` are slow to reach from WSL2, change events do not reach Linux watchers reliably, and keeping the vault inside the WSL filesystem hides it from Windows tools and backups.
- **A mix**: acceptable only if a future dependency works on Linux only, as a helper over localhost. Nothing in the current plan needs this.

## Consequences

- Every dependency must install from Windows wheels. The Phase 0 dependency spike confirms this on the Windows PC. If one does not, that is a change to this ADR and goes to the owner first.
- Windows CI costs more Actions minutes per run than Linux CI, so it runs on pull requests only.
- Line endings and `.ps1` files need care (see `.gitattributes`).
