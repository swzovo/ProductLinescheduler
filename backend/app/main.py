from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .database import connect, init_db, transaction
from .cloud_sync import router as cloud_sync_router
from .order_import import preview_accessory_order_import
from .matrix_import import (
    preview_machine_bom_matrix,
    preview_machine_plan_matrix,
)
from .part_import import preview_import
from .schedule_export import render_schedule
from .scheduler import active_dates
from .schemas import (
    AssignmentsUpdate,
    AccessoryOrderImportCommit,
    AvailabilityUpdate,
    EmployeeCreate,
    EmployeeUpdate,
    LeaveAdjustmentCreate,
    MachineCreate,
    MachineMatrixImportCommit,
    MachinePlanImportCommit,
    MachineUpdate,
    PartCreate,
    PartImportCommit,
    PartUpdate,
    ProductionOrderCreate,
    ProductionOrderUpdate,
    ResolveShortage,
    ScheduleExport,
    SettingsUpdate,
    WeekCreate,
    WeekCalendarUpdate,
    WeekUpdate,
)
from .production import (
    create_order_snapshot,
    generate_cross_week,
    list_orders,
    machine_response,
    order_response,
    save_machine_bom,
    update_order_snapshot,
    generation_today,
)
from .service import (
    approve_required_overtime,
    availability_map,
    calculate_week,
    confirm_week,
    current_settings,
    default_availability_for_employee,
    employee_skill_priorities,
    employee_skills,
    ensure_editable,
    refresh_week_status,
    reset_week_schedule,
    replace_assignments,
    row_dict,
    run_generator,
    selected_employees,
    settings_from_snapshot,
    unconfirm_week,
    week_row,
)


def _sync_settings_to_unconfirmed_weeks(
    connection: sqlite3.Connection,
    settings: dict[str, float],
) -> None:
    unconfirmed_weeks = connection.execute(
        """
        SELECT * FROM week_plans
        WHERE status != 'confirmed'
        ORDER BY week_start
        """
    ).fetchall()
    capacity_changed_week_ids: list[int] = []
    for week in unconfirmed_weeks:
        week_id = int(week["id"])
        previous_settings = settings_from_snapshot(week["settings_snapshot"])
        if any(
            previous_settings[key] != settings[key]
            for key in (
                "daily_hours",
                "efficiency",
                "overtime_limit",
                "overtime_block_hours",
            )
        ):
            capacity_changed_week_ids.append(week_id)
        employees = selected_employees(connection, week_id)
        days = active_dates(
            week["week_start"], bool(week["include_weekend"])
        )
        default_entries: list[tuple[int, int, str, float]] = []
        for employee in employees:
            defaults = default_availability_for_employee(
                employee, days, settings["daily_hours"]
            )
            default_entries.extend(
                (week_id, int(employee["id"]), day, hours)
                for day, hours in defaults.items()
            )
        connection.executemany(
            """
            INSERT INTO daily_availability
                (week_id, employee_id, work_date, hours, is_manual)
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT (week_id, employee_id, work_date)
            DO UPDATE SET hours = excluded.hours
            WHERE daily_availability.is_manual = 0
            """,
            default_entries,
        )
        connection.execute(
            """
            UPDATE week_plans
            SET settings_snapshot = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (json.dumps(settings, ensure_ascii=False), week_id),
        )

    if capacity_changed_week_ids:
        placeholders = ",".join("?" for _ in capacity_changed_week_ids)
        connection.execute(
            f"""
            DELETE FROM overtime_approvals
            WHERE week_id IN ({placeholders}) AND is_manual = 0
            """,
            capacity_changed_week_ids,
        )
        connection.execute(
            """
            UPDATE production_orders
            SET needs_generation = 1, updated_at = CURRENT_TIMESTAMP
            WHERE status = 'active'
            """
        )
    for week in unconfirmed_weeks:
        refresh_week_status(connection, int(week["id"]))



@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with transaction() as connection:
        _sync_settings_to_unconfirmed_weeks(
            connection, current_settings(connection)
        )
    yield


app = FastAPI(title="单产线整机与跨周排班系统", version="3.5.13", lifespan=lifespan)
app.include_router(cloud_sync_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/settings")
def get_settings():
    with connect() as connection:
        return current_settings(connection)


@app.put("/api/settings")
def update_settings(payload: SettingsUpdate):
    with transaction() as connection:
        connection.execute(
            """
            UPDATE settings
            SET daily_hours = ?, efficiency = ?, overtime_limit = ?,
                overtime_block_hours = ?,
                shortage_threshold = ?, green_threshold = ?, yellow_threshold = ?,
                daily_efficiency_low_threshold = ?,
                daily_efficiency_target_threshold = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (
                payload.daily_hours,
                payload.efficiency,
                payload.overtime_block_hours,
                payload.overtime_block_hours,
                payload.shortage_threshold,
                payload.green_threshold,
                payload.yellow_threshold,
                payload.daily_efficiency_low_threshold,
                payload.daily_efficiency_target_threshold,
            ),
        )
        settings = current_settings(connection)
        connection.execute("UPDATE employees SET overtime_limit = NULL")
        connection.execute(
            """
            UPDATE overtime_approvals
            SET hours = ?
            WHERE hours > 0
              AND week_id IN (
                  SELECT id FROM week_plans WHERE status != 'confirmed'
              )
            """,
            (payload.overtime_block_hours,),
        )
        _sync_settings_to_unconfirmed_weeks(connection, settings)
        return settings


@app.post("/api/maintenance/clear-cache")
def clear_application_cache():
    """清理数据库运行缓存和桌面WebView缓存，不触碰任何业务资料。"""
    with connect() as connection:
        connection.execute("PRAGMA optimize")
        connection.commit()
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()

    cleared_paths: list[str] = []
    if sys.platform == "darwin":
        cache_paths = [
            Path.home() / "Library" / "Caches" / "com.local.production-line-scheduler",
            Path.home() / "Library" / "WebKit" / "com.local.production-line-scheduler" / "WebsiteData" / "NetworkCache",
            Path.home() / "Library" / "WebKit" / "com.local.production-line-scheduler" / "WebsiteData" / "CacheStorage",
        ]
        for path in cache_paths:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
                if not path.exists():
                    cleared_paths.append(str(path))
    return {
        "status": "cleared",
        "cleared_paths": cleared_paths,
        "database_checkpoint": list(checkpoint) if checkpoint is not None else [],
    }


@app.delete("/api/maintenance/schedule-history")
def delete_schedule_history():
    """删除生产需求和全部周排班，保留零件、整机、员工与系统设置。"""
    with transaction() as connection:
        counts = {
            "weeks": int(connection.execute("SELECT COUNT(*) FROM week_plans").fetchone()[0]),
            "orders": int(connection.execute("SELECT COUNT(*) FROM production_orders").fetchone()[0]),
            "assignments": int(connection.execute("SELECT COUNT(*) FROM assignments").fetchone()[0]),
        }
        # 先删除周计划，让关联排班通过外键级联清除，再删除任务快照。
        connection.execute("DELETE FROM week_plans")
        connection.execute("DELETE FROM production_orders")
        connection.execute(
            """
            DELETE FROM sqlite_sequence
            WHERE name IN (
                'week_plans', 'part_demands', 'assignments',
                'production_orders', 'production_order_items'
            )
            """
        )
        return {"status": "cleared", **counts}


@app.get("/api/parts")
def list_parts():
    with connect() as connection:
        rows = connection.execute("SELECT * FROM parts ORDER BY code, id").fetchall()
        return [part_response(row, connection) for row in rows]


def part_response(
    row: sqlite3.Row,
    connection: sqlite3.Connection | None = None,
):
    item = {
        **row_dict(row),
        "active": bool(row["active"]),
        "usage_types": [
            usage
            for usage, enabled in (
                ("accessory", bool(row["is_accessory"])),
                ("assembly", bool(row["is_assembly"])),
            )
            if enabled
        ],
    }
    item["employee_priorities"] = (
        [
            {
                "employee_id": int(skill["employee_id"]),
                "employee_name": skill["employee_name"],
                "priority_level": int(skill["priority_level"]),
            }
            for skill in connection.execute(
                """
                SELECT pep.employee_id, e.name AS employee_name,
                       pep.priority_level
                FROM part_employee_priorities pep
                JOIN employees e ON e.id = pep.employee_id
                WHERE pep.part_id = ?
                ORDER BY pep.priority_level
                """,
                (row["id"],),
            ).fetchall()
        ]
        if connection is not None
        else []
    )
    by_level = {
        int(priority["priority_level"]): priority
        for priority in item["employee_priorities"]
    }
    item["level_1_employee_id"] = (
        int(by_level[1]["employee_id"]) if 1 in by_level else None
    )
    item["level_2_employee_id"] = (
        int(by_level[2]["employee_id"]) if 2 in by_level else None
    )
    item["level_3_employee_id"] = (
        int(by_level[3]["employee_id"]) if 3 in by_level else None
    )
    item["level_1_employee"] = by_level.get(1)
    item["level_2_employee"] = by_level.get(2)
    item["level_3_employee"] = by_level.get(3)
    return item


