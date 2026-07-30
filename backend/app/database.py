from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Iterator


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = BASE_DIR / "data" / "scheduler.db"


def user_data_directory() -> Path:
    """返回桌面版可持久写入的用户数据目录。"""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ProductionLineScheduler"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "ProductionLineScheduler"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "production-line-scheduler"


def database_path() -> Path:
    if "SCHEDULER_DB_PATH" in os.environ:
        return Path(os.environ["SCHEDULER_DB_PATH"])
    if getattr(sys, "frozen", False):
        target = user_data_directory() / "scheduler.db"
        migrate_legacy_database(target)
        return target
    return DEFAULT_DB_PATH


def migrate_legacy_database(target: Path) -> None:
    """首次运行桌面版时，保留同一项目目录中的旧版网页数据。"""
    if target.exists():
        return

    executable = Path(sys.executable).resolve()
    candidates = [parent / "backend" / "data" / "scheduler.db" for parent in executable.parents]
    candidates.append(Path.cwd() / "backend" / "data" / "scheduler.db")
    source = next((candidate for candidate in candidates if candidate.is_file()), None)
    if source is None:
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    connection = connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    daily_hours REAL NOT NULL DEFAULT 7.5,
    efficiency REAL NOT NULL DEFAULT 0.9,
    overtime_limit REAL NOT NULL DEFAULT 4.0,
    overtime_block_hours REAL NOT NULL DEFAULT 4.0,
    shortage_threshold REAL NOT NULL DEFAULT 16.875,
    green_threshold REAL NOT NULL DEFAULT 0.8,
    yellow_threshold REAL NOT NULL DEFAULT 1.0,
    daily_efficiency_low_threshold REAL NOT NULL DEFAULT 0.8,
    daily_efficiency_target_threshold REAL NOT NULL DEFAULT 0.9,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    standard_hours REAL NOT NULL CHECK (standard_hours > 0),
    is_accessory INTEGER NOT NULL DEFAULT 1,
    is_assembly INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    employee_type TEXT NOT NULL CHECK (employee_type IN ('core', 'backup')),
    overtime_limit REAL,
    weekly_work_days INTEGER NOT NULL DEFAULT 5 CHECK (weekly_work_days BETWEEN 1 AND 7),
    unavailable_weekdays TEXT NOT NULL DEFAULT '[5, 6]',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS employee_skills (
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE RESTRICT,
    priority_level INTEGER NOT NULL DEFAULT 1 CHECK (priority_level IN (1, 2, 3)),
    PRIMARY KEY (employee_id, part_id)
);

CREATE TABLE IF NOT EXISTS part_employee_priorities (
    part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
    priority_level INTEGER NOT NULL CHECK (priority_level IN (1, 2, 3)),
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    PRIMARY KEY (part_id, priority_level),
    UNIQUE (part_id, employee_id)
);

CREATE TABLE IF NOT EXISTS week_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL UNIQUE,
    include_weekend INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'shortage', 'ready', 'confirmed')),
    settings_snapshot TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS part_demands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_id INTEGER NOT NULL REFERENCES week_plans(id) ON DELETE CASCADE,
    part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE RESTRICT,
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    part_code_snapshot TEXT NOT NULL,
    part_name_snapshot TEXT NOT NULL,
    standard_hours_snapshot REAL NOT NULL,
    UNIQUE (week_id, part_id)
);

CREATE TABLE IF NOT EXISTS daily_availability (
    week_id INTEGER NOT NULL REFERENCES week_plans(id) ON DELETE CASCADE,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    work_date TEXT NOT NULL,
    hours REAL NOT NULL CHECK (hours >= 0),
    is_manual INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (week_id, employee_id, work_date)
);

CREATE TABLE IF NOT EXISTS week_members (
    week_id INTEGER NOT NULL REFERENCES week_plans(id) ON DELETE CASCADE,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'reinforcement',
    PRIMARY KEY (week_id, employee_id)
);

CREATE TABLE IF NOT EXISTS machines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS machine_bom_items (
    machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE RESTRICT,
    quantity_per_machine INTEGER NOT NULL CHECK (quantity_per_machine > 0),
    PRIMARY KEY (machine_id, part_id)
);

