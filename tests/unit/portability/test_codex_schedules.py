# -*- coding: utf-8 -*-
# pylint: disable=protected-access
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from qwenpaw.portability.providers import codex_schedules
from qwenpaw.portability.providers import codex_schedule_reader
from qwenpaw.portability.providers.codex_schedules import (
    discover_codex_scheduled_tasks,
)


def _write_automation(home: Path, automation_id: str, body: str) -> Path:
    path = home / "automations" / automation_id / "automation.toml"
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    return path


def _create_codex_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE automations (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                next_run_at INTEGER,
                last_run_at INTEGER,
                cwds TEXT NOT NULL DEFAULT '[]',
                rrule TEXT NOT NULL,
                model TEXT,
                reasoning_effort TEXT,
                created_at INTEGER,
                updated_at INTEGER,
                target_type TEXT,
                project_id TEXT
            );
            CREATE TABLE automation_runs (
                thread_id TEXT PRIMARY KEY,
                automation_id TEXT NOT NULL,
                status TEXT NOT NULL,
                archived_user_message TEXT,
                archived_assistant_message TEXT
            );
            """,
        )


def _insert_automation(
    connection: sqlite3.Connection,
    automation_id: str,
    name: str,
    prompt: str | bytes,
    rrule: str,
    *,
    status: str = "ACTIVE",
    cwds: str = "[]",
    updated_at: int = 1,
) -> None:
    connection.execute(
        "INSERT INTO automations "
        "(id, name, prompt, status, cwds, rrule, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (automation_id, name, prompt, status, cwds, rrule, updated_at),
    )


@pytest.fixture(autouse=True)
def _stable_local_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        codex_schedules,
        "_local_timezone_name",
        lambda: ("Asia/Shanghai", "system"),
    )


def test_toml_wins_over_sqlite_and_run_ids_are_structured(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".codex"
    _write_automation(
        home,
        "daily",
        """