def _save_part_employee_priorities(
    connection: sqlite3.Connection,
    part_id: int,
    priorities,
) -> None:
    if priorities is None:
        return
    employee_ids = sorted({int(item.employee_id) for item in priorities})
    if employee_ids:
        placeholders = ",".join("?" for _ in employee_ids)
        count = connection.execute(
            f"""
            SELECT COUNT(*) FROM employees
            WHERE id IN ({placeholders}) AND active = 1
            """,
            employee_ids,
        ).fetchone()[0]
        if count != len(employee_ids):
            raise HTTPException(
                status_code=422,
                detail="员工1、员工2或员工3中包含不存在或已停用的员工",
            )
    levels = [int(item.priority_level) for item in priorities]
    if len(levels) != len(set(levels)):
        raise HTTPException(
            status_code=422,
            detail="员工1、员工2和员工3每级最多选择一名员工",
        )
    connection.execute(
        "DELETE FROM part_employee_priorities WHERE part_id = ?", (part_id,)
    )
    connection.executemany(
        """
        INSERT INTO part_employee_priorities
            (employee_id, part_id, priority_level)
        VALUES (?, ?, ?)
        """,
        [
            (int(item.employee_id), part_id, int(item.priority_level))
            for item in priorities
        ],
    )
    connection.executemany(
        """
        INSERT OR IGNORE INTO employee_skills
            (employee_id, part_id, priority_level)
        VALUES (?, ?, ?)
        """,
        [
            (int(item.employee_id), part_id, int(item.priority_level))
            for item in priorities
        ],
    )


def _part_priorities_from_payload(payload: PartCreate | PartUpdate):
    if payload.employee_priorities is not None:
        return payload.employee_priorities
    if {
        "level_1_employee_id",
        "level_2_employee_id",
        "level_3_employee_id",
    } & payload.model_fields_set:
        from .schemas import PartSkillPriorityInput

        return [
            PartSkillPriorityInput(employee_id=employee_id, priority_level=level)
            for level, employee_id in (
                (1, payload.level_1_employee_id),
                (2, payload.level_2_employee_id),
                (3, payload.level_3_employee_id),
            )
            if employee_id is not None
        ]
    return None


@app.post("/api/parts", status_code=201)
def create_part(payload: PartCreate):
    try:
        with transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO parts
                    (code, name, standard_hours, is_accessory, is_assembly, active)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.code.strip(),
                    payload.name.strip(),
                    payload.standard_hours,
                    int("accessory" in payload.usage_types),
                    int("assembly" in payload.usage_types),
                    int(payload.active),
                ),
            )
            row = connection.execute(
                "SELECT * FROM parts WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            _save_part_employee_priorities(
                connection,
                int(cursor.lastrowid),
                _part_priorities_from_payload(payload),
            )
            return part_response(row, connection)
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail="零件编号已存在") from error


@app.delete("/api/parts/all/permanent")
def permanently_delete_all_parts():
    """安全永久删除全部零件；存在业务引用时整批不执行。"""
    with transaction() as connection:
        total = int(connection.execute("SELECT COUNT(*) FROM parts").fetchone()[0])
        referenced = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM parts p
                WHERE EXISTS (SELECT 1 FROM part_demands pd WHERE pd.part_id = p.id)
                   OR EXISTS (SELECT 1 FROM assignments a WHERE a.part_id = p.id)
                   OR EXISTS (SELECT 1 FROM machine_bom_items mbi WHERE mbi.part_id = p.id)
                   OR EXISTS (
                        SELECT 1 FROM production_order_items poi WHERE poi.part_id = p.id
                   )
                """
            ).fetchone()[0]
        )
        if referenced:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"有 {referenced} 个零件已用于整机BOM、生产任务或历史排班，"
                    "为保护关联资料，本次未删除任何零件。请先删除相关整机、生产任务"
                    "和历史排班后重试"
                ),
            )
        connection.execute("DELETE FROM employee_skills")
        connection.execute("DELETE FROM parts")
        connection.execute("DELETE FROM sqlite_sequence WHERE name = 'parts'")
        return {"status": "deleted", "deleted": total}


def part_template_path() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "backend" / "assets" / "零件导入模板.xlsx"
    return Path(__file__).resolve().parents[1] / "assets" / "零件导入模板.xlsx"


def accessory_order_template_path() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "backend" / "assets" / "附件订单导入模板.xlsx"
    return Path(__file__).resolve().parents[1] / "assets" / "附件订单导入模板.xlsx"


def machine_bom_template_path() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "backend" / "assets" / "整机BOM矩阵导入模板.xlsx"
    return Path(__file__).resolve().parents[1] / "assets" / "整机BOM矩阵导入模板.xlsx"


def machine_plan_template_path() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "backend" / "assets" / "整机周计划导入模板.xlsx"
    return Path(__file__).resolve().parents[1] / "assets" / "整机周计划导入模板.xlsx"


def template_download_directory() -> Path:
    if "SCHEDULER_DOWNLOAD_DIR" in os.environ:
        return Path(os.environ["SCHEDULER_DOWNLOAD_DIR"])
    return Path.home() / "Downloads"


def available_download_path(directory: Path, filename: str) -> Path:
    target = directory / filename
    if not target.exists():
        return target
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    index = 1
    while True:
        target = directory / f"{stem} ({index}){suffix}"
        if not target.exists():
            return target
        index += 1


@app.get("/api/parts/import/template")
def download_part_template():
    path = part_template_path()
    if not path.is_file():
        raise HTTPException(status_code=500, detail="零件导入模板缺失")
    return FileResponse(
        path,
        filename="零件导入模板.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/api/parts/import/template/save")
def save_part_template():
    source = part_template_path()
    if not source.is_file():
        raise HTTPException(status_code=500, detail="零件导入模板缺失")
    try:
        directory = template_download_directory()
        directory.mkdir(parents=True, exist_ok=True)
        target = available_download_path(directory, "零件导入模板.xlsx")
        shutil.copy2(source, target)
    except OSError as error:
        raise HTTPException(
            status_code=500,
            detail="模板保存失败，请检查下载文件夹的写入权限",
        ) from error
    return {"filename": target.name, "path": str(target)}


@app.post("/api/parts/import/preview")
async def preview_part_import(file: UploadFile = File(...)):
    filename = file.filename or ""
    content = await file.read()
    with connect() as connection:
        return preview_import(connection, filename, content)


@app.post("/api/parts/import/commit")
def commit_part_import(payload: PartImportCommit):
    codes = [row.code.strip() for row in payload.rows]
    if len(codes) != len(set(codes)):
        raise HTTPException(status_code=422, detail="导入数据中存在重复零件编号")
    created = 0
    updated = 0
    employees_created = 0
    skills_updated = 0
    try:
        with transaction() as connection:
            for item in payload.rows:
                code = item.code.strip()
                name = item.name.strip()
                current = connection.execute(
                    "SELECT id, active, is_accessory, is_assembly FROM parts WHERE code = ?", (code,)
                ).fetchone()
                if current is None:
                    part_id = int(connection.execute(
                        """
                        INSERT INTO parts
                            (code, name, standard_hours, is_accessory, is_assembly, active)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            code,
                            name,
                            item.standard_hours,
                            int("accessory" in (item.usage_types or ["accessory"])),
                            int("assembly" in (item.usage_types or [])),
                            int(item.active if item.active is not None else True),
                        ),
                    ).lastrowid)
                    created += 1
                else:
                    connection.execute(
                        """
                        UPDATE parts
                        SET name = ?, standard_hours = ?, is_accessory = ?,
                            is_assembly = ?, active = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (
                            name,
                            item.standard_hours,
                            int(
                                "accessory" in item.usage_types
                                if item.usage_types is not None
                                else bool(current["is_accessory"])
                            ),
                            int(
                                "assembly" in item.usage_types
                                if item.usage_types is not None
                                else bool(current["is_assembly"])
                            ),
                            int(
                                item.active
                                if item.active is not None
                                else bool(current["active"])
                            ),
                            int(current["id"]),
                        ),
                    )
                    part_id = int(current["id"])
                    updated += 1
                # 导入表中的员工列是该零件技能清单的最终结果；留空即无人具备该技能。
                connection.execute(
                    "DELETE FROM employee_skills WHERE part_id = ?", (part_id,)
                )
                connection.execute(
                    "DELETE FROM part_employee_priorities WHERE part_id = ?",
                    (part_id,),
                )
                explicit_priorities = bool(
                    item.employee_level1_names
                    or item.employee_level2_names
                    or item.employee_level3_names
                )
                priority_names = {
                    1: (
                        item.employee_level1_names[0]
                        if item.employee_level1_names
                        else None
                    ),
                    2: (
                        item.employee_level2_names[0]
                        if item.employee_level2_names
                        else None
                    ),
                    3: (
                        item.employee_level3_names[0]
                        if item.employee_level3_names
                        else None
                    ),
                }
                if not explicit_priorities:
                    priority_names[1] = (
                        item.employee_names[0] if item.employee_names else None
                    )
                    priority_names[2] = (
                        item.employee_names[1] if len(item.employee_names) > 1 else None
                    )
                    priority_names[3] = (
                        item.employee_names[2] if len(item.employee_names) > 2 else None
                    )
                all_employee_names = list(
                    dict.fromkeys(
                        [
                            *item.employee_names,
                            *item.employee_level1_names,
                            *item.employee_level2_names,
                            *item.employee_level3_names,
                        ]
                    )
                )
                employee_ids_by_name: dict[str, int] = {}
                for employee_name in all_employee_names:
                    employee = connection.execute(
                        "SELECT id FROM employees WHERE name = ?",
                        (employee_name,),
                    ).fetchone()
                    if employee is None:
                        employee_id = int(
                            connection.execute(
                                """
                                INSERT INTO employees (name, employee_type)
                                VALUES (?, 'core')
                                """,
                                (employee_name,),
                            ).lastrowid
                        )
                        employees_created += 1
                    else:
                        employee_id = int(employee["id"])
                    employee_ids_by_name[employee_name] = employee_id
                    connection.execute(
                        """
                        INSERT INTO employee_skills
                            (employee_id, part_id, priority_level)
                        VALUES (?, ?, ?)
                        """,
                        (employee_id, part_id, 1),
                    )
                    skills_updated += 1
                for priority_level, employee_name in priority_names.items():
                    if employee_name is None:
                        continue
                    connection.execute(
                        """
                        INSERT INTO part_employee_priorities
                            (part_id, priority_level, employee_id)
                        VALUES (?, ?, ?)
                        """,
                        (
                            part_id,
                            priority_level,
                            employee_ids_by_name[employee_name],
                        ),
                    )
            return {
                "created": created,
                "updated": updated,
                "total": created + updated,
                "employees_created": employees_created,
                "skills_updated": skills_updated,
            }
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail="零件编号冲突，导入未执行") from error


@app.put("/api/parts/{part_id}")
def update_part(part_id: int, payload: PartUpdate):
    try:
        with transaction() as connection:
            result = connection.execute(
                """
                UPDATE parts
                SET code = ?, name = ?, standard_hours = ?, is_accessory = ?,
                    is_assembly = ?, active = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    payload.code.strip(),
                    payload.name.strip(),
                    payload.standard_hours,
                    int("accessory" in payload.usage_types),
                    int("assembly" in payload.usage_types),
                    int(payload.active),
                    part_id,
                ),
            )
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="零件不存在")
            row = connection.execute(
                "SELECT * FROM parts WHERE id = ?", (part_id,)
            ).fetchone()
            _save_part_employee_priorities(
                connection,
                part_id,
                _part_priorities_from_payload(payload),
            )
            return part_response(row, connection)
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail="零件编号已存在") from error


