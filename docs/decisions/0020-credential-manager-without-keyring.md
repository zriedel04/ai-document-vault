# ADR 0020: Credential Manager through advapi32, not keyring

- Status: Accepted
- Date: 2026-11-24
- Hardware-dependent: no
- Plan reference: docs/architecture.md, section 9 ("API key management": "stored in Windows Credential Manager through the `keyring` library"), section 21 (Licences: "keyring | MIT / BSD | OK") and section 13 (Phase 5, "Credential Manager keys")

## Context

The plan stores the cloud providers' API keys in Windows Credential Manager and names the `keyring` library as the way in. ADR 0010 requires only the store: keys live in Credential Manager, never in files, logs, the database or the browser. The key store (`backend/src/vault/ai/keys.py`) needs four calls: write, read and delete one generic credential, and free what a read returns.

Adding `keyring` 25.7.0 was tried (`uv add keyring`) and its Windows backend was read (`keyring/backends/Windows.py`). Four things stand against it:

- **Packages per platform.** keyring brings in `pywin32-ctypes` on Windows only and `jeepney`, `secretstorage`, `cryptography`, `cffi` and `pycparser` on Linux only, next to `jaraco-classes`, `jaraco-context`, `jaraco-functools` and `more-itertools` on both. `scripts/third_party_notices.py check python` runs on Linux and on Windows and compares the exact set of runtime packages each sees, and its docstring says that a platform-only dependency has no single `THIRD_PARTY_NOTICES.md` that passes both checks until the script learns platform-specific entries. That is a change to the compliance tooling for a dependency the app does not need. The licences themselves are permissive (MIT, BSD-3-Clause, Apache-2.0 OR BSD-3-Clause, MIT-0).
- **A copy of the old key on replace.** `WinVaultKeyring.set_password` reads the credential under the service name and, when one exists, first writes its secret again under a second target, `<user>@<service>`, before writing the new one. A key entered with another user name (by `cmdkey` or the Control Panel) is therefore kept under a second name after it was "replaced", so the old secret stays in Credential Manager.
- **Roaming by default.** The backend writes with `CRED_PERSIST_ENTERPRISE` unless the caller changes it, so a key follows a domain roaming profile to other computers. A local-first app (ADR 0001) keeps it on this computer.
- **Lenient reading.** A stored secret that is not UTF-16 is decoded as UTF-8 with a warning through keyring's own logger, and the blob's size is not bounded before it is copied. The key store's rule is that a stored credential is used only if it is exactly what the store would write.

## Decision

- The key store calls advapi32 directly with `ctypes` (`CredWriteW`, `CredReadW`, `CredDeleteW`, `CredFree`), on Windows only, behind a small `CredentialBackend` protocol. `keyring` is not a dependency, and no runtime package is added.
- One generic credential per provider that takes a key (`anthropic`, `openai`; not `fake`), named `AIVaultLocal/<provider>`, with a fixed user name and comment, persisted with `CRED_PERSIST_LOCAL_MACHINE`. Replacing a key overwrites that one credential; no second target is ever written.
- The secret is UTF-16-LE without a terminator, which is what `cmdkey` writes, so a key entered with `cmdkey /generic:AIVaultLocal/<provider>` reads back. A stored secret is bounded in size before it is copied, decoded strictly and checked against the key's shape; anything else is reported as damaged, not used.
- On other systems the store reports, in fixed text, that Credential Manager is not available. Tests on Linux use an in-memory backend; the Windows CI job writes, reads and deletes real credentials under a unique name.

## Alternatives considered

- **keyring through `keyring.get_keyring()`**: a user's keyring configuration file or the `PYTHON_KEYRING_BACKEND` environment variable picks the backend, so a key could be sent to a file or another store without the app knowing. Rejected for that reason alone.
- **keyring's `WinVaultKeyring` chosen explicitly, with a fixed user name, local persistence and a clean-up of `<user>@<service>`**: it would work around three of the four problems above and still need the notices tooling to learn platform-specific entries. More code than the four calls it wraps.
- **`pywin32-ctypes` alone (`win32ctypes.pywin32.win32cred`)**: one Windows-only package, which still has no single notices file that passes both checks, for the same four calls.
- **pywin32**: a large Windows-only package with the same notices problem.

## Consequences

- No new dependency, so nothing changes in the licence gate, the notices or the dependency audit, and the backend's runtime is the same on Linux and Windows.
- The project maintains about a hundred lines of `ctypes` code against a stable, documented Windows API: the `CREDENTIALW` layout and the four calls. The Windows CI job runs a real round trip, so a mistake there fails CI.
- What keyring would have given for free and is not needed here (other platforms' stores, backend plugins) is not available. The app runs on Windows (ADR 0003); a store for another system would be a new backend behind the same protocol and a new ADR.
- The Python string of a key cannot be wiped from memory, with or without keyring; the `ctypes` buffers are zeroed after each call.