id = "daily"
name = "TOML task"
prompt = "Read the TOML definition"
status = "ACTIVE"
kind = "heartbeat"
rrule = "FREQ=DAILY;BYHOUR=9;BYMINUTE=30"
cwd = "/toml/project"
""",
    )
    database = home / "sqlite" / "codex-dev.db"
    _create_codex_database(database)
    with sqlite3.connect(database) as connection:
        _insert_automation(
            connection,
            "daily",
            "stale DB task",
            "stale prompt",
            "FREQ=HOURLY;INTERVAL=2;BYMINUTE=0",
            status="PAUSED",
            cwds='["/sqlite/project"]',
        )
        _insert_automation(
            connection,
            "weekly",
            "DB task",
            "Read the DB definition",
            "FREQ=WEEKLY;BYDAY=MO,WE,FR;BYHOUR=8;BYMINUTE=0",
            status="PAUSED",
            cwds='["/sqlite/project"]',
            updated_at=2,
        )
        connection.execute(
            "INSERT INTO automation_runs VALUES (?, ?, ?, ?, ?)",
            (
                "automation-thread-1",
                "weekly",
                "COMPLETED",
                "must not be loaded",
                "must not be loaded",
            ),
        )

    before = database.read_bytes()
    (
        tasks,
        warnings,
        discovered_count,
        run_ids,
    ) = discover_codex_scheduled_tasks(home)

    assert [task.source_id for task in tasks] == ["daily", "weekly"]
    daily, weekly = tasks
    assert daily.name == "TOML task"
    assert daily.prompt == "Read the TOML definition"
    assert daily.cron == "30 9 * * *"
    assert daily.cwd == "/toml/project"
    assert daily.enabled is True
    assert daily.metadata["source_format"] == "toml"
    assert weekly.cron == "0 8 * * mon,wed,fri"
    assert weekly.enabled is False
    assert weekly.metadata["source_format"] == "sqlite"
    assert discovered_count == 2
    assert run_ids == {"automation-thread-1"}
    assert any("inferred local IANA timezone" in item for item in warnings)
    assert database.read_bytes() == before
    assert not database.with_name(database.name + "-journal").exists()


def test_live_wal_is_read_from_private_snapshot_without_source_writes(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".codex"
    database = home / "sqlite" / "codex-dev.db"
    _create_codex_database(database)
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        connection.execute("PRAGMA wal_autocheckpoint = 0")
        _insert_automation(
            connection,
            "wal-only",
            "WAL task",
            "Read the committed WAL state",
            "FREQ=DAILY;BYHOUR=11;BYMINUTE=25",
            updated_at=3,
        )
        connection.execute(
            "INSERT INTO automation_runs VALUES (?, ?, ?, ?, ?)",
            ("wal-thread", "wal-only", "COMPLETED", "secret", "secret"),
        )
        connection.commit()

        wal = database.with_name(database.name + "-wal")
        shm = database.with_name(database.name + "-shm")
        assert wal.stat().st_size > 0
        source_before = {
            path.name: path.read_bytes() for path in (database, wal, shm)
        }

        immutable_uri = f"{database.resolve().as_uri()}?mode=ro&immutable=1"
        with sqlite3.connect(immutable_uri, uri=True) as stale_connection:
            assert (
                stale_connection.execute(
                    "SELECT COUNT(*) FROM automations",
                ).fetchone()[0]
                == 0
            )

        (
            tasks,
            warnings,
            discovered_count,
            run_ids,
        ) = discover_codex_scheduled_tasks(home)

        assert [task.source_id for task in tasks] == ["wal-only"]
        assert tasks[0].cron == "25 11 * * *"
        assert discovered_count == 1
        assert run_ids == {"wal-thread"}
        assert not any("Could not read" in item for item in warnings)
        assert {
            path.name: path.read_bytes() for path in (database, wal, shm)
        } == source_before
    finally:
        connection.close()


def test_symlinked_wal_is_rejected_without_following_it(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".codex"
    database = home / "sqlite" / "codex-dev.db"
    _create_codex_database(database)
    outside = tmp_path / "outside-wal"
    outside.write_bytes(b"untrusted")
    database.with_name(database.name + "-wal").symlink_to(outside)

    (
        tasks,
        warnings,
        discovered_count,
        run_ids,
    ) = discover_codex_scheduled_tasks(home)

    assert not tasks
    assert discovered_count == 0
    assert not run_ids
    assert outside.read_bytes() == b"untrusted"
    assert any("is not a regular file" in item for item in warnings)


def test_oversized_database_is_rejected_before_sqlite_opens_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".codex"
    database = home / "sqlite" / "codex-dev.db"
    _create_codex_database(database)
    monkeypatch.setattr(codex_schedule_reader, "_MAX_SQLITE_DATABASE_BYTES", 1)

    (
        tasks,
        warnings,
        discovered_count,
        run_ids,
    ) = discover_codex_scheduled_tasks(home)

    assert not tasks
    assert discovered_count == 0
    assert not run_ids
    assert any("exceeds the read safety limit" in item for item in warnings)


def test_database_without_automation_runs_table_is_read_safely(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".codex"
    database = home / "sqlite" / "codex-dev.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE automations ("
            "id TEXT, name TEXT, prompt TEXT, status TEXT, cwds TEXT, "
            "rrule TEXT, updated_at INTEGER)",
        )
        _insert_automation(
            connection,
            "no-runs-table",
            "No runs table",
            "Read this definition",
            "FREQ=DAILY;BYHOUR=12;BYMINUTE=5",
        )

    (
        tasks,
        warnings,
        discovered_count,
        run_ids,
    ) = discover_codex_scheduled_tasks(home)

    assert [task.source_id for task in tasks] == ["no-runs-table"]
    assert discovered_count == 1
    assert not run_ids
    assert not any("Could not read" in item for item in warnings)


@pytest.mark.parametrize("sidecar_suffix", ["-wal", "-shm"])
def test_disappearing_sqlite_sidecar_warns_and_continues_to_next_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sidecar_suffix: str,
) -> None:
    home = tmp_path / ".codex"
    racy_database = home / "sqlite" / "a-racy.db"
    _create_codex_database(racy_database)
    racy_database.with_name(racy_database.name + "-wal").write_bytes(
        b"transient wal",
    )
    racy_database.with_name(racy_database.name + "-shm").write_bytes(
        b"transient shm",
    )

    good_database = home / "sqlite" / "b-good.db"
    _create_codex_database(good_database)
    with sqlite3.connect(good_database) as connection:
        _insert_automation(
            connection,
            "good-after-race",
            "Good database",
            "Continue scanning",
            "FREQ=DAILY;BYHOUR=13;BYMINUTE=15",
        )

    original_copy = codex_schedule_reader._copy_bounded_regular_file
    disappeared = False

    def _disappear_during_copy(
        source: Path,
        target: Path,
        maximum_bytes: int,
    ) -> None:
        nonlocal disappeared
        if (
            not disappeared
            and source.parent == racy_database.parent
            and source.name == racy_database.name + sidecar_suffix
        ):
            disappeared = True
            source.unlink()
        original_copy(source, target, maximum_bytes)

    monkeypatch.setattr(
        codex_schedule_reader,
        "_copy_bounded_regular_file",
        _disappear_during_copy,
    )

    (
        tasks,
        warnings,
        discovered_count,
        run_ids,
    ) = discover_codex_scheduled_tasks(home)

    assert disappeared is True
    assert [task.source_id for task in tasks] == ["good-after-race"]
    assert discovered_count == 1
    assert not run_ids
    assert any(
        "Could not read Codex automation database a-racy.db" in item
        and f"{sidecar_suffix[1:]} disappeared while being copied" in item
        for item in warnings
    )


@pytest.mark.parametrize(
    ("rrule", "expected"),
    [
        ("FREQ=DAILY;BYHOUR=9;BYMINUTE=30", "30 9 * * *"),
        (
            "FREQ=WEEKLY;BYDAY=TU,TH;BYHOUR=7;BYMINUTE=5",
            "5 7 * * tue,thu",
        ),
        ("FREQ=HOURLY;BYMINUTE=15", "15 * * * *"),
        ("FREQ=MINUTELY", "* * * * *"),
    ],
)
def test_common_rrules_have_exact_five_field_conversion(
    tmp_path: Path,
    rrule: str,
    expected: str,
) -> None:
    home = tmp_path / ".codex"
    _write_automation(
        home,
        "task",
        'name = "Task"\nprompt = "Do it"\nstatus = "ACTIVE"\n'
        f'rrule = "{rrule}"\n',
    )

    (
        tasks,
        warnings,
        discovered_count,
        run_ids,
    ) = discover_codex_scheduled_tasks(home)

    assert len(tasks) == 1
    assert tasks[0].schedule_type == "cron"
    assert tasks[0].cron == expected
    assert tasks[0].timezone == "Asia/Shanghai"
    assert tasks[0].metadata["timezone_inferred"] is True
    assert discovered_count == 1
    assert not run_ids
    assert not any("not converted" in item for item in warnings)


@pytest.mark.parametrize(
    ("rrule", "reason_fragment"),
    [
        ("FREQ=HOURLY;INTERVAL=2;BYMINUTE=15", "no reliable phase anchor"),
        ("FREQ=HOURLY;INTERVAL=24;BYMINUTE=0", "no reliable phase anchor"),
        ("FREQ=MINUTELY;INTERVAL=30", "no reliable phase anchor"),
        ("FREQ=MINUTELY;INTERVAL=60", "no reliable phase anchor"),
        (
            "FREQ=DAILY;BYHOUR=9;BYMINUTE=0;COUNT=3",
            "field COUNT is unsupported",
        ),
        (
            "DTSTART;TZID=America/New_York:20260818T090000\n"
            "RRULE:FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            "DTSTART-anchored",
        ),
        ("FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=9;BYMINUTE=0", "frequency MONTHLY"),
    ],
)
def test_lossy_rrules_are_preserved_as_unsupported(
    tmp_path: Path,
    rrule: str,
    reason_fragment: str,
) -> None:
    home = tmp_path / ".codex"
    escaped = (
        rrule.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    )
    _write_automation(
        home,
        "lossy",
        'name = "Lossy"\nprompt = "Do it"\nstatus = "ACTIVE"\n'
        f'rrule = "{escaped}"\n',
    )

    tasks, warnings, _, _ = discover_codex_scheduled_tasks(home)

    assert tasks[0].schedule_type == "unsupported"
    assert tasks[0].cron == ""
    assert reason_fragment in tasks[0].metadata["unsupported_reason"]
    assert any("not converted" in item for item in warnings)


def test_explicit_timezone_and_one_time_schedule_are_preserved(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".codex"
    _write_automation(
        home,
        "once",
        """