@app.delete("/api/parts/{part_id}", status_code=204)
def delete_part(part_id: int):
    with transaction() as connection:
        result = connection.execute(
            "UPDATE parts SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (part_id,),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="零件不存在")


@app.delete("/api/parts/{part_id}/permanent", status_code=204)
def permanently_delete_part(part_id: int):
    with transaction() as connection:
        part = connection.execute(
            "SELECT name FROM parts WHERE id = ?", (part_id,)
        ).fetchone()
        if part is None:
            raise HTTPException(status_code=404, detail="零件不存在")
        used = any(
            connection.execute(
                f"SELECT 1 FROM {table} WHERE part_id = ? LIMIT 1",
                (part_id,),
            ).fetchone()
            for table in (
                "part_demands",
                "assignments",
                "machine_bom_items",
                "production_order_items",
            )
        )
        if used:
            raise HTTPException(
                status_code=409,
                detail="该零件已用于周计划，为保护历史排班只能停用，不能永久删除",
            )
        connection.execute(
            "DELETE FROM employee_skills WHERE part_id = ?", (part_id,)
        )
        connection.execute("DELETE FROM parts WHERE id = ?", (part_id,))


@app.get("/api/machines")
def list_machines():
    with connect() as connection:
        ids = connection.execute(
            "SELECT id FROM machines ORDER BY code, id"
        ).fetchall()
        return [machine_response(connection, int(row["id"])) for row in ids]


@app.get("/api/machines/import/template")
def download_machine_bom_template():
    path = machine_bom_template_path()
    if not path.is_file():
        raise HTTPException(status_code=500, detail="整机BOM矩阵模板缺失")
    return FileResponse(
        path,
        filename="整机BOM矩阵导入模板.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/api/machines/import/template/save")
def save_machine_bom_template():
    source = machine_bom_template_path()
    if not source.is_file():
        raise HTTPException(status_code=500, detail="整机BOM矩阵模板缺失")
    try:
        directory = template_download_directory()
        directory.mkdir(parents=True, exist_ok=True)
        target = available_download_path(directory, source.name)
        shutil.copy2(source, target)
    except OSError as error:
        raise HTTPException(
            status_code=500,
            detail="模板保存失败，请检查下载文件夹的写入权限",
        ) from error
    return {"filename": target.name, "path": str(target)}


@app.post("/api/machines/import/preview")
async def preview_machine_bom_import(file: UploadFile = File(...)):
    filename = file.filename or ""
    content = await file.read()
    with connect() as connection:
        return preview_machine_bom_matrix(connection, filename, content)


@app.post("/api/machines/import/commit")
def commit_machine_bom_import(payload: MachineMatrixImportCommit):
    codes = [item.code.strip() for item in payload.machines]
    if len(codes) != len(set(codes)):
        raise HTTPException(status_code=422, detail="导入数据中存在重复整机编号")
    created = 0
    updated = 0
    try:
        with transaction() as connection:
            for item in payload.machines:
                code = item.code.strip()
                current = connection.execute(
                    "SELECT id, active FROM machines WHERE code = ?",
                    (code,),
                ).fetchone()
                if current is None:
                    machine_id = int(
                        connection.execute(
                            """
                            INSERT INTO machines (code, name, active)
                            VALUES (?, ?, 1)
                            """,
                            (code, item.name.strip()),
                        ).lastrowid
                    )
                    created += 1
                else:
                    machine_id = int(current["id"])
                    connection.execute(
                        """
                        UPDATE machines
                        SET name = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (item.name.strip(), machine_id),
                    )
                    updated += 1
                save_machine_bom(connection, machine_id, item.bom_items)
            return {
                "created": created,
                "updated": updated,
                "total": created + updated,
            }
    except sqlite3.IntegrityError as error:
        raise HTTPException(
            status_code=409,
            detail="整机编号或BOM数据冲突，整批导入未执行",
        ) from error


@app.post("/api/machines", status_code=201)
def create_machine(payload: MachineCreate):
    try:
        with transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO machines (code, name, active) VALUES (?, ?, ?)",
                (payload.code.strip(), payload.name.strip(), int(payload.active)),
            )
            machine_id = int(cursor.lastrowid)
            save_machine_bom(connection, machine_id, payload.bom_items)
            return machine_response(connection, machine_id)
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail="整机编号已存在") from error


@app.put("/api/machines/{machine_id}")
def update_machine(machine_id: int, payload: MachineUpdate):
    try:
        with transaction() as connection:
            result = connection.execute(
                """
                UPDATE machines SET code = ?, name = ?, active = ?,
                    updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (payload.code.strip(), payload.name.strip(), int(payload.active), machine_id),
            )
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="整机不存在")
            save_machine_bom(connection, machine_id, payload.bom_items)
            return machine_response(connection, machine_id)
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail="整机编号已存在") from error


@app.delete("/api/machines/{machine_id}", status_code=204)
def disable_machine(machine_id: int):
    with transaction() as connection:
        result = connection.execute(
            "UPDATE machines SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (machine_id,),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="整机不存在")


@app.delete("/api/machines/{machine_id}/permanent", status_code=204)
def permanently_delete_machine(machine_id: int):
    with transaction() as connection:
        if connection.execute("SELECT 1 FROM production_orders WHERE machine_id = ?", (machine_id,)).fetchone():
            raise HTTPException(status_code=409, detail="整机已用于生产任务，只能停用")
        result = connection.execute("DELETE FROM machines WHERE id = ?", (machine_id,))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="整机不存在")


@app.get("/api/production-orders")
def get_production_orders():
    with connect() as connection:
        return list_orders(connection)


@app.post("/api/production-orders", status_code=201)
def create_production_order(payload: ProductionOrderCreate):
    with transaction() as connection:
        order_id = create_order_snapshot(
            connection,
            payload.order_type,
            payload.source_id,
            payload.quantity,
            payload.start_date.isoformat(),
            payload.end_date.isoformat(),
        )
        return order_response(connection, order_id)


@app.get("/api/production-orders/import/template")
def download_accessory_order_template():
    path = accessory_order_template_path()
    if not path.is_file():
        raise HTTPException(status_code=500, detail="附件订单导入模板缺失")
    return FileResponse(
        path,
        filename="附件订单导入模板.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/api/production-orders/import/template/save")
def save_accessory_order_template():
    source = accessory_order_template_path()
    if not source.is_file():
        raise HTTPException(status_code=500, detail="附件订单导入模板缺失")
    try:
        directory = template_download_directory()
        directory.mkdir(parents=True, exist_ok=True)
        target = available_download_path(directory, "附件订单导入模板.xlsx")
        shutil.copy2(source, target)
    except OSError as error:
        raise HTTPException(
            status_code=500,
            detail="模板保存失败，请检查下载文件夹的写入权限",
        ) from error
    return {"filename": target.name, "path": str(target)}


@app.post("/api/production-orders/import/preview")
async def preview_accessory_orders(file: UploadFile = File(...)):
    filename = file.filename or ""
    content = await file.read()
    with connect() as connection:
        return preview_accessory_order_import(connection, filename, content)


@app.post("/api/production-orders/import/commit")
def commit_accessory_orders(payload: AccessoryOrderImportCommit):
    created_ids: list[int] = []
    with transaction() as connection:
        for item in payload.rows:
            part = connection.execute(
                """
                SELECT id FROM parts
                WHERE code = ? AND active = 1 AND is_accessory = 1
                """,
                (item.part_code.strip(),),
            ).fetchone()
            if part is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"零件“{item.part_code}”不存在、已停用或不具备附件用途",
                )
            created_ids.append(
                create_order_snapshot(
                    connection,
                    "accessory",
                    int(part["id"]),
                    item.quantity,
                    item.start_date.isoformat(),
                    item.end_date.isoformat(),
                    "accessory_import",
                )
            )
        return {
            "created": len(created_ids),
            "order_ids": created_ids,
            "orders": [order_response(connection, order_id) for order_id in created_ids],
        }


