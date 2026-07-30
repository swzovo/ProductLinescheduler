from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from fastapi import HTTPException

from .clock import generation_today
from .scheduler import active_dates
from .service import (
    _employee_limit,
    availability_map,
    current_settings,
    employee_skill_priorities,
    employee_skills,
    ensure_week_availability_snapshot,
    refresh_week_status,
    selected_employees,
    settings_from_snapshot,
)


def monday_for(value: date) -> date:
    return value - timedelta(days=value.weekday())


def machine_response(connection: sqlite3.Connection, machine_id: int) -> dict[str, Any]:
    machine = connection.execute(
        "SELECT * FROM machines WHERE id = ?", (machine_id,)
    ).fetchone()
    if machine is None:
        raise HTTPException(status_code=404, detail="整机不存在")
    items = connection.execute(
        """
        SELECT mbi.part_id, mbi.quantity_per_machine, p.code AS part_code,
               p.name AS part_name, p.standard_hours, p.active AS part_active,
               p.is_assembly
        FROM machine_bom_items mbi
        JOIN parts p ON p.id = mbi.part_id
        WHERE mbi.machine_id = ?
        ORDER BY p.code, p.id
        """,
        (machine_id,),
    ).fetchall()
    return {
        "id": int(machine["id"]),
        "code": machine["code"],
        "name": machine["name"],
        "active": bool(machine["active"]),
        "bom_items": [
            {
                "part_id": int(item["part_id"]),
                "part_code": item["part_code"],
                "part_name": item["part_name"],
                "standard_hours": float(item["standard_hours"]),
                "quantity_per_machine": int(item["quantity_per_machine"]),
                "part_active": bool(item["part_active"]),
                "part_is_assembly": bool(item["is_assembly"]),
            }
            for item in items
        ],
    }


def save_machine_bom(
    connection: sqlite3.Connection, machine_id: int, items: list[Any]
) -> None:
    part_ids = [int(item.part_id) for item in items]
    placeholders = ",".join("?" for _ in part_ids)
    rows = connection.execute(
        f"SELECT id FROM parts WHERE id IN ({placeholders}) AND active = 1 AND is_assembly = 1",
        part_ids,
    ).fetchall()
    if len(rows) != len(part_ids):
        raise HTTPException(
            status_code=422,
            detail="BOM只能选择已启用且具备整机装配用途的零件",
        )
    connection.execute("DELETE FROM machine_bom_items WHERE machine_id = ?", (machine_id,))
    connection.executemany(
        """
        INSERT INTO machine_bom_items
            (machine_id, part_id, quantity_per_machine)
        VALUES (?, ?, ?)
        """,
        [(machine_id, item.part_id, item.quantity_per_machine) for item in items],
    )


