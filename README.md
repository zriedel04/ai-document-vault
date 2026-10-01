# ai-document-vault

A local-first AI knowledge system for securely indexing, searching, and querying documents using hybrid retrieval, RAG, OCR, and verifiable citations.

## Status

Pre-alpha. Phase 0 (foundations) is in progress: see the [milestone](https://github.com/zriedel04/ai-document-vault/milestone/1). The backend skeleton exists ([`backend/`](backend/README.md): a FastAPI app with `GET /api/health`), but there is nothing to install or use yet.

## What it will do

Run on a Windows desktop as a local web app: import documents, extract and OCR their text, search by keyword and meaning, and answer questions with citations that point back to the exact page and passage.

## Privacy rule for this repository

No personal documents, extracted text, databases, keys, logs or local configuration are ever committed. Tests and the demo use synthetic data only. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Development

- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [Changelog](CHANGELOG.md)

## AI assistance

This project is developed with AI coding tools, openly. Every change goes through a pull request, tests and human review.
