# migrate

An official standalone application deliverable for Toka providing local, deterministic SQLite migration orchestration.

## Features

- **Strict Migration Discovery**: Discovers migrations matching `^[0-9]{4}_[a-zA-Z0-9_-]+\.up\.sql$`, rejecting duplicate sequence numbers or malformed names.
- **Single-Migration Transaction Atomicity**: Each migration executes inside its own dedicated transaction (`BEGIN IMMEDIATE ... COMMIT/ROLLBACK`). If a migration fails, its changes and ledger records are rolled back immediately without affecting previously applied migrations.
- **Zero Side-Effect Introspection**: Read-only commands (`status`, `plan`, `verify`) probe existing databases without creating ledger tables or empty database files.
- **Pre-flight Integrity Audit**: `apply` verifies that all previously recorded migrations match on-disk file checksums before executing any pending migrations.
- **Tamper Detection via SHA-256**: State ledger records migration identity, SHA-256 checksum, and execution timestamp (`_toka_migrations`).
- **SQL Safety Scanning**: Pre-scans migration SQL text to forbid nested transaction statements (`BEGIN`, `COMMIT`, `ROLLBACK`, `SAVEPOINT`) and `VACUUM`.

## Installation & Consumption

In your Toka project manifest (`package.tk`):

```toka
dependencies = (
    migrate = "migrate:0.1.0",
)
```

## CLI Usage

```bash
# Display help and version
migrate --help
migrate --version

# Inspect migration status (read-only)
migrate --database app.db --dir migrations status

# Preview pending migrations without execution (read-only)
migrate --database app.db --dir migrations plan

# Apply pending migrations atomically
migrate --database app.db --dir migrations apply

# Audit integrity of applied migrations against local files (read-only)
migrate --database app.db --dir migrations verify
```

## Options

- `-d, --database <path>`: Path to SQLite database file (default: `app.db`). Note: `:memory:` is supported for single-invocation ephemeral execution.
- `-m, --dir <path>`: Directory containing migration scripts (default: `./migrations`).
- `-v, --version`: Print version information.
- `-h, --help`: Print usage instructions.