@app.get("/api/production-orders/machine-plan-import/template")
def download_machine_plan_template():
    path = machine_plan_template_path()
    if not path.is_file():
        raise HTTPException(status_code=500, detail="整机周计划模板缺失")
    return FileResponse(
        path,
        filename="整机周计划导入模板.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/api/production-orders/machine-plan-import/template/save")
def save_machine_plan_template():
    source = machine_plan_template_path()
    if not source.is_file():
        raise HTTPException(status_code=500, detail="整机周计划模板缺失")
    try:
        directory = template_download_directory()
        directory.mkdir(parents=True, exist_ok=True)
        target = available_download_path(directory, source.name)
        shutil.copy2(source, target)
    except OSError as error:
        raise HTTPException(
            status_code=500,
            detail="模板保存失败，请检查下载文件夹的写入权限",
        ) from error
    return {"filename": target.name, "path": str(target)}


@app.post("/api/production-orders/machine-plan-import/preview")
async def preview_machine_plan_import(
    week_start: date = Form(...),
    file: UploadFile = File(...),
):
    filename = file.filename or ""
    content = await file.read()
    with connect() as connection:
        return preview_machine_plan_matrix(
            connection,
            filename,
            content,
            week_start,
        )


@app.post("/api/production-orders/machine-plan-import/commit")
def commit_machine_plan_import(payload: MachinePlanImportCommit):
    today = generation_today()
    if any(item.target_date < today for item in payload.entries):
        raise HTTPException(
            status_code=422,
            detail="计划中包含早于系统当天的日期，整批导入未执行",
        )
    with transaction() as connection:
        settings = current_settings(connection)
        week_start = payload.week_start.isoformat()
        connection.execute(
            """
            INSERT OR IGNORE INTO week_plans
                (week_start, include_weekend, status, settings_snapshot)
            VALUES (?, 0, 'draft', ?)
            """,
            (week_start, json.dumps(settings, ensure_ascii=False)),
        )
        week = connection.execute(
            "SELECT * FROM week_plans WHERE week_start = ?",
            (week_start,),
        ).fetchone()
        if week["status"] == "confirmed":
            raise HTTPException(
                status_code=409,
                detail="目标周已经确认，请先取消确认后再导入",
            )
        if connection.execute(
            """
            SELECT 1 FROM week_adjustments
            WHERE week_id = ? AND status = 'active'
            """,
            (week["id"],),
        ).fetchone():
            raise HTTPException(
                status_code=409,
                detail="目标周正在进行请假调整，请先完成或取消调整",
            )

        machines = {
            str(row["code"]): row
            for row in connection.execute(
                "SELECT id, code, active FROM machines"
            ).fetchall()
        }
        for item in payload.entries:
            machine = machines.get(item.machine_code.strip())
            if machine is None or not bool(machine["active"]):
                raise HTTPException(
                    status_code=422,
                    detail=f"整机“{item.machine_code}”不存在或已停用",
                )

        old_orders = connection.execute(
            """
            SELECT id FROM production_orders
            WHERE origin = 'machine_plan_import'
              AND import_week_start = ?
            """,
            (week_start,),
        ).fetchall()
        old_order_ids = [int(row["id"]) for row in old_orders]
        if old_order_ids:
            placeholders = ",".join("?" for _ in old_order_ids)
            locked = connection.execute(
                f"""
                SELECT 1
                FROM assignments a
                JOIN production_order_items poi ON poi.id = a.order_item_id
                JOIN week_plans wp ON wp.id = a.week_id
                WHERE poi.order_id IN ({placeholders})
                  AND (
                    wp.status = 'confirmed'
                    OR a.source = 'manual'
                    OR a.work_date < ?
                  )
                LIMIT 1
                """,
                [*old_order_ids, today.isoformat()],
            ).fetchone()
            if locked is not None:
                raise HTTPException(
                    status_code=409,
                    detail="原导入计划包含已确认、人工调整或已过去的排班，不能自动替换",
                )
            item_ids = [
                int(row["id"])
                for row in connection.execute(
                    f"""
                    SELECT id FROM production_order_items
                    WHERE order_id IN ({placeholders})
                    """,
                    old_order_ids,
                ).fetchall()
            ]
            if item_ids:
                item_placeholders = ",".join("?" for _ in item_ids)
                connection.execute(
                    f"DELETE FROM assignments WHERE order_item_id IN ({item_placeholders})",
                    item_ids,
                )
                connection.execute(
                    f"DELETE FROM week_order_demands WHERE order_item_id IN ({item_placeholders})",
                    item_ids,
                )
            connection.execute(
                f"DELETE FROM production_orders WHERE id IN ({placeholders})",
                old_order_ids,
            )

        weekend_used = any(
            item.target_date.weekday() >= 5 for item in payload.entries
        )
        if weekend_used:
            connection.execute(
                """
                UPDATE week_plans
                SET include_weekend = 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (week["id"],),
            )

        created_ids: list[int] = []
        for item in sorted(
            payload.entries,
            key=lambda value: (
                value.target_date,
                value.machine_code,
            ),
        ):
            machine = machines[item.machine_code.strip()]
            created_ids.append(
                create_order_snapshot(
                    connection,
                    "machine",
                    int(machine["id"]),
                    item.quantity,
                    item.target_date.isoformat(),
                    item.target_date.isoformat(),
                    "machine_plan_import",
                    week_start,
                )
            )
        return {
            "created": len(created_ids),
            "replaced": len(old_order_ids),
            "order_ids": created_ids,
            "week_id": int(week["id"]),
            "weekend_enabled": weekend_used,
        }


@app.put("/api/production-orders/{order_id}")
def update_production_order(order_id: int, payload: ProductionOrderUpdate):
    with transaction() as connection:
        update_order_snapshot(
            connection,
            order_id,
            payload.quantity,
            payload.start_date.isoformat(),
            payload.end_date.isoformat(),
        )
        return order_response(connection, order_id)


@app.delete("/api/production-orders/{order_id}", status_code=204)
def cancel_production_order(order_id: int):
    with transaction() as connection:
        order = connection.execute(
            "SELECT status FROM production_orders WHERE id = ?", (order_id,)
        ).fetchone()
        if order is None:
            raise HTTPException(status_code=404, detail="生产任务不存在")
        if order["status"] != "active":
            raise HTTPException(status_code=409, detail="任务已取消或属于历史数据")
        connection.execute(
            "UPDATE production_orders SET status = 'cancelled', needs_generation = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (order_id,),
        )


@app.delete("/api/production-orders/{order_id}/permanent", status_code=204)
def permanently_delete_production_order(order_id: int):
    with transaction() as connection:
        order = connection.execute(
            "SELECT status FROM production_orders WHERE id = ?", (order_id,)
        ).fetchone()
        if order is None:
            raise HTTPException(status_code=404, detail="生产任务不存在")
        if order["status"] == "legacy":
            raise HTTPException(status_code=409, detail="历史迁移任务不能单独删除")
        locked = connection.execute(
            """
            SELECT 1
            FROM assignments a
            JOIN week_plans wp ON wp.id = a.week_id
            JOIN production_order_items poi ON poi.id = a.order_item_id
            WHERE poi.order_id = ?
              AND (wp.status = 'confirmed' OR a.source = 'manual')
            LIMIT 1
            """,
            (order_id,),
        ).fetchone()
        if locked is not None:
            raise HTTPException(
                status_code=409,
                detail="任务包含已确认或人工调整的排班，请先取消确认并移除人工调整",
            )
        connection.execute(
            """
            UPDATE production_orders
            SET status = 'cancelled', needs_generation = 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (order_id,),
        )
        generate_cross_week(connection)
        connection.execute("DELETE FROM production_orders WHERE id = ?", (order_id,))


@app.post("/api/production-orders/generate")
def generate_production_orders():
    with transaction() as connection:
        if connection.execute(
            "SELECT 1 FROM week_adjustments WHERE status = 'active' LIMIT 1"
        ).fetchone():
            raise HTTPException(
                status_code=409,
                detail="存在进行中的请假调整，请先重新确认或取消调整",
            )
        return generate_cross_week(connection)


