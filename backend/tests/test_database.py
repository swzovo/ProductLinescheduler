from pathlib import Path
import sqlite3
import json

from backend.app import database


def test_existing_settings_table_gets_shortage_threshold_column(tmp_path, monkeypatch):
    path = tmp_path / "scheduler.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE settings (
            id INTEGER PRIMARY KEY,
            daily_hours REAL NOT NULL,
            efficiency REAL NOT NULL,
            overtime_limit REAL NOT NULL,
            green_threshold REAL NOT NULL,
            yellow_threshold REAL NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        """
        INSERT INTO settings
            (id, daily_hours, efficiency, overtime_limit, green_threshold, yellow_threshold)
        VALUES (1, 7.5, 0.9, 2, 0.8, 1)
        """
    )
    connection.commit()
    connection.close()

    monkeypatch.setenv("SCHEDULER_DB_PATH", str(path))
    database.init_db()

    with database.connect() as migrated:
        row = migrated.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        assert row is not None
        assert row["shortage_threshold"] == 16.875
        assert row["daily_efficiency_low_threshold"] == 0.8
        assert row["daily_efficiency_target_threshold"] == 0.9
        overtime_columns = {
            item["name"]
            for item in migrated.execute(
                "PRAGMA table_info(overtime_approvals)"
            ).fetchall()
        }
        assert "is_manual" in overtime_columns


def test_existing_employee_table_gets_work_pattern_columns(tmp_path, monkeypatch):
    path = tmp_path / "scheduler.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            employee_type TEXT NOT NULL,
            overtime_limit REAL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        "INSERT INTO employees (name, employee_type) VALUES ('旧员工', 'core')"
    )
    connection.commit()
    connection.close()

    monkeypatch.setenv("SCHEDULER_DB_PATH", str(path))
    database.init_db()

    with database.connect() as migrated:
        row = migrated.execute("SELECT * FROM employees WHERE name = '旧员工'").fetchone()
        assert row is not None
        assert row["weekly_work_days"] == 5
        assert row["unavailable_weekdays"] == "[5, 6]"


def test_desktop_database_migrates_legacy_file_once(tmp_path, monkeypatch):
    project = tmp_path / "project"
    legacy = project / "backend" / "data" / "scheduler.db"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy-data")

    executable = project / "release" / "Scheduler.app" / "Contents" / "MacOS" / "Scheduler"
    destination_dir = tmp_path / "user-data"
    monkeypatch.delenv("SCHEDULER_DB_PATH", raising=False)
    monkeypatch.setattr(database.sys, "frozen", True, raising=False)
    monkeypatch.setattr(database.sys, "executable", str(executable))
    monkeypatch.setattr(database, "user_data_directory", lambda: destination_dir)

    target = database.database_path()
    assert target == destination_dir / "scheduler.db"
    assert target.read_bytes() == b"legacy-data"

    legacy.write_bytes(b"new-legacy-data")
    assert database.database_path().read_bytes() == b"legacy-data"


def test_legacy_week_demands_and_assignments_migrate_to_order_sources(tmp_path, monkeypatch):
    path = tmp_path / "scheduler.db"
    monkeypatch.setenv("SCHEDULER_DB_PATH", str(path))
    database.init_db()
    with database.transaction() as connection:
        part_id = connection.execute(
            "INSERT INTO parts (code, name, standard_hours) VALUES ('OLD-P', '旧零件', 1)"
        ).lastrowid
        employee_id = connection.execute(
            "INSERT INTO employees (name, employee_type) VALUES ('旧员工排班', 'core')"
        ).lastrowid
        week_id = connection.execute(
            """
            INSERT INTO week_plans (week_start, status, settings_snapshot)
            VALUES ('2026-07-20', 'confirmed', ?)
            """,
            (json.dumps({"daily_hours": 7.5, "efficiency": 0.9, "overtime_limit": 2, "green_threshold": 0.8, "yellow_threshold": 1}),),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO part_demands
                (week_id, part_id, quantity, part_code_snapshot, part_name_snapshot, standard_hours_snapshot)
            VALUES (?, ?, 2, 'OLD-P', '旧零件', 1)
            """,
            (week_id, part_id),
        )
        connection.execute("DROP TABLE assignments")
        connection.execute(
            """
            CREATE TABLE assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_id INTEGER NOT NULL,
                employee_id INTEGER NOT NULL,
                part_id INTEGER NOT NULL,
                work_date TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                standard_hours_snapshot REAL NOT NULL,
                source TEXT NOT NULL DEFAULT 'generated',
                UNIQUE (week_id, employee_id, part_id, work_date)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO assignments
                (week_id, employee_id, part_id, work_date, quantity, standard_hours_snapshot)
            VALUES (?, ?, ?, '2026-07-20', 2, 1)
            """,
            (week_id, employee_id, part_id),
        )

    database.init_db()
    with database.connect() as migrated:
        part = migrated.execute("SELECT * FROM parts WHERE id = ?", (part_id,)).fetchone()
        assert part["is_accessory"] == 1
        assert part["is_assembly"] == 0
        source = migrated.execute("SELECT * FROM production_orders").fetchone()
        assert source["status"] == "legacy"
        assignment = migrated.execute("SELECT * FROM assignments").fetchone()
        assert assignment["order_item_id"] is not None
        assert migrated.execute("SELECT COUNT(*) FROM week_order_demands").fetchone()[0] == 1
        assert migrated.execute("SELECT status FROM week_plans WHERE id = ?", (week_id,)).fetchone()[0] == "confirmed"


def test_v2_migration_creates_consistent_database_backup(tmp_path, monkeypatch):
    path = tmp_path / "scheduler.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE parts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            standard_hours REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        "INSERT INTO parts (code, name, standard_hours) VALUES ('SAFE-OLD', '升级前零件', 1)"
    )
    connection.commit()
    connection.close()

    monkeypatch.setenv("SCHEDULER_DB_PATH", str(path))
    database.init_db()

    backup_path = tmp_path / "scheduler.pre-v2-backup.db"
    assert backup_path.is_file()
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("SELECT code FROM parts").fetchone()[0] == "SAFE-OLD"
    with database.connect() as migrated:
        part = migrated.execute("SELECT * FROM parts WHERE code = 'SAFE-OLD'").fetchone()
        assert part["is_accessory"] == 1
        assert part["is_assembly"] == 0