CREATE TABLE IF NOT EXISTS production_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_type TEXT NOT NULL CHECK (order_type IN ('machine', 'accessory')),
    machine_id INTEGER REFERENCES machines(id) ON DELETE RESTRICT,
    accessory_part_id INTEGER REFERENCES parts(id) ON DELETE RESTRICT,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'cancelled', 'legacy')),
    needs_generation INTEGER NOT NULL DEFAULT 1,
    source_code_snapshot TEXT NOT NULL,
    source_name_snapshot TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'manual'
        CHECK (origin IN ('manual', 'accessory_import', 'machine_plan_import', 'legacy')),
    import_week_start TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS production_order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES production_orders(id) ON DELETE CASCADE,
    part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE RESTRICT,
    quantity_per_unit INTEGER NOT NULL CHECK (quantity_per_unit > 0),
    required_quantity INTEGER NOT NULL CHECK (required_quantity > 0),
    part_code_snapshot TEXT NOT NULL,
    part_name_snapshot TEXT NOT NULL,
    standard_hours_snapshot REAL NOT NULL,
    is_dual_usage_snapshot INTEGER NOT NULL DEFAULT 0,
    UNIQUE (order_id, part_id)
);

CREATE TABLE IF NOT EXISTS week_order_demands (
    week_id INTEGER NOT NULL REFERENCES week_plans(id) ON DELETE CASCADE,
    order_item_id INTEGER NOT NULL REFERENCES production_order_items(id) ON DELETE CASCADE,
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    PRIMARY KEY (week_id, order_item_id)
);

CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_id INTEGER NOT NULL REFERENCES week_plans(id) ON DELETE CASCADE,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE RESTRICT,
    part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE RESTRICT,
    work_date TEXT NOT NULL,
    target_date TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    standard_hours_snapshot REAL NOT NULL,
    order_item_id INTEGER REFERENCES production_order_items(id) ON DELETE RESTRICT,
    source TEXT NOT NULL DEFAULT 'generated'
        CHECK (source IN ('generated', 'manual')),
    UNIQUE (week_id, employee_id, order_item_id, work_date, target_date)
);

CREATE TABLE IF NOT EXISTS overtime_approvals (
    week_id INTEGER NOT NULL REFERENCES week_plans(id) ON DELETE CASCADE,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    work_date TEXT NOT NULL,
    hours REAL NOT NULL CHECK (hours >= 0),
    is_manual INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (week_id, employee_id, work_date)
);

CREATE TABLE IF NOT EXISTS week_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_id INTEGER NOT NULL REFERENCES week_plans(id) ON DELETE CASCADE,
    snapshot_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'applied', 'cancelled')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_week_adjustments_active