def _employee_response(connection: sqlite3.Connection, employee_id: int):
    row = connection.execute(
        "SELECT * FROM employees WHERE id = ?", (employee_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="员工不存在")
    skills = connection.execute(
        """
        SELECT es.part_id, pep.priority_level
        FROM employee_skills es
        LEFT JOIN part_employee_priorities pep
          ON pep.part_id = es.part_id AND pep.employee_id = es.employee_id
        WHERE es.employee_id = ?
        ORDER BY es.part_id
        """,
        (employee_id,),
    ).fetchall()
    item = row_dict(row)
    item["unavailable_weekdays"] = json.loads(row["unavailable_weekdays"])
    return {
        **item,
        "active": bool(row["active"]),
        "skill_part_ids": [int(skill["part_id"]) for skill in skills],
        "skill_priorities": [
            {
                "part_id": int(skill["part_id"]),
                "priority_level": int(skill["priority_level"]),
            }
            for skill in skills
            if skill["priority_level"] is not None
        ],
    }


@app.get("/api/employees")
def list_employees():
    with connect() as connection:
        ids = connection.execute(
            """
            SELECT id FROM employees
            ORDER BY CASE employee_type WHEN 'core' THEN 0 ELSE 1 END, name, id
            """
        ).fetchall()
        return [_employee_response(connection, int(row["id"])) for row in ids]


def _save_skills(connection: sqlite3.Connection, employee_id: int, part_ids: list[int]):
    unique_ids = sorted(set(part_ids))
    if unique_ids:
        placeholders = ",".join("?" for _ in unique_ids)
        count = connection.execute(
            f"SELECT COUNT(*) FROM parts WHERE id IN ({placeholders})", unique_ids
        ).fetchone()[0]
        if count != len(unique_ids):
            raise HTTPException(status_code=422, detail="技能中包含不存在的零件")
    if unique_ids:
        placeholders = ",".join("?" for _ in unique_ids)
        connection.execute(
            f"""
            DELETE FROM part_employee_priorities
            WHERE employee_id = ? AND part_id NOT IN ({placeholders})
            """,
            [employee_id, *unique_ids],
        )
    else:
        connection.execute(
            "DELETE FROM part_employee_priorities WHERE employee_id = ?",
            (employee_id,),
        )
    connection.execute(
        "DELETE FROM employee_skills WHERE employee_id = ?", (employee_id,)
    )
    connection.executemany(
        """
        INSERT INTO employee_skills
            (employee_id, part_id, priority_level)
        VALUES (?, ?, ?)
        """,
        [
            (employee_id, part_id, 1)
            for part_id in unique_ids
        ],
    )
    # 兼容从员工管理添加技能的旧操作：若零件尚有空的优先槽位，
    # 按员工加入顺序自动补为员工1、员工2、员工3，管理员之后仍可修改。
    for part_id in unique_ids:
        if connection.execute(
            """
            SELECT 1 FROM part_employee_priorities
            WHERE part_id = ? AND employee_id = ?
            """,
            (part_id, employee_id),
        ).fetchone():
            continue
        used_levels = {
            int(row["priority_level"])
            for row in connection.execute(
                """
                SELECT priority_level FROM part_employee_priorities
                WHERE part_id = ?
                """,
                (part_id,),
            ).fetchall()
        }
        available_level = next(
            (level for level in (1, 2, 3) if level not in used_levels),
            None,
        )
        if available_level is not None:
            connection.execute(
                """
                INSERT INTO part_employee_priorities
                    (part_id, priority_level, employee_id)
                VALUES (?, ?, ?)
                """,
                (part_id, available_level, employee_id),
            )


@app.post("/api/employees", status_code=201)
def create_employee(payload: EmployeeCreate):
    try:
        with transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO employees
                    (name, employee_type, overtime_limit, weekly_work_days,
                     unavailable_weekdays, active)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.name.strip(),
                    payload.employee_type,
                    None,
                    5,
                    json.dumps([5, 6]),
                    int(payload.active),
                ),
            )
            employee_id = int(cursor.lastrowid)
            _save_skills(connection, employee_id, payload.skill_part_ids)
            return _employee_response(connection, employee_id)
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail="员工姓名已存在") from error


@app.delete("/api/employees/all/permanent")
def permanently_delete_all_employees():
    """安全永久删除全部员工；存在排班引用时整批不执行。"""
    with transaction() as connection:
        total = int(connection.execute("SELECT COUNT(*) FROM employees").fetchone()[0])
        referenced = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM employees e
                WHERE EXISTS (SELECT 1 FROM assignments a WHERE a.employee_id = e.id)
                   OR EXISTS (
                        SELECT 1 FROM daily_availability da WHERE da.employee_id = e.id
                   )
                   OR EXISTS (
                        SELECT 1 FROM week_members wm WHERE wm.employee_id = e.id
                   )
                   OR EXISTS (
                        SELECT 1 FROM overtime_approvals oa WHERE oa.employee_id = e.id
                   )
                """
            ).fetchone()[0]
        )
        if referenced:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"有 {referenced} 名员工已参与周计划或历史排班，"
                    "为保护关联资料，本次未删除任何员工。请先删除全部历史排班后重试"
                ),
            )
        connection.execute("DELETE FROM employees")
        connection.execute("DELETE FROM sqlite_sequence WHERE name = 'employees'")
        return {"status": "deleted", "deleted": total}


@app.put("/api/employees/{employee_id}")
def update_employee(employee_id: int, payload: EmployeeUpdate):
    try:
        with transaction() as connection:
            result = connection.execute(
                """
                UPDATE employees
                SET name = ?, employee_type = ?, overtime_limit = ?,
                    weekly_work_days = ?, unavailable_weekdays = ?, active = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    payload.name.strip(),
                    payload.employee_type,
                    None,
                    5,
                    json.dumps([5, 6]),
                    int(payload.active),
                    employee_id,
                ),
            )
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="员工不存在")
            _save_skills(connection, employee_id, payload.skill_part_ids)
            return _employee_response(connection, employee_id)
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail="员工姓名已存在") from error


@app.delete("/api/employees/{employee_id}", status_code=204)
def delete_employee(employee_id: int):
    with transaction() as connection:
        result = connection.execute(
            """
            UPDATE employees
            SET active = 0, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (employee_id,),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="员工不存在")


@app.delete("/api/employees/{employee_id}/permanent", status_code=204)
def permanently_delete_employee(employee_id: int):
    with transaction() as connection:
        employee = connection.execute(
            "SELECT name FROM employees WHERE id = ?", (employee_id,)
        ).fetchone()
        if employee is None:
            raise HTTPException(status_code=404, detail="员工不存在")
        used = any(
            connection.execute(
                f"SELECT 1 FROM {table} WHERE employee_id = ? LIMIT 1",
                (employee_id,),
            ).fetchone()
            for table in (
                "assignments",
                "daily_availability",
                "week_members",
                "overtime_approvals",
            )
        )
        if used:
            raise HTTPException(
                status_code=409,
                detail="该员工已参与周计划，为保护历史排班只能停用，不能永久删除",
            )
        connection.execute("DELETE FROM employees WHERE id = ?", (employee_id,))


@app.get("/api/weeks")
def list_weeks():
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT wp.*,
                   COALESCE(SUM(pd.quantity * pd.standard_hours_snapshot), 0) AS required_hours
            FROM week_plans wp
            LEFT JOIN part_demands pd ON pd.week_id = wp.id
            GROUP BY wp.id
            ORDER BY wp.week_start DESC
            """
        ).fetchall()
        return [
            {
                **row_dict(row),
                "include_weekend": bool(row["include_weekend"]),
                "settings_snapshot": json.loads(row["settings_snapshot"]),
                "required_hours": round(float(row["required_hours"]), 4),
            }
            for row in rows
        ]


def _replace_demands(connection: sqlite3.Connection, week_id: int, demands):
    unique: dict[int, int] = {}
    for demand in demands:
        if demand.part_id in unique:
            raise HTTPException(status_code=422, detail="同一零件不能重复录入")
        unique[demand.part_id] = demand.quantity
    if not unique:
        connection.execute("DELETE FROM part_demands WHERE week_id = ?", (week_id,))
        return
    placeholders = ",".join("?" for _ in unique)
    parts = connection.execute(
        f"SELECT * FROM parts WHERE id IN ({placeholders})", list(unique)
    ).fetchall()
    if len(parts) != len(unique):
        raise HTTPException(status_code=422, detail="需求中包含不存在的零件")
    connection.execute("DELETE FROM part_demands WHERE week_id = ?", (week_id,))
    for part in parts:
        connection.execute(
            """
            INSERT INTO part_demands
                (week_id, part_id, quantity, part_code_snapshot,
                 part_name_snapshot, standard_hours_snapshot)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                week_id,
                part["id"],
                unique[int(part["id"])],
                part["code"],
                part["name"],
                part["standard_hours"],
            ),
        )


@app.post("/api/weeks", status_code=201)
def create_week(payload: WeekCreate):
    try:
        with transaction() as connection:
            settings = current_settings(connection)
            cursor = connection.execute(
                """
                INSERT INTO week_plans
                    (week_start, include_weekend, status, settings_snapshot)
                VALUES (?, ?, 'draft', ?)
                """,
                (
                    payload.week_start.isoformat(),
                    1,
                    json.dumps(settings, ensure_ascii=False),
                ),
            )
            week_id = int(cursor.lastrowid)
            _replace_demands(connection, week_id, payload.demands)
            return calculate_week(connection, week_id)
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail="该周计划已存在") from error


@app.get("/api/weeks/{week_id}")
def get_week(week_id: int):
    with connect() as connection:
        return calculate_week(connection, week_id)


@app.put("/api/weeks/{week_id}")
def update_week(week_id: int, payload: WeekUpdate):
    with transaction() as connection:
        week = week_row(connection, week_id)
        ensure_editable(week)
        connection.execute(
            """
            UPDATE week_plans
            SET include_weekend = ?, status = 'draft', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (1, week_id),
        )
        _replace_demands(connection, week_id, payload.demands)
        if connection.execute("SELECT 1 FROM production_orders WHERE status = 'active' LIMIT 1").fetchone():
            connection.execute(
                "DELETE FROM assignments WHERE week_id = ? AND source = 'generated'",
                (week_id,),
            )
        else:
            connection.execute("DELETE FROM assignments WHERE week_id = ?", (week_id,))
        connection.execute(
            "DELETE FROM overtime_approvals WHERE week_id = ? AND is_manual = 0",
            (week_id,),
        )
        return calculate_week(connection, week_id)