def create_order_snapshot(
    connection: sqlite3.Connection,
    order_type: str,
    source_id: int,
    quantity: int,
    start_date: str,
    end_date: str,
    origin: str = "manual",
    import_week_start: str | None = None,
) -> int:
    if order_type == "machine":
        source = connection.execute(
            "SELECT * FROM machines WHERE id = ? AND active = 1", (source_id,)
        ).fetchone()
        if source is None:
            raise HTTPException(status_code=422, detail="所选整机不存在或已停用")
        parts = connection.execute(
            """
            SELECT mbi.part_id, mbi.quantity_per_machine, p.code, p.name,
                   p.standard_hours,
                   CASE WHEN p.is_accessory = 1 AND p.is_assembly = 1
                        THEN 1 ELSE 0 END AS is_dual_usage
            FROM machine_bom_items mbi
            JOIN parts p ON p.id = mbi.part_id
            WHERE mbi.machine_id = ?
            ORDER BY p.code, p.id
            """,
            (source_id,),
        ).fetchall()
        if not parts:
            raise HTTPException(status_code=422, detail="整机尚未配置BOM")
        machine_id, accessory_part_id = source_id, None
    else:
        source = connection.execute(
            "SELECT * FROM parts WHERE id = ? AND active = 1 AND is_accessory = 1",
            (source_id,),
        ).fetchone()
        if source is None:
            raise HTTPException(status_code=422, detail="所选零件不存在、已停用或不能作为附件")
        parts = [
            {
                "part_id": source_id,
                "quantity_per_machine": 1,
                "code": source["code"],
                "name": source["name"],
                "standard_hours": source["standard_hours"],
                "is_dual_usage": int(
                    bool(source["is_accessory"]) and bool(source["is_assembly"])
                ),
            }
        ]
        machine_id, accessory_part_id = None, source_id
    cursor = connection.execute(
        """
        INSERT INTO production_orders
            (order_type, machine_id, accessory_part_id, quantity, start_date,
             end_date, status, source_code_snapshot, source_name_snapshot,
             origin, import_week_start)
        VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
        """,
        (
            order_type, machine_id, accessory_part_id, quantity, start_date,
            end_date, source["code"], source["name"], origin,
            import_week_start,
        ),
    )
    order_id = int(cursor.lastrowid)
    connection.executemany(
        """
        INSERT INTO production_order_items
            (order_id, part_id, quantity_per_unit, required_quantity,
             part_code_snapshot, part_name_snapshot, standard_hours_snapshot,
             is_dual_usage_snapshot)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                order_id,
                int(item["part_id"]),
                int(item["quantity_per_machine"]),
                quantity * int(item["quantity_per_machine"]),
                item["code"],
                item["name"],
                float(item["standard_hours"]),
                int(item["is_dual_usage"]),
            )
            for item in parts
        ],
    )
    return order_id


def update_order_snapshot(
    connection: sqlite3.Connection,
    order_id: int,
    quantity: int,
    start_date: str,
    end_date: str,
) -> None:
    order = connection.execute(
        "SELECT * FROM production_orders WHERE id = ?", (order_id,)
    ).fetchone()
    if order is None:
        raise HTTPException(status_code=404, detail="生产任务不存在")
    if order["status"] != "active":
        raise HTTPException(status_code=409, detail="历史或已取消任务不能修改")
    locked_quantities = connection.execute(
        """
        SELECT poi.id, poi.quantity_per_unit, COALESCE(SUM(a.quantity), 0) AS assigned
        FROM assignments a
        JOIN week_plans wp ON wp.id = a.week_id
        JOIN production_order_items poi ON poi.id = a.order_item_id
        WHERE poi.order_id = ? AND (wp.status = 'confirmed' OR a.source = 'manual')
        GROUP BY poi.id, poi.quantity_per_unit
        """,
        (order_id,),
    ).fetchall()
    if any(
        quantity * int(row["quantity_per_unit"]) < int(row["assigned"])
        for row in locked_quantities
    ):
        raise HTTPException(status_code=409, detail="新数量不能小于已确认或人工调整中的分配数量")
    locked_dates = connection.execute(
        """
        SELECT MIN(a.work_date) AS first_date, MAX(a.work_date) AS last_date
        FROM assignments a
        JOIN week_plans wp ON wp.id = a.week_id
        JOIN production_order_items poi ON poi.id = a.order_item_id
        WHERE poi.order_id = ? AND (wp.status = 'confirmed' OR a.source = 'manual')
        """,
        (order_id,),
    ).fetchone()
    if (
        locked_dates["first_date"] is not None
        and (start_date > locked_dates["first_date"] or end_date < locked_dates["last_date"])
    ):
        raise HTTPException(status_code=409, detail="新日期范围不能排除已确认或人工调整的任务")
    connection.execute(
        """
        UPDATE production_orders
        SET quantity = ?, start_date = ?, end_date = ?, needs_generation = 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (quantity, start_date, end_date, order_id),
    )
    connection.execute(
        """
        UPDATE production_order_items
        SET required_quantity = quantity_per_unit * ?
        WHERE order_id = ?
        """,
        (quantity, order_id),
    )


def order_response(connection: sqlite3.Connection, order_id: int) -> dict[str, Any]:
    order = connection.execute(
        "SELECT * FROM production_orders WHERE id = ?", (order_id,)
    ).fetchone()
    if order is None:
        raise HTTPException(status_code=404, detail="生产任务不存在")
    items = connection.execute(
        """
        SELECT poi.*,
               COALESCE((SELECT SUM(a.quantity) FROM assignments a
                         WHERE a.order_item_id = poi.id), 0) AS assigned_quantity
        FROM production_order_items poi
        WHERE poi.order_id = ?
        ORDER BY poi.part_code_snapshot, poi.id
        """,
        (order_id,),
    ).fetchall()
    confirmed = connection.execute(
        """
        SELECT DISTINCT wp.id, wp.week_start
        FROM week_plans wp
        WHERE wp.status = 'confirmed'
          AND date(wp.week_start, '+6 day') >= date(?)
          AND date(wp.week_start) <= date(?)
        ORDER BY wp.week_start
        """,
        (order["start_date"], order["end_date"]),
    ).fetchall()
    required_hours = sum(
        int(item["required_quantity"]) * float(item["standard_hours_snapshot"])
        for item in items
    )
    scheduled_hours = sum(
        int(item["assigned_quantity"]) * float(item["standard_hours_snapshot"])
        for item in items
    )
    remaining_quantity = sum(
        max(0, int(item["required_quantity"]) - int(item["assigned_quantity"]))
        for item in items
    )
    return {
        "id": int(order["id"]),
        "order_type": order["order_type"],
        "source_id": int(order["machine_id"] or order["accessory_part_id"]),
        "source_code": order["source_code_snapshot"],
        "source_name": order["source_name_snapshot"],
        "quantity": int(order["quantity"]),
        "start_date": order["start_date"],
        "end_date": order["end_date"],
        "status": order["status"],
        "origin": order["origin"],
        "import_week_start": order["import_week_start"],
        "needs_generation": bool(order["needs_generation"]),
        "schedule_status": (
            "completed" if remaining_quantity == 0 else
            "unscheduled" if scheduled_hours == 0 else "partial"
        ),
        "required_hours": round(required_hours, 4),
        "scheduled_hours": round(scheduled_hours, 4),
        "remaining_hours": round(max(0, required_hours - scheduled_hours), 4),
        "remaining_quantity": remaining_quantity,
        "confirmed_conflicts": [
            {"week_id": int(row["id"]), "week_start": row["week_start"]}
            for row in confirmed
        ],
        "items": [
            {
                "id": int(item["id"]),
                "part_id": int(item["part_id"]),
                "part_code": item["part_code_snapshot"],
                "part_name": item["part_name_snapshot"],
                "standard_hours": float(item["standard_hours_snapshot"]),
                "quantity_per_unit": int(item["quantity_per_unit"]),
                "required_quantity": int(item["required_quantity"]),
                "assigned_quantity": int(item["assigned_quantity"]),
                "remaining_quantity": max(
                    0, int(item["required_quantity"]) - int(item["assigned_quantity"])
                ),
            }
            for item in items
        ],
    }


