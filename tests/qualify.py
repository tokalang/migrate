#!/usr/bin/env python3
"""Comprehensive RC6 Qualification Test Suite for tokalang/migrate."""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile


def log(msg: str) -> None:
    print(f"[QUALIFY] {msg}", flush=True)


def find_sdk() -> tuple[Path, Path, Path]:
    sdk_env = os.environ.get("TOKA_SDK", "/tmp/toka-sdk-rc6")
    root_path = Path(sdk_env)
    toka = root_path / "bin" / "toka"
    tokac = root_path / "bin" / "tokac"
    lib = root_path / "lib"
    if not toka.is_file() or not tokac.is_file() or not lib.is_dir():
        raise RuntimeError(f"Invalid TOKA_SDK at {sdk_env}: missing bin/toka, bin/tokac, or lib/")
    return toka, tokac, lib


def run_cmd(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    res = subprocess.run(cmd, cwd=str(cwd) if cwd else None, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"Command failed (exit {res.returncode}): {' '.join(cmd)}\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
    return res


def compile_migrate(repo_root: Path, tokac: Path, sdk_lib: Path) -> Path:
    target_dir = repo_root / "target"
    target_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. Compile C shim for sqlite
    sqlite_repo = repo_root.parent / "sqlite"
    c_shim = sqlite_repo / "native" / "sqlite_preflight.c"
    shim_obj = target_dir / "sqlite_preflight.o"
    run_cmd(["clang", "-c", "-O2", str(c_shim), "-o", str(shim_obj)])

    # 2. Compile Toka sources to LLVM IR
    main_ll = target_dir / "main.ll"
    run_cmd([
        str(tokac),
        "-I", str(sdk_lib),
        "-I", str(sqlite_repo / "lib"),
        "-I", str(repo_root),
        "--emit-llvm",
        str(repo_root / "src" / "main.tk"),
        "-o", str(main_ll)
    ], cwd=repo_root)
    assert main_ll.is_file(), "main.ll not generated"

    # 3. Link binary
    bin_path = target_dir / "migrate"
    runtime_obj = sdk_lib / "sys" / "toka_rt.o"
    link_cmd = [
        "clang",
        str(main_ll),
        str(shim_obj),
        str(runtime_obj),
        "-o", str(bin_path)
    ]
    try:
        sqlite_flags = subprocess.check_output(["pkg-config", "--libs", "sqlite3"], text=True).strip()
        link_cmd.extend(sqlite_flags.split())
    except Exception:
        link_cmd.append("-lsqlite3")

    try:
        ssl_flags = subprocess.check_output(["pkg-config", "--libs", "openssl"], text=True).strip()
        link_cmd.extend(ssl_flags.split())
    except Exception:
        pass

    if platform.system() == "Darwin":
        try:
            sdk_path = subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip()
            link_cmd.extend(["-isysroot", sdk_path])
        except Exception:
            pass

    run_cmd(link_cmd)
    assert bin_path.is_file(), "migrate binary not generated"
    return bin_path


def main() -> int:
    log("Starting tokalang/migrate RC6 qualification test suite...")
    repo_root = Path(__file__).resolve().parent.parent
    toka, tokac, sdk_lib = find_sdk()
    log(f"Using Toka compiler: {tokac}")
    log(f"Using standard library: {sdk_lib}")

    bin_path = compile_migrate(repo_root, tokac, sdk_lib)
    log("Step 1: Compiled migrate binary successfully.")

    # Step 2: CLI basics (--help, --version)
    log("Step 2: Testing CLI baseline options (--help, --version)...")
    res = run_cmd([str(bin_path), "--help"])
    assert "Usage:" in res.stdout and "Commands:" in res.stdout, "Help output malformed"

    res = run_cmd([str(bin_path), "--version"])
    assert "migrate 0.1.1" in res.stdout, "Version string mismatch"

    # Step 3: Zero side-effect introspection (status, plan, verify on non-existent db)
    log("Step 3: Testing zero side-effect introspection on non-existent database file...")
    with tempfile.TemporaryDirectory(prefix="migrate-zero-side-effect-") as tmp_dir:
        non_existent_db = os.path.join(tmp_dir, "fresh_uncreated.db")
        mig_dir = os.path.join(tmp_dir, "migrations")
        os.makedirs(mig_dir, exist_ok=True)
        with open(os.path.join(mig_dir, "0001_init.up.sql"), "w") as f:
            f.write("CREATE TABLE users (id INTEGER PRIMARY KEY);")

        # status on uncreated db
        res = run_cmd([str(bin_path), "-d", non_existent_db, "-m", mig_dir, "status"])
        assert "[PENDING]  0001_init.up.sql" in res.stdout
        assert not os.path.exists(non_existent_db), "CRITICAL: status created an empty database file on disk!"

        # plan on uncreated db
        res = run_cmd([str(bin_path), "-d", non_existent_db, "-m", mig_dir, "plan"])
        assert "1. 0001_init.up.sql" in res.stdout
        assert not os.path.exists(non_existent_db), "CRITICAL: plan created an empty database file on disk!"

        # verify on uncreated db
        res = run_cmd([str(bin_path), "-d", non_existent_db, "-m", mig_dir, "verify"])
        assert "0 applied migrations verified" in res.stdout
        assert not os.path.exists(non_existent_db), "CRITICAL: verify created an empty database file on disk!"

    # Step 4: Strict Discovery (Malformed filenames & duplicate numbers)
    log("Step 4: Testing strict discovery rules (malformed files, duplicate sequence numbers)...")
    with tempfile.TemporaryDirectory(prefix="migrate-discovery-") as tmp_dir:
        mig_dir = os.path.join(tmp_dir, "migrations")
        os.makedirs(mig_dir, exist_ok=True)
        # Normal README is ignored
        with open(os.path.join(mig_dir, "README.md"), "w") as f:
            f.write("# Migration scripts")
        # Malformed: starts with digit but missing .up
        with open(os.path.join(mig_dir, "0001_init.sql"), "w") as f:
            f.write("SELECT 1;")
        
        res = run_cmd([str(bin_path), "-m", mig_dir, "status"], check=False)
        assert res.returncode == 1, "Expected failure on malformed migration file 0001_init.sql"
        assert "malformed migration filename" in res.stdout

        # Clean malformed, add duplicate sequence numbers
        os.remove(os.path.join(mig_dir, "0001_init.sql"))
        with open(os.path.join(mig_dir, "0001_alpha.up.sql"), "w") as f:
            f.write("SELECT 1;")
        with open(os.path.join(mig_dir, "0001_beta.up.sql"), "w") as f:
            f.write("SELECT 2;")

        res = run_cmd([str(bin_path), "-m", mig_dir, "status"], check=False)
        assert res.returncode == 1, "Expected failure on duplicate sequence numbers"
        assert "duplicate migration sequence number '0001'" in res.stdout

    # Step 5: SQL Safety Scanning (Forbidden transaction control, END, RELEASE, VACUUM, & comments/literals)
    log("Step 5: Testing SQL safety scanning (BEGIN, COMMIT, END, ROLLBACK, SAVEPOINT, RELEASE, VACUUM)...")
    forbidden_keywords = (
        "BEGIN TRANSACTION;", "commit;", "END;", "END TRANSACTION;",
        "ROLLBACK;", "SAVEPOINT sp1;", "RELEASE SAVEPOINT sp1;", "RELEASE sp1;", "VACUUM;"
    )
    for bad_keyword in forbidden_keywords:
        with tempfile.TemporaryDirectory(prefix="migrate-safety-") as tmp_dir:
            db_file = os.path.join(tmp_dir, "test.db")
            mig_dir = os.path.join(tmp_dir, "migrations")
            os.makedirs(mig_dir, exist_ok=True)
            with open(os.path.join(mig_dir, "0001_bad.up.sql"), "w") as f:
                f.write(f"CREATE TABLE t (id INT);\n{bad_keyword}\n")
            
            res = run_cmd([str(bin_path), "-d", db_file, "-m", mig_dir, "apply"], check=False)
            assert res.returncode == 1, f"Expected rejection for SQL with '{bad_keyword}'"
            assert "SQL Safety Check Failed" in res.stdout
            assert "forbidden statement" in res.stdout

    # Step 5b: Confirm comments and string literals containing keywords are allowed
    log("Step 5b: Confirming keywords in SQL comments and string literals are safely allowed...")
    with tempfile.TemporaryDirectory(prefix="migrate-safety-valid-") as tmp_dir:
        db_file = os.path.join(tmp_dir, "valid.db")
        mig_dir = os.path.join(tmp_dir, "migrations")
        os.makedirs(mig_dir, exist_ok=True)
        with open(os.path.join(mig_dir, "0001_valid_literals.up.sql"), "w") as f:
            f.write("""-- Comment containing BEGIN, COMMIT, END, ROLLBACK, and VACUUM
/* Multi-line comment
   SAVEPOINT sp1; RELEASE sp1;
*/
CREATE TABLE logs (id INTEGER PRIMARY KEY, msg TEXT);
INSERT INTO logs VALUES (1, 'User requested END of session with COMMIT token');
""")
        res = run_cmd([str(bin_path), "-d", db_file, "-m", mig_dir, "apply"])
        assert "Applied '0001_valid_literals.up.sql' successfully." in res.stdout

    # Step 6: Single-Migration Transaction Atomicity & Rollback (including END evasion attack check)
    log("Step 6: Testing single-migration transaction atomicity, rollback, and END evasion defense...")
    with tempfile.TemporaryDirectory(prefix="migrate-atomicity-") as tmp_dir:
        db_file = os.path.join(tmp_dir, "test.db")
        mig_dir = os.path.join(tmp_dir, "migrations")
        os.makedirs(mig_dir, exist_ok=True)
        with open(os.path.join(mig_dir, "0001_step1.up.sql"), "w") as f:
            f.write("CREATE TABLE table1 (id INTEGER PRIMARY KEY, name TEXT);")
        with open(os.path.join(mig_dir, "0002_step2_failing.up.sql"), "w") as f:
            f.write("CREATE TABLE table2 (id INTEGER PRIMARY KEY); SYNTAX ERROR THIS IS INVALID SQL;")

        res = run_cmd([str(bin_path), "-d", db_file, "-m", mig_dir, "apply"], check=False)
        assert res.returncode == 1, "Expected apply to fail on migration 0002"
        assert "Applied '0001_step1.up.sql' successfully." in res.stdout
        assert "Failed to execute migration SQL in '0002_step2_failing.up.sql'" in res.stdout

        # Verify database state using native sqlite3
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [r[0] for r in cursor.fetchall()]
        assert "table1" in tables, "table1 should exist from committed migration 0001"
        assert "table2" not in tables, "table2 must NOT exist due to rollback of migration 0002"
        assert "_toka_migrations" in tables, "_toka_migrations should exist"

        cursor.execute("SELECT id FROM _toka_migrations;")
        records = [r[0] for r in cursor.fetchall()]
        assert records == ["0001_step1.up.sql"], f"Ledger should only contain 0001, got: {records}"
        conn.close()

    # Step 6b: END evasion attack: ensure table is not left on disk
    with tempfile.TemporaryDirectory(prefix="migrate-evasion-") as tmp_dir:
        db_file = os.path.join(tmp_dir, "evasion.db")
        mig_dir = os.path.join(tmp_dir, "migrations")
        os.makedirs(mig_dir, exist_ok=True)
        with open(os.path.join(mig_dir, "0001_evade.up.sql"), "w") as f:
            f.write("CREATE TABLE escaped_commit (id INT); END; THIS IS INVALID SQL;")

        res = run_cmd([str(bin_path), "-d", db_file, "-m", mig_dir, "apply"], check=False)
        assert res.returncode == 1, "apply must fail on END evasion attack"
        assert "forbidden statement: 'END'" in res.stdout

        if os.path.exists(db_file):
            conn = sqlite3.connect(db_file)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = [r[0] for r in cursor.fetchall()]
            assert "escaped_commit" not in tables, "escaped_commit table must NOT exist!"
            assert "_toka_migrations" not in tables, "_toka_migrations table must NOT exist!"
            conn.close()

    # Step 7: Full Normal Lifecycle (status -> plan -> apply -> verify)
    log("Step 7: Testing full multi-step migration lifecycle (status -> plan -> apply -> verify)...")
    with tempfile.TemporaryDirectory(prefix="migrate-lifecycle-") as tmp_dir:
        db_file = os.path.join(tmp_dir, "app.db")
        mig_dir = os.path.join(tmp_dir, "migrations")
        os.makedirs(mig_dir, exist_ok=True)
        with open(os.path.join(mig_dir, "0001_users.up.sql"), "w") as f:
            f.write("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL);\nINSERT INTO users (id, username) VALUES (1, 'alice');")
        with open(os.path.join(mig_dir, "0002_posts.up.sql"), "w") as f:
            f.write("CREATE TABLE posts (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT);\nINSERT INTO posts (id, user_id, title) VALUES (10, 1, 'First Post');")

        # 1. status before apply
        res = run_cmd([str(bin_path), "-d", db_file, "-m", mig_dir, "status"])
        assert "[PENDING]  0001_users.up.sql" in res.stdout
        assert "[PENDING]  0002_posts.up.sql" in res.stdout

        # 2. plan
        res = run_cmd([str(bin_path), "-d", db_file, "-m", mig_dir, "plan"])
        assert "1. 0001_users.up.sql" in res.stdout
        assert "2. 0002_posts.up.sql" in res.stdout
        assert "Total pending migrations to apply: 2" in res.stdout

        # 3. apply
        res = run_cmd([str(bin_path), "-d", db_file, "-m", mig_dir, "apply"])
        assert "Applied '0001_users.up.sql' successfully." in res.stdout
        assert "Applied '0002_posts.up.sql' successfully." in res.stdout
        assert "Successfully applied all 2 migration(s)." in res.stdout

        # 4. status after apply
        res = run_cmd([str(bin_path), "-d", db_file, "-m", mig_dir, "status"])
        assert "[APPLIED]  0001_users.up.sql" in res.stdout
        assert "[APPLIED]  0002_posts.up.sql" in res.stdout
        assert "Pending: 0" in res.stdout

        # 5. verify
        res = run_cmd([str(bin_path), "-d", db_file, "-m", mig_dir, "verify"])
        assert "All 2 applied migrations verified successfully (0 tampered, 0 missing)." in res.stdout

        # 6. re-apply idempotence
        res = run_cmd([str(bin_path), "-d", db_file, "-m", mig_dir, "apply"])
        assert "Database is already up to date. 0 pending migrations." in res.stdout

        # Step 8: Tamper Detection & Pre-Flight Fail-Closed
        log("Step 8: Testing tamper detection and pre-flight fail-closed on apply...")
        # Modify 0001_users.up.sql content
        with open(os.path.join(mig_dir, "0001_users.up.sql"), "a") as f:
            f.write("\n-- Tampered comment")

        # verify must fail
        res = run_cmd([str(bin_path), "-d", db_file, "-m", mig_dir, "verify"], check=False)
        assert res.returncode == 1, "verify should fail after file tampering"
        assert "VERIFY FAILURE: Migration '0001_users.up.sql' checksum mismatch!" in res.stdout

        # Add a new migration 0003
        with open(os.path.join(mig_dir, "0003_comments.up.sql"), "w") as f:
            f.write("CREATE TABLE comments (id INTEGER PRIMARY KEY, content TEXT);")

        # apply must fail-closed in pre-flight without applying 0003
        res = run_cmd([str(bin_path), "-d", db_file, "-m", mig_dir, "apply"], check=False)
        assert res.returncode == 1, "apply should fail closed on tampered prior migration"
        assert "PRE-FLIGHT AUDIT FAILED" in res.stdout
        assert "database ledger integrity is compromised" in res.stdout

        # Confirm 0003 was never applied
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [r[0] for r in cursor.fetchall()]
        assert "comments" not in tables, "comments table must not exist"
        cursor.execute("SELECT id FROM _toka_migrations;")
        records = [r[0] for r in cursor.fetchall()]
        assert "0003_comments.up.sql" not in records
        conn.close()

        # Step 9: Missing on disk detection
        log("Step 9: Testing missing on-disk migration detection...")
        # Restore 0001, delete 0002
        with open(os.path.join(mig_dir, "0001_users.up.sql"), "w") as f:
            f.write("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL);\nINSERT INTO users (id, username) VALUES (1, 'alice');")
        os.remove(os.path.join(mig_dir, "0002_posts.up.sql"))

        res = run_cmd([str(bin_path), "-d", db_file, "-m", mig_dir, "verify"], check=False)
        assert res.returncode == 1, "verify should fail when recorded file is deleted"
        assert "VERIFY FAILURE: Migration '0002_posts.up.sql' is recorded in database but missing on disk!" in res.stdout

        res = run_cmd([str(bin_path), "-d", db_file, "-m", mig_dir, "apply"], check=False)
        assert res.returncode == 1, "apply should fail closed when recorded file is deleted"
        assert "Recorded migration '0002_posts.up.sql' is missing on disk!" in res.stdout

    log("ALL QUALIFICATION TESTS (INCLUDING END/RELEASE EVASION DEFENSE) PASSED (100% SUCCESS)!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