@app.put("/api/weeks/{week_id}/availability")
def update_availability(week_id: int, payload: AvailabilityUpdate):
    with transaction() as connection:
        week = week_row(connection, week_id)
        ensure_editable(week)
        valid_days = set(
            active_dates(week["week_start"], bool(week["include_weekend"]))
        )
        for item in [*payload.entries, *payload.overtime_entries]:
            if item.work_date.isoformat() not in valid_days:
                raise HTTPException(status_code=422, detail="可用性日期不属于本周工作日")
            exists = connection.execute(
                "SELECT 1 FROM employees WHERE id = ?", (item.employee_id,)
            ).fetchone()
            if not exists:
                raise HTTPException(status_code=422, detail="员工不存在")
        connection.executemany(
            """
            INSERT INTO daily_availability
                (week_id, employee_id, work_date, hours, is_manual)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT (week_id, employee_id, work_date)
            DO UPDATE SET hours = excluded.hours, is_manual = 1
            """,
            [
                (
                    week_id,
                    item.employee_id,
                    item.work_date.isoformat(),
                    item.hours,
                )
                for item in payload.entries
            ],
        )
        for item in payload.overtime_entries:
            key = (
                week_id,
                item.employee_id,
                item.work_date.isoformat(),
            )
            if item.manual:
                block_hours = float(
                    settings_from_snapshot(week["settings_snapshot"]).get(
                        "overtime_block_hours", 4.0
                    )
                )
                if abs(float(item.hours) - block_hours) > 0.001:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"加班只能选择0小时或完整的{block_hours:g}小时固定班次"
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO overtime_approvals
                        (week_id, employee_id, work_date, hours, is_manual)
                    VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT (week_id, employee_id, work_date)
                    DO UPDATE SET hours = excluded.hours, is_manual = 1
                    """,
                    (*key, block_hours),
                )
            else:
                connection.execute(
                    """
                    DELETE FROM overtime_approvals
                    WHERE week_id = ? AND employee_id = ? AND work_date = ?
                      AND is_manual = 1
                    """,
                    key,
                )
        if connection.execute("SELECT 1 FROM production_orders WHERE status = 'active' LIMIT 1").fetchone():
            connection.execute(
                "DELETE FROM assignments WHERE week_id = ? AND source = 'generated'",
                (week_id,),
            )
        else:
            connection.execute("DELETE FROM assignments WHERE week_id = ?", (week_id,))
        connection.execute(
            "DELETE FROM overtime_approvals WHERE week_id = ? AND is_manual = 0",
            (week_id,),
        )
        refresh_week_status(connection, week_id)
        return calculate_week(connection, week_id)


@app.put("/api/weeks/{week_id}/calendar")
def update_week_calendar(week_id: int, payload: WeekCalendarUpdate):
    with transaction() as connection:
        week = week_row(connection, week_id)
        ensure_editable(week)
        connection.execute(
            """
            UPDATE week_plans SET include_weekend = ?, status = 'draft',
                updated_at = CURRENT_TIMESTAMP WHERE id = ?
            """,
            (int(payload.include_weekend), week_id),
        )
        if connection.execute("SELECT 1 FROM production_orders WHERE status = 'active' LIMIT 1").fetchone():
            generate_cross_week(connection)
        elif connection.execute("SELECT 1 FROM part_demands WHERE week_id = ? LIMIT 1", (week_id,)).fetchone():
            run_generator(connection, week_id)
        return calculate_week(connection, week_id)


@app.post("/api/weeks/{week_id}/generate")
def generate_week(week_id: int):
    with transaction() as connection:
        if connection.execute(
            """
            SELECT 1 FROM week_adjustments
            WHERE week_id = ? AND status = 'active'
            """,
            (week_id,),
        ).fetchone():
            raise HTTPException(
                status_code=409,
                detail="请假调整期间只保留原员工本人补班，不能重新自动分配",
            )
        if connection.execute(
            "SELECT 1 FROM production_orders WHERE status = 'active' LIMIT 1"
        ).fetchone():
            generate_cross_week(connection)
        else:
            run_generator(connection, week_id)
        return calculate_week(connection, week_id)


@app.post("/api/weeks/{week_id}/resolve")
def resolve_shortage(week_id: int, payload: ResolveShortage):
    with transaction() as connection:
        week = week_row(connection, week_id)
        ensure_editable(week)
        if connection.execute(
            """
            SELECT 1 FROM week_adjustments
            WHERE week_id = ? AND status = 'active'
            """,
            (week_id,),
        ).fetchone():
            raise HTTPException(
                status_code=409,
                detail="请假调整任务已锁定给原员工，不能选择其他人员",
            )
        employees = []
        if payload.employee_ids:
            placeholders = ",".join("?" for _ in payload.employee_ids)
            employees = connection.execute(
                f"SELECT * FROM employees WHERE id IN ({placeholders}) AND active = 1",
                payload.employee_ids,
            ).fetchall()
            if len(employees) != len(set(payload.employee_ids)):
                raise HTTPException(status_code=422, detail="所选人员不存在或已停用")
        if payload.mode == "alternate":
            connection.execute(
                """
                UPDATE week_plans
                SET allow_machine_alternates = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (week_id,),
            )
            if connection.execute(
                "SELECT 1 FROM production_orders WHERE status = 'active' LIMIT 1"
            ).fetchone():
                generate_cross_week(connection)
            else:
                run_generator(connection, week_id)
        elif payload.mode == "advance":
            connection.execute(
                """
                UPDATE week_plans
                SET allow_machine_advance = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (week_id,),
            )
            if connection.execute(
                "SELECT 1 FROM production_orders WHERE status = 'active' LIMIT 1"
            ).fetchone():
                generate_cross_week(connection)
            else:
                run_generator(connection, week_id)
        elif payload.mode == "reinforcement":
            if any(row["employee_type"] != "backup" for row in employees):
                raise HTTPException(status_code=422, detail="增援只能选择候补人员")
            connection.executemany(
                """
                INSERT OR IGNORE INTO week_members
                    (week_id, employee_id, source)
                VALUES (?, ?, 'reinforcement')
                """,
                [(week_id, employee_id) for employee_id in payload.employee_ids],
            )
            connection.execute(
                """
                UPDATE week_plans
                SET allow_machine_alternates = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (week_id,),
            )
            if connection.execute("SELECT 1 FROM production_orders WHERE status = 'active' LIMIT 1").fetchone():
                generate_cross_week(connection)
            else:
                run_generator(connection, week_id)
        else:
            selected_ids = {
                int(row["id"])
                for row in connection.execute(
                    """
                    SELECT e.id
                    FROM employees e
                    LEFT JOIN week_members wm
                      ON wm.employee_id = e.id AND wm.week_id = ?
                    WHERE e.active = 1
                      AND (e.employee_type = 'core' OR wm.week_id IS NOT NULL)
                    """,
                    (week_id,),
                ).fetchall()
            }
            if not set(payload.employee_ids).issubset(selected_ids):
                raise HTTPException(status_code=422, detail="加班人员尚未加入本周排班")
            if connection.execute("SELECT 1 FROM production_orders WHERE status = 'active' LIMIT 1").fetchone():
                generate_cross_week(connection, {week_id: payload.employee_ids})
            else:
                run_generator(connection, week_id, payload.employee_ids)
        return calculate_week(connection, week_id)


@app.put("/api/weeks/{week_id}/assignments")
def update_assignments(week_id: int, payload: AssignmentsUpdate):
    with transaction() as connection:
        if connection.execute(
            """
            SELECT 1 FROM week_adjustments
            WHERE week_id = ? AND status = 'active'
            """,
            (week_id,),
        ).fetchone():
            raise HTTPException(
                status_code=409,
                detail="请假调整期间不能手工修改任务，请先完成或取消本次调整",
            )
        replace_assignments(
            connection,
            week_id,
            [
                {
                    "employee_id": item.employee_id,
                    "part_id": item.part_id,
                    "order_item_id": item.order_item_id,
                    "work_date": item.work_date.isoformat(),
                    "target_date": (
                        item.target_date.isoformat()
                        if item.target_date is not None
                        else item.work_date.isoformat()
                    ),
                    "quantity": item.quantity,
                }
                for item in payload.assignments
            ],
        )
        return calculate_week(connection, week_id)


@app.post("/api/weeks/{week_id}/approve-overtime")
def approve_overtime(week_id: int):
    with transaction() as connection:
        approve_required_overtime(connection, week_id)
        return calculate_week(connection, week_id)


@app.post("/api/weeks/{week_id}/confirm")
def confirm(week_id: int):
    with transaction() as connection:
        confirm_week(connection, week_id)
        return calculate_week(connection, week_id)


@app.post("/api/weeks/{week_id}/unconfirm")
def unconfirm(week_id: int):
    with transaction() as connection:
        unconfirm_week(connection, week_id)
        return calculate_week(connection, week_id)


def _week_adjustment_snapshot(
    connection: sqlite3.Connection, week_id: int
) -> dict[str, object]:
    week = week_row(connection, week_id)
    return {
        "week": {
            "status": week["status"],
            "confirmed_at": week["confirmed_at"],
            "include_weekend": bool(week["include_weekend"]),
        },
        "assignments": [
            row_dict(row)
            for row in connection.execute(
                "SELECT * FROM assignments WHERE week_id = ? ORDER BY id",
                (week_id,),
            ).fetchall()
        ],
        "availability": [
            row_dict(row)
            for row in connection.execute(
                "SELECT * FROM daily_availability WHERE week_id = ?",
                (week_id,),
            ).fetchall()
        ],
        "overtime": [
            row_dict(row)
            for row in connection.execute(
                "SELECT * FROM overtime_approvals WHERE week_id = ?",
                (week_id,),
            ).fetchall()
        ],
        "members": [
            row_dict(row)
            for row in connection.execute(
                "SELECT * FROM week_members WHERE week_id = ?",
                (week_id,),
            ).fetchall()
        ],
    }