def list_orders(connection: sqlite3.Connection, include_legacy: bool = False) -> list[dict[str, Any]]:
    where = "" if include_legacy else "WHERE status != 'legacy'"
    ids = connection.execute(
        f"SELECT id FROM production_orders {where} ORDER BY start_date, end_date, id"
    ).fetchall()
    return [order_response(connection, int(row["id"])) for row in ids]


def _ensure_weeks(
    connection: sqlite3.Connection,
    orders: list[sqlite3.Row],
    today: date,
) -> list[sqlite3.Row]:
    if not orders:
        return []
    first = min(
        min(date.fromisoformat(row["start_date"]), today)
        if row["order_type"] == "machine"
        and date.fromisoformat(row["end_date"]) >= today
        else date.fromisoformat(row["start_date"])
        for row in orders
    )
    last = max(date.fromisoformat(row["end_date"]) for row in orders)
    settings = current_settings(connection)
    cursor = monday_for(first)
    while cursor <= last:
        connection.execute(
            """
            INSERT OR IGNORE INTO week_plans
                (week_start, include_weekend, status, settings_snapshot)
            VALUES (?, 0, 'draft', ?)
            """,
            (cursor.isoformat(), json.dumps(settings, ensure_ascii=False)),
        )
        cursor += timedelta(days=7)
    return connection.execute(
        """
        SELECT * FROM week_plans
        WHERE date(week_start, '+6 day') >= date(?) AND date(week_start) <= date(?)
        ORDER BY week_start
        """,
        (first.isoformat(), last.isoformat()),
    ).fetchall()


def _eligible_slots(
    order: sqlite3.Row,
    weeks: list[sqlite3.Row],
    employees_by_week: dict[int, list[sqlite3.Row]],
    skills: dict[int, set[int]],
    residual: dict[tuple[int, int, str], int],
    part_id: int,
    unit_minutes: int,
    employee_type: str,
    priorities: dict[tuple[int, int], int] | None = None,
    required_priority: int | None = None,
    earliest_day: str | None = None,
) -> dict[str, list[tuple[int, int]]]:
    result: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for week in weeks:
        if week["status"] == "confirmed":
            continue
        week_id = int(week["id"])
        for day in active_dates(week["week_start"], bool(week["include_weekend"])):
            if (
                day < order["start_date"]
                or day > order["end_date"]
                or earliest_day is not None
                and day < earliest_day
            ):
                continue
            for employee in employees_by_week[week_id]:
                if employee["employee_type"] != employee_type:
                    continue
                employee_id = int(employee["id"])
                key = (week_id, employee_id, day)
                if (
                    part_id in skills.get(employee_id, set())
                    and (
                        required_priority is None
                        or priorities is not None
                        and priorities.get((employee_id, part_id)) == required_priority
                    )
                    and residual.get(key, 0) >= unit_minutes
                ):
                    result[day].append((week_id, employee_id))
    return result


def _configured_priority_levels(
    priorities: dict[tuple[int, int], int],
    part_id: int,
) -> tuple[int | None, ...]:
    """返回零件已配置的员工级别；未配置时沿用普通技能均衡规则。"""
    configured = {
        priority_level
        for (employee_id, priority_part_id), priority_level in priorities.items()
        if priority_part_id == part_id
    }
    levels = tuple(
        priority_level
        for priority_level in (1, 2, 3)
        if priority_level in configured
    )
    return levels or (None,)


def _available_work_days(
    weeks: list[sqlite3.Row],
    earliest: str,
    latest: str,
) -> list[str]:
    return sorted(
        day
        for week in weeks
        if week["status"] != "confirmed"
        for day in active_dates(
            week["week_start"], bool(week["include_weekend"])
        )
        if earliest <= day <= latest
    )


