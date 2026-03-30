# Digital Procurement News Scout

Digital Procurement News Scout (DPNS) is a daily automated digest that fetches procurement and digital transformation news, scores relevance with an LLM, renders an executive email, and sends it to a configurable recipient list.

## Current Status

This repository includes the Phase 1 / Epic 1.1 foundation:

- Project structure and Python packaging
- Typed config loading from YAML and environment variables
- Structured logging utilities
- SQLite schema initialization and persistence helpers

## Quick Start

1. Create a virtual environment with Python 3.11+.
2. Install dependencies:

```bash
pip install -e .
```

3. Copy the environment template and fill in secrets:

```bash
cp .env.example .env
```

4. Run the initialization entry point:

```bash
python -m src.main
```

## Development

Run tests with:

```bash
pytest tests/ -v
```