@app.post("/api/weeks/{week_id}/leave-adjustments")
def create_leave_adjustment(week_id: int, payload: LeaveAdjustmentCreate):
    with transaction() as connection:
        week = week_row(connection, week_id)
        if week["status"] != "confirmed":
            raise HTTPException(status_code=409, detail="只有已确认周可以创建请假调整")
        if connection.execute(
            """
            SELECT 1 FROM week_adjustments
            WHERE week_id = ? AND status = 'active'
            """,
            (week_id,),
        ).fetchone():
            raise HTTPException(status_code=409, detail="本周已有进行中的请假调整")
        valid_days = set(active_dates(week["week_start"], True))
        selected_ids = {
            int(employee["id"])
            for employee in selected_employees(connection, week_id)
        }
        if payload.employee_id is not None:
            leave_employee_id = payload.employee_id
            leave_days = {item.isoformat() for item in payload.leave_dates}
        else:
            legacy_employee_ids = {item.employee_id for item in payload.entries}
            if len(legacy_employee_ids) != 1:
                raise HTTPException(status_code=422, detail="一次请假调整只能选择一名员工")
            leave_employee_id = legacy_employee_ids.pop()
            leave_days = {item.work_date.isoformat() for item in payload.entries}
        if leave_employee_id not in selected_ids:
            raise HTTPException(status_code=422, detail="请假员工不在本周排班中")
        if not leave_days or not leave_days.issubset(valid_days):
            raise HTTPException(status_code=422, detail="请假日期不属于本周")

        released_rows = connection.execute(
            f"""
            SELECT a.*, po.order_type
            FROM assignments a
            LEFT JOIN production_order_items poi ON poi.id = a.order_item_id
            LEFT JOIN production_orders po ON po.id = poi.order_id
            WHERE a.week_id = ? AND a.employee_id = ?
              AND a.work_date IN ({','.join('?' for _ in leave_days)})
            ORDER BY a.work_date, a.id
            """,
            (week_id, leave_employee_id, *sorted(leave_days)),
        ).fetchall()
        if not released_rows:
            raise HTTPException(status_code=422, detail="所选日期没有该员工的排班任务")

        snapshot = _week_adjustment_snapshot(connection, week_id)
        snapshot["adjustment_policy"] = {
            "employee_id": leave_employee_id,
            "leave_dates": sorted(leave_days),
            "use_overtime": payload.use_overtime,
            "use_weekend": payload.use_weekend,
            "released_quantity": sum(int(row["quantity"]) for row in released_rows),
        }
        cursor = connection.execute(
            """
            INSERT INTO week_adjustments (week_id, snapshot_json)
            VALUES (?, ?)
            """,
            (week_id, json.dumps(snapshot, ensure_ascii=False)),
        )
        # 锁定原已确认排班，只有请假员工对应日期的任务被释放。
        connection.execute(
            "UPDATE assignments SET source = 'manual' WHERE week_id = ?",
            (week_id,),
        )
        for day in sorted(leave_days):
            connection.execute(
                """
                DELETE FROM assignments
                WHERE week_id = ? AND employee_id = ? AND work_date = ?
                """,
                (week_id, leave_employee_id, day),
            )
            connection.execute(
                """
                INSERT INTO daily_availability
                    (week_id, employee_id, work_date, hours, is_manual)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT (week_id, employee_id, work_date)
                DO UPDATE SET hours = excluded.hours, is_manual = 1
                """,
                (week_id, leave_employee_id, day, 0),
            )
            connection.execute(
                """
                DELETE FROM overtime_approvals
                WHERE week_id = ? AND employee_id = ? AND work_date = ?
                """,
                (week_id, leave_employee_id, day),
            )

        settings = settings_from_snapshot(week["settings_snapshot"])
        all_days = active_dates(week["week_start"], True)
        weekend_days = {
            day for day in all_days if date.fromisoformat(day).weekday() >= 5
        }
        if payload.use_weekend:
            connection.execute(
                "UPDATE week_plans SET include_weekend = 1 WHERE id = ?",
                (week_id,),
            )

        refreshed_week = week_row(connection, week_id)
        employees = selected_employees(connection, week_id)
        availability = availability_map(
            connection, refreshed_week, employees, settings
        )
        efficiency = float(settings["efficiency"])
        block_hours = float(settings.get("overtime_block_hours", 4.0))
        existing_load = {
            row["work_date"]: int(
                round(float(row["hours"] or 0) * 60)
            )
            for row in connection.execute(
                """
                SELECT work_date,
                       SUM(quantity * standard_hours_snapshot) AS hours
                FROM assignments
                WHERE week_id = ? AND employee_id = ?
                GROUP BY work_date
                """,
                (week_id, leave_employee_id),
            ).fetchall()
        }
        approved = {
            row["work_date"]: float(row["hours"])
            for row in connection.execute(
                """
                SELECT work_date, hours FROM overtime_approvals
                WHERE week_id = ? AND employee_id = ?
                """,
                (week_id, leave_employee_id),
            ).fetchall()
        }
        residual = {
            day: max(
                0,
                int(
                    round(
                        (
                            availability[(leave_employee_id, day)]
                            + approved.get(day, 0.0)
                        )
                        * efficiency
                        * 60
                    )
                )
                - existing_load.get(day, 0),
            )
            for day in all_days
            if day not in leave_days
            and (
                date.fromisoformat(day).weekday() < 5
                or payload.use_weekend
            )
        }
        unopened_weekend_days = [
            day
            for day in sorted(weekend_days - leave_days, reverse=True)
            if availability[(leave_employee_id, day)] <= 0
        ] if payload.use_weekend else []
        overtime_days = [
            day
            for day in sorted(all_days, reverse=True)
            if day not in leave_days
            and availability[(leave_employee_id, day)] > 0
            and day not in approved
            and (
                date.fromisoformat(day).weekday() < 5
                or payload.use_weekend
            )
        ]
        employee_by_id = {int(employee["id"]): employee for employee in employees}
        all_skills = employee_skills(connection, list(employee_by_id))
        all_priorities = employee_skill_priorities(
            connection, list(employee_by_id)
        )
        approved_by_employee = {
            (int(row["employee_id"]), row["work_date"]): float(row["hours"])
            for row in connection.execute(
                """
                SELECT employee_id, work_date, hours
                FROM overtime_approvals WHERE week_id = ?
                """,
                (week_id,),
            ).fetchall()
        }
        load_by_employee = {
            (int(row["employee_id"]), row["work_date"]): int(
                round(float(row["hours"] or 0) * 60)
            )
            for row in connection.execute(
                """
                SELECT employee_id, work_date,
                       SUM(quantity * standard_hours_snapshot) AS hours
                FROM assignments
                WHERE week_id = ?
                GROUP BY employee_id, work_date
                """,
                (week_id,),
            ).fetchall()
        }
        employee_residual = {
            (employee_id, day): max(
                0,
                int(
                    round(
                        (
                            availability[(employee_id, day)]
                            + approved_by_employee.get(
                                (employee_id, day), 0.0
                            )
                        )
                        * efficiency
                        * 60
                    )
                )
                - load_by_employee.get((employee_id, day), 0),
            )
            for employee_id in employee_by_id
            for day in all_days
        }

        recovered_machine: dict[
            tuple[int, int | None, float, str, str, int], int
        ] = defaultdict(int)
        today_text = generation_today().isoformat()
        for row in released_rows:
            if row["order_type"] != "machine":
                continue
            part_id = int(row["part_id"])
            order_item_id = (
                int(row["order_item_id"])
                if row["order_item_id"] is not None
                else None
            )
            standard_hours = float(row["standard_hours_snapshot"])
            unit_minutes = max(1, int(round(standard_hours * 60)))
            original_work_date = row["work_date"]
            target_date = row["target_date"] or original_work_date
            candidate_days = [
                day
                for day in sorted(all_days, reverse=True)
                if today_text <= day <= original_work_date
                and (
                    date.fromisoformat(day).weekday() < 5
                    or bool(refreshed_week["include_weekend"])
                )
            ]
            for _ in range(int(row["quantity"])):
                allocated = False
                for work_day in candidate_days:
                    for priority_level in (1, 2, 3):
                        candidate_ids = sorted(
                            employee_id
                            for employee_id in employee_by_id
                            if (
                                employee_id != leave_employee_id
                                or work_day not in leave_days
                            )
                            and part_id in all_skills.get(employee_id, set())
                            and all_priorities.get((employee_id, part_id))
                            == priority_level
                            and employee_residual.get(
                                (employee_id, work_day), 0
                            )
                            >= unit_minutes
                        )
                        if not candidate_ids:
                            continue
                        target_employee_id = candidate_ids[0]
                        employee_residual[
                            (target_employee_id, work_day)
                        ] -= unit_minutes
                        if target_employee_id == leave_employee_id:
                            residual[work_day] = max(
                                0,
                                residual.get(work_day, 0) - unit_minutes,
                            )
                        recovered_machine[
                            (
                                part_id,
                                order_item_id,
                                standard_hours,
                                work_day,
                                target_date,
                                target_employee_id,
                            )
                        ] += 1
                        allocated = True
                        break
                    if allocated:
                        break

        connection.executemany(
            """
            INSERT INTO assignments
                (week_id, employee_id, part_id, work_date, target_date,
                 quantity, standard_hours_snapshot, order_item_id, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual')
            ON CONFLICT (
                week_id, employee_id, order_item_id, work_date, target_date
            )
            DO UPDATE SET
                quantity = assignments.quantity + excluded.quantity,
                source = 'manual'
            """,
            [
                (
                    week_id,
                    employee_id,
                    part_id,
                    work_date,
                    target_date,
                    quantity,
                    standard_hours,
                    order_item_id,
                )
                for (
                    part_id,
                    order_item_id,
                    standard_hours,
                    work_date,
                    target_date,
                    employee_id,
                ), quantity in recovered_machine.items()
                if quantity > 0
            ],
        )

        grouped_released: dict[
            tuple[int, int | None, float, str], int
        ] = defaultdict(int)
        for row in released_rows:
            if row["order_type"] == "machine":
                continue
            grouped_released[
                (
                    int(row["part_id"]),
                    int(row["order_item_id"]) if row["order_item_id"] is not None else None,
                    float(row["standard_hours_snapshot"]),
                    row["target_date"] or row["work_date"],
                )
            ] += int(row["quantity"])

        recovered: dict[
            tuple[int, int | None, float, str, str], int
        ] = defaultdict(int)
        for (
            part_id,
            order_item_id,
            standard_hours,
            original_target_date,
        ), quantity in sorted(
            grouped_released.items(),
            key=lambda item: (-item[0][2], item[0][0], item[0][1] or 0),
        ):
            unit_minutes = max(1, int(round(standard_hours * 60)))
            for _ in range(quantity):
                candidates = [
                    day
                    for day, capacity in residual.items()
                    if capacity >= unit_minutes
                ]
                while not candidates and unopened_weekend_days:
                    weekend_day = unopened_weekend_days.pop(0)
                    connection.execute(
                        """
                        INSERT INTO daily_availability
                            (week_id, employee_id, work_date, hours, is_manual)
                        VALUES (?, ?, ?, ?, 1)
                        ON CONFLICT (week_id, employee_id, work_date)
                        DO UPDATE SET hours = excluded.hours, is_manual = 1
                        """,
                        (
                            week_id,
                            leave_employee_id,
                            weekend_day,
                            settings["daily_hours"],
                        ),
                    )
                    availability[(leave_employee_id, weekend_day)] = float(
                        settings["daily_hours"]
                    )
                    residual[weekend_day] = max(
                        0,
                        int(
                            round(
                                float(settings["daily_hours"])
                                * efficiency
                                * 60
                            )
                        )
                        - existing_load.get(weekend_day, 0),
                    )
                    if weekend_day not in approved:
                        overtime_days.append(weekend_day)
                        overtime_days.sort(reverse=True)
                    candidates = [
                        day
                        for day, capacity in residual.items()
                        if capacity >= unit_minutes
                    ]
                if not candidates and payload.use_overtime:
                    overtime_capacity = int(
                        round(block_hours * efficiency * 60)
                    )
                    viable_overtime_days = [
                        day
                        for day in overtime_days
                        if residual.get(day, 0) + overtime_capacity >= unit_minutes
                    ]
                    if viable_overtime_days:
                        overtime_day = viable_overtime_days[0]
                        overtime_days.remove(overtime_day)
                    else:
                        overtime_day = None
                else:
                    overtime_day = None
                if overtime_day is not None:
                    connection.execute(
                        """
                        INSERT INTO overtime_approvals
                            (week_id, employee_id, work_date, hours, is_manual)
                        VALUES (?, ?, ?, ?, 1)
                        ON CONFLICT (week_id, employee_id, work_date)
                        DO UPDATE SET hours = excluded.hours, is_manual = 1
                        """,
                        (week_id, leave_employee_id, overtime_day, block_hours),
                    )
                    residual[overtime_day] = (
                        residual.get(overtime_day, 0)
                        + int(round(block_hours * efficiency * 60))
                    )
                    candidates = [
                        day
                        for day, capacity in residual.items()
                        if capacity >= unit_minutes
                    ]
                if not candidates:
                    continue
                target_day = min(
                    candidates,
                    key=lambda day: (
                        date.fromisoformat(day).weekday() >= 5,
                        -residual[day],
                        day,
                    ),
                )
                residual[target_day] -= unit_minutes
                recovered[
                    (
                        part_id,
                        order_item_id,
                        standard_hours,
                        target_day,
                        original_target_date,
                    )
                ] += 1

        connection.executemany(
            """
            INSERT INTO assignments
                (week_id, employee_id, part_id, work_date, target_date,
                 quantity, standard_hours_snapshot, order_item_id, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual')
            ON CONFLICT (
                week_id, employee_id, order_item_id, work_date, target_date
            )
            DO UPDATE SET
                quantity = assignments.quantity + excluded.quantity,
                source = 'manual'
            """,
            [
                (
                    week_id,
                    leave_employee_id,
                    part_id,
                    work_date,
                    target_date,
                    quantity,
                    standard_hours,
                    order_item_id,
                )
                for (
                    part_id,
                    order_item_id,
                    standard_hours,
                    work_date,
                    target_date,
                ), quantity in recovered.items()
                if quantity > 0
            ],
        )
        connection.execute(
            """
            UPDATE week_plans
            SET status = 'ready', confirmed_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (week_id,),
        )
        refresh_week_status(connection, week_id)
        detail = calculate_week(connection, week_id)
        detail["adjustment_id"] = int(cursor.lastrowid)
        return detail


@app.post("/api/weeks/{week_id}/leave-adjustments/cancel")
def cancel_leave_adjustment(week_id: int):
    with transaction() as connection:
        adjustment = connection.execute(
            """
            SELECT * FROM week_adjustments
            WHERE week_id = ? AND status = 'active'
            """,
            (week_id,),
        ).fetchone()
        if adjustment is None:
            raise HTTPException(status_code=404, detail="没有进行中的请假调整")
        snapshot = json.loads(adjustment["snapshot_json"])
        connection.execute("DELETE FROM assignments WHERE week_id = ?", (week_id,))
        connection.execute(
            "DELETE FROM daily_availability WHERE week_id = ?", (week_id,)
        )
        connection.execute(
            "DELETE FROM overtime_approvals WHERE week_id = ?", (week_id,)
        )
        connection.execute("DELETE FROM week_members WHERE week_id = ?", (week_id,))
        assignments = snapshot.get("assignments", [])
        connection.executemany(
            """
            INSERT INTO assignments
                (id, week_id, employee_id, part_id, work_date, target_date, quantity,
                 standard_hours_snapshot, order_item_id, source)
            VALUES (:id, :week_id, :employee_id, :part_id, :work_date, :target_date, :quantity,
                    :standard_hours_snapshot, :order_item_id, :source)
            """,
            assignments,
        )
        connection.executemany(
            """
            INSERT INTO daily_availability
                (week_id, employee_id, work_date, hours, is_manual)
            VALUES (:week_id, :employee_id, :work_date, :hours, :is_manual)
            """,
            snapshot.get("availability", []),
        )
        connection.executemany(
            """
            INSERT INTO overtime_approvals
                (week_id, employee_id, work_date, hours, is_manual)
            VALUES (:week_id, :employee_id, :work_date, :hours, :is_manual)
            """,
            snapshot.get("overtime", []),
        )
        connection.executemany(
            """
            INSERT INTO week_members (week_id, employee_id, source)
            VALUES (:week_id, :employee_id, :source)
            """,
            snapshot.get("members", []),
        )
        original_week = snapshot["week"]
        connection.execute(
            """
            UPDATE week_plans
            SET status = ?, confirmed_at = ?, include_weekend = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                original_week["status"],
                original_week["confirmed_at"],
                int(original_week.get("include_weekend", False)),
                week_id,
            ),
        )
        connection.execute(
            """
            UPDATE week_adjustments
            SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (adjustment["id"],),
        )
        return calculate_week(connection, week_id)


@app.post("/api/weeks/{week_id}/reset")
def reset_schedule(week_id: int):
    with transaction() as connection:
        reset_week_schedule(connection, week_id)
        return calculate_week(connection, week_id)


@app.post("/api/weeks/{week_id}/export")
def export_schedule(week_id: int, payload: ScheduleExport):
    with connect() as connection:
        detail = calculate_week(connection, week_id)
    if detail["status"] != "confirmed":
        raise HTTPException(status_code=409, detail="周排班确认后才可以导出")
    extension = payload.format
    filename = f"周排班明细_{detail['week_start']}.{extension}"
    try:
        directory = template_download_directory()
        directory.mkdir(parents=True, exist_ok=True)
        target = available_download_path(directory, filename)
        page_count = render_schedule(detail, target, payload.format)
    except (OSError, RuntimeError) as error:
        raise HTTPException(
            status_code=500,
            detail=f"排班导出失败：{error}",
        ) from error
    return {
        "filename": target.name,
        "path": str(target),
        "format": payload.format,
        "page_count": page_count,
    }


def frontend_dist_path() -> Path:
    if "SCHEDULER_FRONTEND_DIR" in os.environ:
        return Path(os.environ["SCHEDULER_FRONTEND_DIR"])
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "frontend" / "dist"
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


FRONTEND_DIST = frontend_dist_path()
if FRONTEND_DIST.exists():
    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIST / "assets"),
        name="frontend-assets",
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    def frontend(full_path: str):
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