def _eligible_pairs_on_day(
    day: str,
    weeks: list[sqlite3.Row],
    employees_by_week: dict[int, list[sqlite3.Row]],
    skills: dict[int, set[int]],
    residual: dict[tuple[int, int, str], int],
    part_id: int,
    unit_minutes: int,
    priorities: dict[tuple[int, int], int],
    priority_level: int | None,
) -> list[tuple[int, int]]:
    for week in weeks:
        if week["status"] == "confirmed":
            continue
        active = active_dates(
            week["week_start"], bool(week["include_weekend"])
        )
        if day not in active:
            continue
        week_id = int(week["id"])
        return [
            (week_id, int(employee["id"]))
            for employee in employees_by_week.get(week_id, [])
            if part_id in skills.get(int(employee["id"]), set())
            and (
                priority_level is None
                or priorities.get((int(employee["id"]), part_id))
                == priority_level
            )
            and residual.get((week_id, int(employee["id"]), day), 0)
            >= unit_minutes
        ]
    return []


def generate_cross_week(
    connection: sqlite3.Connection,
    overtime_by_week: dict[int, list[int]] | None = None,
) -> dict[str, Any]:
    today = generation_today()
    today_text = today.isoformat()
    orders = connection.execute(
        "SELECT * FROM production_orders WHERE status = 'active' ORDER BY end_date, id"
    ).fetchall()
    weeks = _ensure_weeks(connection, orders, today)
    active_item_ids = [
        int(row["id"])
        for row in connection.execute(
            """
            SELECT poi.id FROM production_order_items poi
            JOIN production_orders po ON po.id = poi.order_id
            WHERE po.status = 'active'
            """
        ).fetchall()
    ]
    managed_item_ids = [
        int(row["id"])
        for row in connection.execute(
            """
            SELECT poi.id FROM production_order_items poi
            JOIN production_orders po ON po.id = poi.order_id
            WHERE po.status != 'legacy'
            """
        ).fetchall()
    ]
    cancelled_item_ids = [
        int(row["id"])
        for row in connection.execute(
            """
            SELECT poi.id FROM production_order_items poi
            JOIN production_orders po ON po.id = poi.order_id
            WHERE po.status = 'cancelled'
            """
        ).fetchall()
    ]
    previous_week_ids: set[int] = set()
    if managed_item_ids:
        item_placeholders = ",".join("?" for _ in managed_item_ids)
        previous_week_ids = {
            int(row["week_id"])
            for row in connection.execute(
                f"""
                SELECT DISTINCT wod.week_id
                FROM week_order_demands wod
                JOIN week_plans wp ON wp.id = wod.week_id
                WHERE wod.order_item_id IN ({item_placeholders})
                  AND wp.status != 'confirmed'
                """,
                managed_item_ids,
            ).fetchall()
        }
    horizon_unconfirmed = {int(week["id"]) for week in weeks if week["status"] != "confirmed"}
    unconfirmed_ids = sorted(previous_week_ids | horizon_unconfirmed)
    touched_weeks = (
        connection.execute(
            f"SELECT * FROM week_plans WHERE id IN ({','.join('?' for _ in unconfirmed_ids)}) ORDER BY week_start",
            unconfirmed_ids,
        ).fetchall()
        if unconfirmed_ids else []
    )
    affected_ids = sorted({int(week["id"]) for week in weeks} | set(unconfirmed_ids))
    if unconfirmed_ids:
        placeholders = ",".join("?" for _ in unconfirmed_ids)
        if managed_item_ids:
            item_placeholders = ",".join("?" for _ in managed_item_ids)
            connection.execute(
                f"DELETE FROM week_order_demands WHERE week_id IN ({placeholders}) AND order_item_id IN ({item_placeholders})",
                [*unconfirmed_ids, *managed_item_ids],
            )
            connection.execute(
                f"""
                DELETE FROM assignments
                WHERE week_id IN ({placeholders})
                  AND source = 'generated'
                  AND order_item_id IN ({item_placeholders})
                  AND work_date >= ?
                """,
                [*unconfirmed_ids, *managed_item_ids, today_text],
            )
            if cancelled_item_ids:
                cancelled_placeholders = ",".join("?" for _ in cancelled_item_ids)
                connection.execute(
                    f"DELETE FROM assignments WHERE week_id IN ({placeholders}) AND order_item_id IN ({cancelled_placeholders})",
                    [*unconfirmed_ids, *cancelled_item_ids],
                )

    employees_by_week: dict[int, list[sqlite3.Row]] = {}
    settings_by_week: dict[int, dict[str, float]] = {}
    original: dict[tuple[int, int, str], int] = {}
    residual: dict[tuple[int, int, str], int] = {}
    overtime_residual: dict[tuple[int, int, str], int] = {}
    all_employee_ids: set[int] = set()
    overtime_by_week = overtime_by_week or {}
    for week in weeks:
        week_id = int(week["id"])
        settings = settings_from_snapshot(week["settings_snapshot"])
        settings_by_week[week_id] = settings
        if week["status"] != "confirmed":
            connection.execute(
                "DELETE FROM overtime_approvals WHERE week_id = ? AND is_manual = 0",
                (week_id,),
            )
        employees = selected_employees(connection, week_id)
        employees_by_week[week_id] = employees
        all_employee_ids.update(int(row["id"]) for row in employees)
        ensure_week_availability_snapshot(connection, week, employees, settings)
        available = availability_map(connection, week, employees, settings)
        approved_rows = connection.execute(
            "SELECT * FROM overtime_approvals WHERE week_id = ?", (week_id,)
        ).fetchall()
        approved = {
            (int(row["employee_id"]), row["work_date"]): float(row["hours"])
            for row in approved_rows
            if bool(row["is_manual"])
        }
        manual_approved = {
            (int(row["employee_id"]), row["work_date"])
            for row in approved_rows
            if bool(row["is_manual"])
        }
        selected_overtime = set(overtime_by_week.get(week_id, []))
        for employee in employees:
            employee_id = int(employee["id"])
            for day in active_dates(week["week_start"], bool(week["include_weekend"])):
                extra = approved.get((employee_id, day), 0.0)
                key = (week_id, employee_id, day)
                capacity = int(round((available[(employee_id, day)] + extra) * settings["efficiency"] * 60))
                original[key] = capacity
                residual[key] = capacity
                block_hours = float(settings.get("overtime_block_hours", 4.0))
                if (
                    employee_id in selected_overtime
                    and available[(employee_id, day)] > 0
                    and (employee_id, day) not in manual_approved
                    and _employee_limit(employee, settings) + 0.001 >= block_hours
                ):
                    overtime_residual[key] = int(
                        round(block_hours * settings["efficiency"] * 60)
                    )

    skills = employee_skills(connection, sorted(all_employee_ids))
    priorities = employee_skill_priorities(connection, sorted(all_employee_ids))
    locked = connection.execute(
        """
        SELECT a.*, COALESCE(poi.standard_hours_snapshot, a.standard_hours_snapshot) AS item_hours
        FROM assignments a
        LEFT JOIN production_order_items poi ON poi.id = a.order_item_id
        JOIN week_plans wp ON wp.id = a.week_id
        WHERE wp.status = 'confirmed'
           OR a.source = 'manual'
           OR a.work_date < ?
        """
        ,
        (today_text,),
    ).fetchall()
    locked_by_item: dict[int, int] = defaultdict(int)
    locked_by_target: dict[tuple[int, str], int] = defaultdict(int)
    day_item_quantity: dict[tuple[int, str], int] = defaultdict(int)
    for row in locked:
        if row["order_item_id"] is not None:
            item_id = int(row["order_item_id"])
            locked_by_item[item_id] += int(row["quantity"])
            target_date = row["target_date"] or row["work_date"]
            locked_by_target[(item_id, target_date)] += int(row["quantity"])
            day_item_quantity[(item_id, target_date)] += int(row["quantity"])
        key = (int(row["week_id"]), int(row["employee_id"]), row["work_date"])
        residual[key] = max(
            0,
            residual.get(key, 0)
            - int(round(int(row["quantity"]) * float(row["item_hours"]) * 60)),
        )

    generated: dict[tuple[int, int, int, str, str], int] = defaultdict(int)
    remaining_by_item: dict[int, int] = {}
    remaining_machine_targets: dict[tuple[int, str], int] = {}

    def worker_load_key(
        candidate: tuple[int, int],
        work_date: str,
    ) -> tuple[float, float, int]:
        """附件订单优先交给累计负荷较低者，负荷接近时保持均分。"""
        week_id, employee_id = candidate
        worker_keys = [
            key
            for key in original
            if key[0] == week_id and key[1] == employee_id
        ]
        weekly_capacity = sum(original[key] for key in worker_keys)
        weekly_assigned = sum(
            original[key] - residual.get(key, 0) for key in worker_keys
        )
        day_key = (week_id, employee_id, work_date)
        day_capacity = original.get(day_key, 0)
        day_assigned = day_capacity - residual.get(day_key, 0)
        return (
            weekly_assigned / max(weekly_capacity, 1),
            day_assigned / max(day_capacity, 1),
            employee_id,
        )

    order_by_id = {int(order["id"]): order for order in orders}
    items = connection.execute(
        """
        SELECT poi.*, po.order_type, po.start_date, po.end_date, po.id AS production_order_id
        FROM production_order_items poi
        JOIN production_orders po ON po.id = poi.order_id
        WHERE po.status = 'active'
        ORDER BY CASE po.order_type WHEN 'machine' THEN 0 ELSE 1 END,
                 po.end_date, po.id, poi.standard_hours_snapshot DESC, poi.id
        """
    ).fetchall()
    machine_target_units: dict[int, dict[str, int]] = {}
    for order in orders:
        if order["order_type"] != "machine":
            continue
        target_days = sorted(
            {
                day
                for week in weeks
                for day in active_dates(
                    week["week_start"], bool(week["include_weekend"])
                )
                if order["start_date"] <= day <= order["end_date"]
            }
        )
        if not target_days:
            target_days = [order["end_date"]]
        targets: dict[str, int] = {}
        previous = 0
        for index, target_day in enumerate(target_days):
            cumulative = math.floor(
                (
                    (index + 1)
                    * int(order["quantity"])
                    / max(len(target_days), 1)
                )
                + 0.5
            )
            targets[target_day] = cumulative - previous
            previous = cumulative
        machine_target_units[int(order["id"])] = targets

    def machine_policy(target_day: str) -> tuple[bool, bool]:
        target_date = date.fromisoformat(target_day)
        target_week = next(
            (
                week
                for week in weeks
                if date.fromisoformat(week["week_start"])
                <= target_date
                <= date.fromisoformat(week["week_start"])
                + timedelta(days=6)
            ),
            None,
        )
        if target_week is None:
            return False, False
        return (
            bool(target_week["allow_machine_alternates"]),
            bool(target_week["allow_machine_advance"]),
        )

    for item in items:
        item_id = int(item["id"])
        order = order_by_id[int(item["production_order_id"])]
        unit_minutes = max(1, int(round(float(item["standard_hours_snapshot"]) * 60)))
        if item["order_type"] == "machine":
            order_targets = machine_target_units.get(
                int(item["production_order_id"]), {}
            )
            quantity_per_machine = int(item["quantity_per_unit"])
            item_remaining = 0
            for target_day, machine_quantity in sorted(order_targets.items()):
                required = machine_quantity * quantity_per_machine
                remaining = max(
                    0,
                    required - locked_by_target[(item_id, target_day)],
                )
                allow_alternates, allow_advance = machine_policy(target_day)
                configured_levels = _configured_priority_levels(
                    priorities, int(item["part_id"])
                )
                priority_levels = (
                    configured_levels
                    if allow_alternates
                    else configured_levels[:1]
                )
                candidate_days = (
                    list(
                        reversed(
                            _available_work_days(
                                weeks,
                                today_text,
                                target_day,
                            )
                        )
                    )
                    if allow_advance
                    else [target_day]
                )
                for work_day in candidate_days:
                    for priority_level in priority_levels:
                        while remaining > 0:
                            candidates = _eligible_pairs_on_day(
                                work_day,
                                weeks,
                                employees_by_week,
                                skills,
                                residual,
                                int(item["part_id"]),
                                unit_minutes,
                                priorities,
                                priority_level,
                            )
                            if not candidates:
                                break
                            week_id, employee_id = max(
                                candidates,
                                key=lambda pair: (
                                    residual[
                                        (pair[0], pair[1], work_day)
                                    ],
                                    -pair[1],
                                ),
                            )
                            key = (week_id, employee_id, work_day)
                            residual[key] -= unit_minutes
                            generated[
                                (
                                    week_id,
                                    employee_id,
                                    item_id,
                                    work_day,
                                    target_day,
                                )
                            ] += 1
                            remaining -= 1
                        if remaining == 0:
                            break
                    if remaining == 0:
                        break
                remaining_machine_targets[(item_id, target_day)] = remaining
                item_remaining += remaining
            remaining_by_item[item_id] = item_remaining
        else:
            remaining = max(
                0,
                int(item["required_quantity"]) - locked_by_item[item_id],
            )
            if remaining == 0:
                remaining_by_item[item_id] = 0
                continue
            part_id = int(item["part_id"])
            # 附件订单同样遵守零件员工级别：先用完员工1在整个订单
            # 日期范围内的剩余正常产能，仍无法按期完成时才依次使用员工2、员工3。
            # 未配置员工级别的旧数据继续按技能和负荷均衡分配。
            for priority_level in _configured_priority_levels(
                priorities, part_id
            ):
                for employee_type in ("core", "backup"):
                    slots = _eligible_slots(
                        order, weeks, employees_by_week, skills, residual,
                        part_id, unit_minutes, employee_type,
                        priorities, priority_level,
                        today_text,
                    )
                    for day in sorted(slots):
                        while remaining > 0:
                            candidates = [
                                pair
                                for pair in slots[day]
                                if residual[(pair[0], pair[1], day)] >= unit_minutes
                            ]
                            if not candidates:
                                break
                            week_id, employee_id = min(
                                candidates,
                                key=lambda pair: worker_load_key(pair, day),
                            )
                            key = (week_id, employee_id, day)
                            residual[key] -= unit_minutes
                            generated[
                                (week_id, employee_id, item_id, day, day)
                            ] += 1
                            remaining -= 1
                        if remaining == 0:
                            break
                    if remaining == 0:
                        break
                if remaining == 0:
                    break
            remaining_by_item[item_id] = remaining

    # 自动加班使用独立的四小时班次，不参与正常工时均衡；按任务截止日期
    # 从后往前填充，且继续遵守整机优先和员工1/员工2/员工3规则。
    for item in items:
        item_id = int(item["id"])
        order = order_by_id[int(item["production_order_id"])]
        part_id = int(item["part_id"])
        unit_minutes = max(
            1, int(round(float(item["standard_hours_snapshot"]) * 60))
        )
        if item["order_type"] == "machine":
            for target_day in sorted(
                target
                for (target_item_id, target), quantity
                in remaining_machine_targets.items()
                if target_item_id == item_id and quantity > 0
            ):
                remaining = remaining_machine_targets[(item_id, target_day)]
                _, allow_advance = machine_policy(target_day)
                candidate_days = (
                    list(
                        reversed(
                            _available_work_days(
                                weeks, today_text, target_day
                            )
                        )
                    )
                    if allow_advance
                    else [target_day]
                )
                for work_day in candidate_days:
                    for priority_level in (1, 2, 3):
                        matching_keys = sorted(
                            (
                                key
                                for key, capacity in overtime_residual.items()
                                if key[2] == work_day
                                and capacity >= unit_minutes
                                and part_id in skills.get(key[1], set())
                                and priorities.get((key[1], part_id))
                                == priority_level
                            ),
                            key=lambda key: key[1],
                        )
                        for week_id, employee_id, day in matching_keys:
                            key = (week_id, employee_id, day)
                            while (
                                remaining > 0
                                and overtime_residual.get(key, 0)
                                >= unit_minutes
                            ):
                                overtime_residual[key] -= unit_minutes
                                generated[
                                    (
                                        week_id,
                                        employee_id,
                                        item_id,
                                        day,
                                        target_day,
                                    )
                                ] += 1
                                remaining -= 1
                            if remaining == 0:
                                break
                        if remaining == 0:
                            break
                    if remaining == 0:
                        break
                remaining_machine_targets[(item_id, target_day)] = remaining
            remaining_by_item[item_id] = sum(
                quantity
                for (target_item_id, _), quantity
                in remaining_machine_targets.items()
                if target_item_id == item_id
            )
            continue

        remaining = remaining_by_item.get(item_id, 0)
        if remaining <= 0:
            continue
        priority_levels = _configured_priority_levels(
            priorities, part_id
        )
        for priority_level in priority_levels:
            for employee_type in ("core", "backup"):
                candidate_keys = [
                    key
                    for key, capacity in overtime_residual.items()
                    if capacity >= unit_minutes
                    and key[2] >= max(order["start_date"], today_text)
                    and key[2] <= order["end_date"]
                    and any(
                        int(employee["id"]) == key[1]
                        and employee["employee_type"] == employee_type
                        for employee in employees_by_week.get(key[0], [])
                    )
                    and part_id in skills.get(key[1], set())
                    and (
                        priority_level is None
                        or priorities.get((key[1], part_id)) == priority_level
                    )
                ]
                for week_id, employee_id, day in sorted(
                    candidate_keys,
                    key=lambda key: (key[2], -key[1]),
                    reverse=True,
                ):
                    key = (week_id, employee_id, day)
                    while (
                        remaining > 0
                        and overtime_residual.get(key, 0) >= unit_minutes
                    ):
                        overtime_residual[key] -= unit_minutes
                        generated[
                            (week_id, employee_id, item_id, day, day)
                        ] += 1
                        day_item_quantity[(item_id, day)] += 1
                        remaining -= 1
                    if remaining == 0:
                        break
                if remaining == 0:
                    break
            if remaining == 0:
                break
        remaining_by_item[item_id] = remaining

    item_by_id = {int(item["id"]): item for item in items}
    for (
        week_id,
        employee_id,
        item_id,
        day,
        target_day,
    ), quantity in generated.items():
        item = item_by_id[item_id]
        connection.execute(
            """
            INSERT INTO assignments
                (week_id, employee_id, part_id, work_date, target_date, quantity,
                 standard_hours_snapshot, order_item_id, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'generated')
            """,
            (
                week_id, employee_id, item["part_id"], day, target_day, quantity,
                item["standard_hours_snapshot"], item_id,
            ),
        )

    # 周需求按实际生产周归集；整机未排数量保留在其每日目标所在周。
    quantities = defaultdict(int)
    assigned_rows = connection.execute(
        """
        SELECT a.week_id, a.order_item_id, SUM(a.quantity) AS quantity
        FROM assignments a
        JOIN production_order_items poi ON poi.id = a.order_item_id
        JOIN production_orders po ON po.id = poi.order_id
        WHERE po.status = 'active'
        GROUP BY a.week_id, a.order_item_id
        """
    ).fetchall()
    assigned_total: dict[int, int] = defaultdict(int)
    for row in assigned_rows:
        quantities[(int(row["week_id"]), int(row["order_item_id"]))] += int(row["quantity"])
        assigned_total[int(row["order_item_id"])] += int(row["quantity"])
    for item in items:
        if item["order_type"] == "machine":
            for (target_item_id, target_day), remaining in (
                remaining_machine_targets.items()
            ):
                if target_item_id != int(item["id"]) or remaining <= 0:
                    continue
                target_week = next(
                    (
                        week
                        for week in weeks
                        if week["week_start"] <= target_day
                        and (
                            date.fromisoformat(week["week_start"])
                            + timedelta(days=6)
                        ).isoformat()
                        >= target_day
                    ),
                    None,
                )
                if (
                    target_week is not None
                    and target_week["status"] != "confirmed"
                ):
                    quantities[
                        (int(target_week["id"]), int(item["id"]))
                    ] += remaining
            continue
        remaining = max(0, int(item["required_quantity"]) - assigned_total[int(item["id"])])
        if not remaining:
            continue
        candidates = [
            week for week in weeks
            if week["status"] != "confirmed"
            and week["week_start"] <= item["end_date"]
            and (date.fromisoformat(week["week_start"]) + timedelta(days=6)).isoformat() >= item["start_date"]
        ]
        if candidates:
            quantities[(int(candidates[-1]["id"]), int(item["id"]))] += remaining
    for (week_id, item_id), quantity in quantities.items():
        week = next((row for row in weeks if int(row["id"]) == week_id), None)
        if week is not None and week["status"] != "confirmed" and quantity > 0:
            connection.execute(
                """
                INSERT INTO week_order_demands (week_id, order_item_id, quantity)
                VALUES (?, ?, ?)
                ON CONFLICT (week_id, order_item_id) DO UPDATE SET quantity = excluded.quantity
                """,
                (week_id, item_id, quantity),
            )

    for week in touched_weeks:
        if week["status"] == "confirmed":
            continue
        week_id = int(week["id"])
        connection.execute("DELETE FROM part_demands WHERE week_id = ?", (week_id,))
        aggregates = connection.execute(
            """
            SELECT poi.part_id, SUM(wod.quantity) AS quantity,
                   MIN(poi.part_code_snapshot) AS part_code,
                   MIN(poi.part_name_snapshot) AS part_name,
                   MIN(poi.standard_hours_snapshot) AS standard_hours
            FROM week_order_demands wod
            JOIN production_order_items poi ON poi.id = wod.order_item_id
            WHERE wod.week_id = ?
            GROUP BY poi.part_id
            """,
            (week_id,),
        ).fetchall()
        connection.executemany(
            """
            INSERT INTO part_demands
                (week_id, part_id, quantity, part_code_snapshot,
                 part_name_snapshot, standard_hours_snapshot)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    week_id, row["part_id"], row["quantity"], row["part_code"],
                    row["part_name"], row["standard_hours"],
                )
                for row in aggregates
            ],
        )
        refresh_week_status(connection, week_id)

    for week_id, employee_ids in overtime_by_week.items():
        if week_id not in unconfirmed_ids or not employee_ids:
            continue
        # 审批值按新排班实际超出正常产能的部分计算。
        week = next(row for row in weeks if int(row["id"]) == week_id)
        settings = settings_by_week[week_id]
        employees = {int(row["id"]): row for row in employees_by_week[week_id]}
        available = availability_map(connection, week, list(employees.values()), settings)
        loads = connection.execute(
            """
            SELECT employee_id, work_date, SUM(quantity * standard_hours_snapshot) AS hours
            FROM assignments WHERE week_id = ? GROUP BY employee_id, work_date
            """,
            (week_id,),
        ).fetchall()
        manual_overtime = {
            (int(row["employee_id"]), row["work_date"])
            for row in connection.execute(
                """
                SELECT employee_id, work_date FROM overtime_approvals
                WHERE week_id = ? AND is_manual = 1
                """,
                (week_id,),
            ).fetchall()
        }
        connection.execute(
            "DELETE FROM overtime_approvals WHERE week_id = ? AND is_manual = 0",
            (week_id,),
        )
        for load in loads:
            employee_id = int(load["employee_id"])
            if employee_id not in employee_ids:
                continue
            if (employee_id, load["work_date"]) in manual_overtime:
                continue
            normal = available[(employee_id, load["work_date"])] * settings["efficiency"]
            extra_standard = max(0.0, float(load["hours"]) - normal)
            actual_extra = extra_standard / settings["efficiency"]
            limit = _employee_limit(employees[employee_id], settings)
            block_hours = float(settings.get("overtime_block_hours", 4.0))
            if block_hours > limit + 0.001:
                continue
            if actual_extra > 0:
                connection.execute(
                    """
                    INSERT INTO overtime_approvals
                        (week_id, employee_id, work_date, hours, is_manual)
                    VALUES (?, ?, ?, ?, 0)
                    """,
                    (week_id, employee_id, load["work_date"], block_hours),
                )
        refresh_week_status(connection, week_id)

    connection.execute(
        "UPDATE production_orders SET needs_generation = 0 WHERE status != 'legacy'"
    )
    return {
        "affected_week_ids": affected_ids,
        "orders": list_orders(connection),
    }