ON week_adjustments(week_id) WHERE status = 'active';
"""


def _backup_before_v2_migration() -> Path | None:
    """旧版数据库首次升级前创建一致性备份，避免迁移中断损伤用户数据。"""
    path = database_path()
    if not path.is_file():
        return None
    probe = sqlite3.connect(path)
    try:
        tables = {
            str(row[0])
            for row in probe.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "parts" not in tables:
            return None
        part_columns = {
            str(row[1]) for row in probe.execute("PRAGMA table_info(parts)").fetchall()
        }
        if {"is_accessory", "is_assembly"}.issubset(part_columns) and {
            "machines",
            "production_orders",
            "week_order_demands",
        }.issubset(tables):
            return None
        backup_path = path.with_name(f"{path.stem}.pre-v2-backup{path.suffix}")
        if backup_path.exists():
            return backup_path
        backup = sqlite3.connect(backup_path)
        try:
            probe.backup(backup)
        finally:
            backup.close()
        return backup_path
    finally:
        probe.close()


def init_db() -> None:
    _backup_before_v2_migration()
    with transaction() as connection:
        connection.executescript(SCHEMA)
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(settings)").fetchall()
        }
        if "shortage_threshold" not in columns:
            connection.execute(
                "ALTER TABLE settings ADD COLUMN shortage_threshold REAL NOT NULL DEFAULT 16.875"
            )
        if "overtime_block_hours" not in columns:
            connection.execute(
                "ALTER TABLE settings ADD COLUMN overtime_block_hours REAL NOT NULL DEFAULT 4.0"
            )
            connection.execute(
                "UPDATE settings SET overtime_limit = 4.0 WHERE overtime_limit = 2.0"
            )
        if "daily_efficiency_low_threshold" not in columns:
            connection.execute(
                "ALTER TABLE settings ADD COLUMN daily_efficiency_low_threshold REAL NOT NULL DEFAULT 0.8"
            )
        if "daily_efficiency_target_threshold" not in columns:
            connection.execute(
                "ALTER TABLE settings ADD COLUMN daily_efficiency_target_threshold REAL NOT NULL DEFAULT 0.9"
            )
        employee_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(employees)").fetchall()
        }
        if "weekly_work_days" not in employee_columns:
            connection.execute(
                "ALTER TABLE employees ADD COLUMN weekly_work_days INTEGER NOT NULL DEFAULT 5"
            )
        if "unavailable_weekdays" not in employee_columns:
            connection.execute(
                "ALTER TABLE employees ADD COLUMN unavailable_weekdays TEXT NOT NULL DEFAULT '[5, 6]'"
            )
        skill_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(employee_skills)"
            ).fetchall()
        }
        if "priority_level" not in skill_columns:
            connection.execute(
                "ALTER TABLE employee_skills ADD COLUMN priority_level INTEGER NOT NULL DEFAULT 1"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS part_employee_priorities (
                part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
                priority_level INTEGER NOT NULL CHECK (priority_level IN (1, 2, 3)),
                employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
                PRIMARY KEY (part_id, priority_level),
                UNIQUE (part_id, employee_id)
            )
            """
        )
        skill_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'employee_skills'"
            ).fetchone()["sql"]
        )
        if "IN (1, 2, 3)" not in skill_sql:
            connection.executescript(
                """
                CREATE TABLE employee_skills_v3 (
                    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
                    part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE RESTRICT,
                    priority_level INTEGER NOT NULL DEFAULT 1
                        CHECK (priority_level IN (1, 2, 3)),
                    PRIMARY KEY (employee_id, part_id)
                );
                INSERT INTO employee_skills_v3
                    (employee_id, part_id, priority_level)
                SELECT employee_id, part_id, priority_level
                FROM employee_skills;
                DROP TABLE employee_skills;
                ALTER TABLE employee_skills_v3 RENAME TO employee_skills;
                """
            )
        priority_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'part_employee_priorities'"
            ).fetchone()["sql"]
        )
        if "IN (1, 2, 3)" not in priority_sql:
            connection.executescript(
                """
                CREATE TABLE part_employee_priorities_v3 (
                    part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
                    priority_level INTEGER NOT NULL
                        CHECK (priority_level IN (1, 2, 3)),
                    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
                    PRIMARY KEY (part_id, priority_level),
                    UNIQUE (part_id, employee_id)
                );
                INSERT INTO part_employee_priorities_v3
                    (part_id, priority_level, employee_id)
                SELECT part_id, priority_level, employee_id
                FROM part_employee_priorities;
                DROP TABLE part_employee_priorities;
                ALTER TABLE part_employee_priorities_v3
                    RENAME TO part_employee_priorities;
                """
            )
        if not connection.execute(
            "SELECT 1 FROM part_employee_priorities LIMIT 1"
        ).fetchone():
            part_ids = connection.execute(
                "SELECT DISTINCT part_id FROM employee_skills ORDER BY part_id"
            ).fetchall()
            for part in part_ids:
                candidates = connection.execute(
                    """
                    SELECT es.employee_id
                    FROM employee_skills es
                    JOIN employees e ON e.id = es.employee_id
                    WHERE es.part_id = ?
                    ORDER BY
                        CASE WHEN e.active = 1 THEN 0 ELSE 1 END,
                        CASE e.employee_type WHEN 'core' THEN 0 ELSE 1 END,
                        e.id
                    LIMIT 2
                    """,
                    (part["part_id"],),
                ).fetchall()
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO part_employee_priorities
                        (part_id, priority_level, employee_id)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (part["part_id"], index + 1, row["employee_id"])
                        for index, row in enumerate(candidates)
                    ],
                )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS week_adjustments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_id INTEGER NOT NULL REFERENCES week_plans(id) ON DELETE CASCADE,
                snapshot_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'applied', 'cancelled')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_week_adjustments_active
            ON week_adjustments(week_id) WHERE status = 'active'
            """
        )
        availability_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(daily_availability)"
            ).fetchall()
        }
        if "is_manual" not in availability_columns:
            connection.execute(
                "ALTER TABLE daily_availability ADD COLUMN is_manual INTEGER NOT NULL DEFAULT 0"
            )
            # 升级已有数据时，根据当时的员工出勤规则识别请假、半天等人工值。
            # 这样后续修改全局每日工时，只更新默认值，不覆盖人工调整。
            availability_rows = connection.execute(
                """
                SELECT da.week_id, da.employee_id, da.work_date, da.hours,
                       wp.week_start, wp.include_weekend, wp.settings_snapshot,
                       e.weekly_work_days, e.unavailable_weekdays
                FROM daily_availability da
                JOIN week_plans wp ON wp.id = da.week_id
                JOIN employees e ON e.id = da.employee_id
                """
            ).fetchall()
            grouped_rows: dict[tuple[int, int], list[sqlite3.Row]] = {}
            for row in availability_rows:
                grouped_rows.setdefault(
                    (int(row["week_id"]), int(row["employee_id"])), []
                ).append(row)
            manual_entries: list[tuple[int, int, str]] = []
            for rows in grouped_rows.values():
                sample = rows[0]
                settings = json.loads(sample["settings_snapshot"])
                start = date.fromisoformat(sample["week_start"])
                day_count = 7 if bool(sample["include_weekend"]) else 5
                days = [
                    (start + timedelta(days=offset)).isoformat()
                    for offset in range(day_count)
                ]
                try:
                    unavailable = {
                        int(value)
                        for value in json.loads(sample["unavailable_weekdays"])
                    }
                except (TypeError, ValueError, json.JSONDecodeError):
                    unavailable = {5, 6}
                eligible = [
                    day
                    for day in days
                    if date.fromisoformat(day).weekday() not in unavailable
                ]
                work_day_count = min(
                    int(sample["weekly_work_days"]), len(eligible)
                )
                if work_day_count == 0:
                    selected: set[str] = set()
                elif work_day_count == 1:
                    selected = {eligible[(len(eligible) - 1) // 2]}
                elif work_day_count == len(eligible):
                    selected = set(eligible)
                else:
                    indexes = {
                        round(
                            index
                            * (len(eligible) - 1)
                            / (work_day_count - 1)
                        )
                        for index in range(work_day_count)
                    }
                    selected = {eligible[index] for index in indexes}
                daily_hours = float(settings.get("daily_hours", 7.5))
                expected = {
                    day: daily_hours if day in selected else 0.0 for day in days
                }
                for row in rows:
                    if abs(
                        float(row["hours"])
                        - expected.get(row["work_date"], 0.0)
                    ) > 0.0001:
                        manual_entries.append(
                            (
                                int(row["week_id"]),
                                int(row["employee_id"]),
                                row["work_date"],
                            )
                        )
            connection.executemany(
                """
                UPDATE daily_availability SET is_manual = 1
                WHERE week_id = ? AND employee_id = ? AND work_date = ?
                """,
                manual_entries,
            )
        overtime_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(overtime_approvals)"
            ).fetchall()
        }
        if "is_manual" not in overtime_columns:
            connection.execute(
                "ALTER TABLE overtime_approvals ADD COLUMN is_manual INTEGER NOT NULL DEFAULT 0"
            )
        part_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(parts)").fetchall()
        }
        if "is_accessory" not in part_columns:
            connection.execute(
                "ALTER TABLE parts ADD COLUMN is_accessory INTEGER NOT NULL DEFAULT 1"
            )
        if "is_assembly" not in part_columns:
            connection.execute(
                "ALTER TABLE parts ADD COLUMN is_assembly INTEGER NOT NULL DEFAULT 0"
            )
        assignment_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(assignments)").fetchall()
        }
        if "order_item_id" not in assignment_columns:
            connection.execute("ALTER TABLE assignments RENAME TO assignments_before_orders")
            connection.execute(
                """
                CREATE TABLE assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    week_id INTEGER NOT NULL REFERENCES week_plans(id) ON DELETE CASCADE,
                    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE RESTRICT,
                    part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE RESTRICT,
                    work_date TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    standard_hours_snapshot REAL NOT NULL,
                    order_item_id INTEGER REFERENCES production_order_items(id) ON DELETE RESTRICT,
                    source TEXT NOT NULL DEFAULT 'generated'
                        CHECK (source IN ('generated', 'manual')),
                    UNIQUE (week_id, employee_id, order_item_id, work_date)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO assignments
                    (id, week_id, employee_id, part_id, work_date, quantity,
                     standard_hours_snapshot, source)
                SELECT id, week_id, employee_id, part_id, work_date, quantity,
                       standard_hours_snapshot, source
                FROM assignments_before_orders
                """
            )
            connection.execute("DROP TABLE assignments_before_orders")
        assignment_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(assignments)").fetchall()
        }
        if "target_date" not in assignment_columns:
            connection.execute(
                "ALTER TABLE assignments RENAME TO assignments_before_target_date"
            )
            connection.execute(
                """
                CREATE TABLE assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    week_id INTEGER NOT NULL REFERENCES week_plans(id) ON DELETE CASCADE,
                    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE RESTRICT,
                    part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE RESTRICT,
                    work_date TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    standard_hours_snapshot REAL NOT NULL,
                    order_item_id INTEGER REFERENCES production_order_items(id) ON DELETE RESTRICT,
                    source TEXT NOT NULL DEFAULT 'generated'
                        CHECK (source IN ('generated', 'manual')),
                    UNIQUE (
                        week_id, employee_id, order_item_id, work_date, target_date
                    )
                )
                """
            )
            connection.execute(
                """
                INSERT INTO assignments
                    (id, week_id, employee_id, part_id, work_date, target_date,
                     quantity, standard_hours_snapshot, order_item_id, source)
                SELECT id, week_id, employee_id, part_id, work_date, work_date,
                       quantity, standard_hours_snapshot, order_item_id, source
                FROM assignments_before_target_date
                """
            )
            connection.execute("DROP TABLE assignments_before_target_date")
        order_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(production_orders)").fetchall()
        }
        if "needs_generation" not in order_columns:
            connection.execute(
                "ALTER TABLE production_orders ADD COLUMN needs_generation INTEGER NOT NULL DEFAULT 0"
            )
        if "origin" not in order_columns:
            connection.execute(
                "ALTER TABLE production_orders ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'"
            )
            connection.execute(
                "UPDATE production_orders SET origin = 'legacy' WHERE status = 'legacy'"
            )
        if "import_week_start" not in order_columns:
            connection.execute(
                "ALTER TABLE production_orders ADD COLUMN import_week_start TEXT"
            )
        order_item_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(production_order_items)"
            ).fetchall()
        }
        if "is_dual_usage_snapshot" not in order_item_columns:
            connection.execute(
                "ALTER TABLE production_order_items ADD COLUMN is_dual_usage_snapshot INTEGER NOT NULL DEFAULT 0"
            )
            connection.execute(
                """
                UPDATE production_order_items
                SET is_dual_usage_snapshot = COALESCE(
                    (
                        SELECT CASE
                            WHEN p.is_accessory = 1 AND p.is_assembly = 1 THEN 1
                            ELSE 0
                        END
                        FROM parts p
                        WHERE p.id = production_order_items.part_id
                    ),
                    0
                )
                """
            )

        # 把旧版逐周零件需求映射为只读历史生产任务，便于新旧数据共同展示。
        legacy_demands = connection.execute(
            """
            SELECT pd.*, wp.week_start, wp.include_weekend
            FROM part_demands pd
            JOIN week_plans wp ON wp.id = pd.week_id
            LEFT JOIN week_order_demands wod
              ON wod.week_id = pd.week_id
            WHERE wod.week_id IS NULL
            ORDER BY pd.id
            """
        ).fetchall()
        for demand in legacy_demands:
            start = date.fromisoformat(demand["week_start"])
            end = start + timedelta(days=6 if bool(demand["include_weekend"]) else 4)
            order_cursor = connection.execute(
                """
                INSERT INTO production_orders
                    (order_type, accessory_part_id, quantity, start_date, end_date,
                     status, needs_generation, source_code_snapshot,
                     source_name_snapshot, origin)
                VALUES ('accessory', ?, ?, ?, ?, 'legacy', 0, ?, ?, 'legacy')
                """,
                (
                    demand["part_id"], demand["quantity"], start.isoformat(),
                    end.isoformat(), demand["part_code_snapshot"],
                    demand["part_name_snapshot"],
                ),
            )
            item_cursor = connection.execute(
                """
                INSERT INTO production_order_items
                    (order_id, part_id, quantity_per_unit, required_quantity,
                     part_code_snapshot, part_name_snapshot, standard_hours_snapshot)
                VALUES (?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    order_cursor.lastrowid, demand["part_id"], demand["quantity"],
                    demand["part_code_snapshot"], demand["part_name_snapshot"],
                    demand["standard_hours_snapshot"],
                ),
            )
            connection.execute(
                "INSERT INTO week_order_demands (week_id, order_item_id, quantity) VALUES (?, ?, ?)",
                (demand["week_id"], item_cursor.lastrowid, demand["quantity"]),
            )
            connection.execute(
                """
                UPDATE assignments SET order_item_id = ?
                WHERE week_id = ? AND part_id = ? AND order_item_id IS NULL
                """,
                (item_cursor.lastrowid, demand["week_id"], demand["part_id"]),
            )
        # 升级前的已确认计划默认在其启用的每一天都可出勤；补齐快照，避免
        # 新增的员工常规休息规则改变历史周计划的负荷显示。
        confirmed_weeks = connection.execute(
            "SELECT * FROM week_plans WHERE status = 'confirmed'"
        ).fetchall()
        for week in confirmed_weeks:
            settings = json.loads(week["settings_snapshot"])
            day_count = 7 if bool(week["include_weekend"]) else 5
            start = date.fromisoformat(week["week_start"])
            days = [
                (start + timedelta(days=offset)).isoformat()
                for offset in range(day_count)
            ]
            employees = connection.execute(
                """
                SELECT DISTINCT e.id
                FROM employees e
                LEFT JOIN week_members wm
                  ON wm.employee_id = e.id AND wm.week_id = ?
                WHERE (e.active = 1 AND e.employee_type = 'core')
                   OR wm.week_id IS NOT NULL
                """,
                (week["id"],),
            ).fetchall()
            connection.executemany(
                """
                INSERT OR IGNORE INTO daily_availability
                    (week_id, employee_id, work_date, hours)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (week["id"], employee["id"], day, settings["daily_hours"])
                    for employee in employees
                    for day in days
                ],
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO settings
                (id, daily_hours, efficiency, overtime_limit, overtime_block_hours,
                 shortage_threshold,
                 green_threshold, yellow_threshold,
                 daily_efficiency_low_threshold,
                 daily_efficiency_target_threshold)
            VALUES (1, 7.5, 0.9, 4.0, 4.0, 16.875, 0.8, 1.0, 0.8, 0.9)
            """
        )
        # 3.1 起加班采用全局固定班次：员工不再保留个人上限，
        # 未确认排班中的旧自由加班值统一为“0或当前完整班次”。
        connection.execute(
            "UPDATE settings SET overtime_limit = overtime_block_hours WHERE id = 1"
        )
        connection.execute("UPDATE employees SET overtime_limit = NULL")
        connection.execute(
            """
            UPDATE employees
            SET weekly_work_days = 5, unavailable_weekdays = '[5, 6]'
            """
        )
        # 员工资料不再保存长期工作日规则。未确认周中尚未被管理员逐周
        # 修改的出勤记录，统一恢复为周一至周五；is_manual=1 的逐周设置保留。
        connection.execute(
            """
            UPDATE daily_availability
            SET hours = CASE
                WHEN CAST(strftime('%w', work_date) AS INTEGER) IN (0, 6)
                    THEN 0
                ELSE (SELECT daily_hours FROM settings WHERE id = 1)
            END
            WHERE is_manual = 0
              AND week_id IN (
                  SELECT id FROM week_plans WHERE status != 'confirmed'
              )
            """
        )
        connection.execute(
            """
            DELETE FROM overtime_approvals
            WHERE hours <= 0
              AND week_id IN (
                  SELECT id FROM week_plans WHERE status != 'confirmed'
              )
            """
        )
        connection.execute(
            """
            UPDATE overtime_approvals
            SET hours = (SELECT overtime_block_hours FROM settings WHERE id = 1)
            WHERE hours > 0
              AND week_id IN (
                  SELECT id FROM week_plans WHERE status != 'confirmed'
              )
            """
        )