name = "One time"
prompt = "Do it once"
status = "PAUSED"
run_at = "2030-01-02T09:30:00"
timezone = "America/New_York"

[schedule]
type = "once"
""",
    )

    tasks, warnings, _, _ = discover_codex_scheduled_tasks(home)

    task = tasks[0]
    assert task.schedule_type == "once"
    assert task.run_at is not None
    assert task.run_at.isoformat() == "2030-01-02T09:30:00-05:00"
    assert task.timezone == "America/New_York"
    assert task.enabled is False
    assert task.metadata["timezone_inferred"] is False
    assert not any("stored no timezone" in item for item in warnings)


def test_project_id_resolves_workspace_without_copying_project_state(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".codex"
    home.mkdir()
    (home / ".codex-global-state.json").write_text(
        '{"local-projects":{"project-1":{"id":"project-1",'
        '"rootPaths":["/project/one","/project/two"]}}}',
        encoding="utf-8",
    )
    _write_automation(
        home,
        "project-task",
        """
name = "Project task"
prompt = "Inspect the project"
status = "ACTIVE"
rrule = "FREQ=DAILY;BYHOUR=10;BYMINUTE=0"
projectId = "project-1"
""",
    )

    tasks, warnings, _, _ = discover_codex_scheduled_tasks(home)

    assert tasks[0].cwd == "/project/one"
    assert tasks[0].metadata["source_cwds"] == [
        "/project/one",
        "/project/two",
    ]
    assert any("multiple workspaces" in item for item in warnings)


def test_plain_schedule_files_use_the_shared_no_follow_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".codex"
    home.mkdir()
    (home / ".codex-global-state.json").write_text(
        '{"local-projects":{"project-1":{"id":"project-1",'
        '"rootPaths":["/project/one"]}}}',
        encoding="utf-8",
    )
    _write_automation(
        home,
        "project-task",
        'name = "Project task"\nprompt = "Inspect"\nstatus = "ACTIVE"\n'
        'rrule = "FREQ=DAILY;BYHOUR=10;BYMINUTE=0"\n'
        'projectId = "project-1"\n',
    )

    def fail_path_open(_self, *args, **kwargs):
        del args, kwargs
        raise AssertionError("ordinary schedule reads must use safe_files")

    monkeypatch.setattr(Path, "open", fail_path_open)

    tasks, warnings, discovered_count, _ = discover_codex_scheduled_tasks(
        home,
    )

    assert discovered_count == 1
    assert tasks[0].cwd == "/project/one"
    assert not any("Could not" in warning for warning in warnings)


def test_schema_drift_corruption_and_bad_rows_do_not_abort_discovery(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".codex"
    _write_automation(home, "broken-toml", "this is not = valid TOML")
    corrupt_database = home / "sqlite" / "a-corrupt.db"
    corrupt_database.parent.mkdir(parents=True)
    corrupt_database.write_bytes(b"not a sqlite database")
    database = home / "sqlite" / "b-schema-drift.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE automations "
            "(id TEXT, rrule TEXT, status TEXT, cwds TEXT)",
        )
        connection.execute(
            "INSERT INTO automations VALUES (?, ?, ?, ?)",
            (
                "recoverable",
                "FREQ=DAILY;BYHOUR=6;BYMINUTE=0",
                "MYSTERY",
                "not-json",
            ),
        )
        connection.execute(
            "INSERT INTO automations VALUES (?, ?, ?, ?)",
            (None, "FREQ=DAILY;BYHOUR=7;BYMINUTE=0", "ACTIVE", "[]"),
        )
        connection.execute("CREATE TABLE automation_runs (thread_id TEXT)")
        connection.execute(
            "INSERT INTO automation_runs VALUES (?)",
            ("run-from-new-schema",),
        )

    (
        tasks,
        warnings,
        discovered_count,
        run_ids,
    ) = discover_codex_scheduled_tasks(home)

    assert [task.source_id for task in tasks] == ["recoverable"]
    assert tasks[0].schedule_type == "cron"
    assert tasks[0].enabled is False
    assert tasks[0].prompt == ""
    assert discovered_count == 2  # corrupt TOML candidate + recoverable DB id
    assert run_ids == {"run-from-new-schema"}
    warning_text = "\n".join(warnings)
    assert "Could not parse Codex automation 'broken-toml'" in warning_text
    assert (
        "Could not read Codex automation database a-corrupt.db" in warning_text
    )
    assert "unknown source status" in warning_text
    assert "malformed cwds" in warning_text
    assert "without an id" in warning_text


def test_missing_source_timezone_reports_utc_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        codex_schedules,
        "_local_timezone_name",
        lambda: ("UTC", "utc_fallback"),
    )
    home = tmp_path / ".codex"
    _write_automation(
        home,
        "utc-task",
        'name = "UTC"\nprompt = "Do it"\nstatus = "ACTIVE"\n'
        'rrule = "FREQ=DAILY;BYHOUR=9;BYMINUTE=0"\n',
    )

    tasks, warnings, _, _ = discover_codex_scheduled_tasks(home)

    assert tasks[0].timezone == "UTC"
    assert tasks[0].metadata["timezone_source"] == "utc_fallback"
    assert any("UTC was used" in item for item in warnings)


def test_missing_codex_schedule_stores_are_an_empty_inventory(
    tmp_path: Path,
) -> None:
    assert discover_codex_scheduled_tasks(tmp_path / ".codex") == (
        [],
        [],
        0,
        set(),
    )


@pytest.mark.parametrize(
    ("prompt", "reason"),
    [
        pytest.param(
            "PROMPT_MUST_NOT_LEAK_"
            + "x" * (codex_schedule_reader._MAX_PROMPT_CHARS + 1),
            "source_prompt_exceeds_limit",
            id="character-limit",
        ),
        pytest.param(
            "PROMPT_MUST_NOT_LEAK_\x01tail",
            "source_prompt_unsafe",
            id="control-character",
        ),
    ],
)
def test_unsafe_prompt_is_omitted_audited_and_never_scheduled(
    tmp_path: Path,
    prompt: str,
    reason: str,
) -> None:
    home = tmp_path / ".codex"
    database = home / "sqlite" / "codex-dev.db"
    _create_codex_database(database)
    with sqlite3.connect(database) as connection:
        _insert_automation(
            connection,
            "unsafe-prompt",
            "Safe title",
            prompt,
            "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        )

    tasks, warnings, discovered_count, _ = discover_codex_scheduled_tasks(home)

    assert discovered_count == 1
    assert len(tasks) == 1
    task = tasks[0]
    assert task.prompt == ""
    assert task.schedule_type == "unsupported"
    assert task.cron == ""
    assert task.run_at is None
    assert task.metadata["unsupported_reason"] == reason
    audit = task.metadata["prompt_audit"]
    encoded = prompt.encode("utf-8")
    assert audit == {
        "disposition": "omitted",
        "original_chars": len(prompt),
        "original_bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }
    exported = json.dumps(task.model_dump(mode="json"), ensure_ascii=False)
    assert "PROMPT_MUST_NOT_LEAK" not in exported
    assert "PROMPT_MUST_NOT_LEAK" not in "\n".join(warnings)


def test_non_text_prompt_is_omitted_and_hashed_without_blob_leakage(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".codex"
    database = home / "sqlite" / "codex-dev.db"
    _create_codex_database(database)
    prompt = b"BLOB_PROMPT_MUST_NOT_LEAK\x00tail"
    with sqlite3.connect(database) as connection:
        _insert_automation(
            connection,
            "blob-prompt",
            "Safe title",
            sqlite3.Binary(prompt),
            "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        )

    tasks, warnings, _, _ = discover_codex_scheduled_tasks(home)

    task = tasks[0]
    assert task.prompt == ""
    assert task.schedule_type == "unsupported"
    assert task.metadata["unsupported_reason"] == "source_prompt_unsafe"
    assert task.metadata["prompt_audit"] == {
        "disposition": "omitted",
        "original_chars": 0,
        "original_bytes": len(prompt),
        "sha256": hashlib.sha256(prompt).hexdigest(),
    }
    exported = json.dumps(task.model_dump(mode="json"), ensure_ascii=False)
    assert "BLOB_PROMPT_MUST_NOT_LEAK" not in exported
    assert "BLOB_PROMPT_MUST_NOT_LEAK" not in "\n".join(warnings)


def test_untrusted_fields_are_bounded_before_entering_task_metadata(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".codex"
    database = home / "sqlite" / "codex-dev.db"
    database.parent.mkdir(parents=True)
    huge_rrule = "FREQ=DAILY;" + "X" * (
        codex_schedule_reader._MAX_RRULE_CHARS + 1
    )
    unsafe_cwd = "/tmp/CWD_MUST_NOT_LEAK\x00tail"
    unsafe_timezone = "Z" * (codex_schedule_reader._MAX_TIMEZONE_CHARS + 1)
    cwd_values = [f"/workspace/{index}" for index in range(30)]
    cwd_values.append("/unsafe\x02path")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE automations ("
            "id TEXT, name TEXT, prompt TEXT, status TEXT, cwd TEXT, "
            "cwds TEXT, rrule TEXT, timezone TEXT, model TEXT, "
            "next_run_at TEXT)",
        )
        connection.execute(
            "INSERT INTO automations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "bounded-fields",
                "Title\x01" + "T" * 500,
                "Safe prompt",
                "ACTIVE",
                unsafe_cwd,
                json.dumps(cwd_values),
                huge_rrule,
                unsafe_timezone,
                "M" * 10_000 + "\x03",
                "N" * 10_000,
            ),
        )
        connection.execute("CREATE TABLE automation_runs (thread_id TEXT)")
        connection.execute(
            "INSERT INTO automation_runs VALUES (?)",
            ("unsafe-thread\x04",),
        )

    (
        tasks,
        warnings,
        discovered_count,
        run_ids,
    ) = discover_codex_scheduled_tasks(home)

    assert discovered_count == 1
    assert not run_ids
    assert len(tasks) == 1
    task = tasks[0]
    assert task.schedule_type == "unsupported"
    assert task.cron == ""
    assert task.cwd == "/workspace/0"
    assert len(task.name) <= codex_schedule_reader._MAX_TITLE_CHARS
    assert not codex_schedule_reader._contains_control(task.name)
    assert task.metadata["source_rrule"] == ""
    assert (
        task.metadata["rrule_audit"]["sha256"]
        == hashlib.sha256(huge_rrule.encode("utf-8")).hexdigest()
    )
    assert (
        task.metadata["cwd_audit"]["sha256"]
        == hashlib.sha256(unsafe_cwd.encode("utf-8")).hexdigest()
    )
    assert task.metadata["timezone_audit"]["original_chars"] == len(
        unsafe_timezone,
    )
    assert len(task.metadata["source_cwds"]) <= 16
    assert len(task.metadata["model"]) <= 256
    assert len(task.metadata["source_next_run_at"]) <= 256
    exported = json.dumps(task.model_dump(mode="json"), ensure_ascii=False)
    assert "CWD_MUST_NOT_LEAK" not in exported
    assert not any(
        codex_schedule_reader._contains_control(value)
        for value in task.metadata.values()
        if isinstance(value, str)
    )
    assert any("unsafe values" in item for item in warnings)


def test_unsafe_source_ids_are_skipped_but_counted_without_leaking(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".codex"
    oversized_id = "OVERSIZED_ID_MUST_NOT_LEAK_" + "x" * (
        codex_schedule_reader._MAX_SOURCE_ID_CHARS + 1
    )
    _write_automation(
        home,
        "unsafe-toml-record",
        f'id = "{oversized_id}"\n'
        'name = "Unsafe"\nprompt = "Do it"\nstatus = "ACTIVE"\n'
        'rrule = "FREQ=DAILY;BYHOUR=9;BYMINUTE=0"\n',
    )
    database = home / "sqlite" / "codex-dev.db"
    _create_codex_database(database)
    unsafe_sqlite_id = "SQLITE_ID_MUST_NOT_LEAK\x01"
    with sqlite3.connect(database) as connection:
        _insert_automation(
            connection,
            unsafe_sqlite_id,
            "Unsafe",
            "Do it",
            "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        )

    tasks, warnings, discovered_count, _ = discover_codex_scheduled_tasks(home)

    assert not tasks
    assert discovered_count == 2
    warning_text = "\n".join(warnings)
    assert "unsafe or oversized" in warning_text
    assert "OVERSIZED_ID_MUST_NOT_LEAK" not in warning_text
    assert "SQLITE_ID_MUST_NOT_LEAK" not in warning_text
