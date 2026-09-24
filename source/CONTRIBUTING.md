# Contributing

## Getting it running

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
.venv/bin/uvicorn app.main:app --reload
```

The schema is applied automatically on startup. Without Docker, set
`SANDBOX_DRIVER=local_unsafe` — everything works except service deployments, and code
runs **unisolated on your machine**, so use it only on a laptop.

## Before opening a pull request

```bash
.venv/bin/ruff check app tests scripts migrations
.venv/bin/pytest
```

Both must pass. If your change touches the sandbox, also run the suite that needs a real
daemon:

```bash
docker build -t agentspace/runtime:latest -f docker/runtime.Dockerfile .
.venv/bin/pytest tests/test_docker_sandbox.py
```

If you changed a model, generate the migration — the suite fails if you forget:

```bash
.venv/bin/alembic revision --autogenerate -m "describe the change"
```

## How this codebase is organised

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) first; it explains the three decisions
that shape everything else. The short version:

- **`app/operations.py` holds every capability.** REST, MCP and A2A are thin adapters over
  it. Put behaviour there, not in a transport.
- **`app/teaching.py` is not documentation, it is code.** `AgentSpaceError` requires a
  `fix` argument. An error that cannot say how to recover does not compile.
- **`app/storage.py:resolve()` is the only function allowed to turn an untrusted string
  into a path.** Everything that touches user files goes through it.

## What a good change looks like

**Add a capability** by adding one entry to `CAPABILITIES` and one branch to
`operations.dispatch`. The REST catalogue, the MCP tool list, the A2A agent card and
`/llms.txt` all render from that one place, so they cannot drift.

**Write the error message for the agent that will read it.** Not "invalid path" —
"Paths are relative to your workspace root. Drop any leading '/' and any '..'." The
reader is a program with no memory of the docs, deciding what to do next from your
sentence alone.

**Test the behaviour, not the implementation.** The suite is full of tests named after
the property they protect (`test_hidden_files_are_never_served`), because in a year the
name is the only thing that explains why the code is shaped that way.

**Say what you could not verify.** A PR that says "I could not test rootless Docker on
macOS" is far more useful than one that implies it was tested.

## Security

Do not open an issue for a vulnerability. See [SECURITY.md](SECURITY.md).

## Licence

Contributions are accepted under the Apache License 2.0. By opening a pull request you
agree that your contribution is licensed under it.
