from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from .scheduler import Allocation, Demand, Worker, active_dates, solve_schedule


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def current_settings(connection: sqlite3.Connection) -> dict[str, float]:
    row = connection.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    assert row is not None
    return {
        "daily_hours": float(row["daily_hours"]),
        "efficiency": float(row["efficiency"]),
        # 兼容旧客户端保留 overtime_limit 字段，但其值始终等于固定班次。
        "overtime_limit": float(row["overtime_block_hours"]),
        "overtime_block_hours": float(row["overtime_block_hours"]),
        "shortage_threshold": float(row["shortage_threshold"]),
        "green_threshold": float(row["green_threshold"]),
        "yellow_threshold": float(row["yellow_threshold"]),
        "daily_efficiency_low_threshold": float(
            row["daily_efficiency_low_threshold"]
        ),
        "daily_efficiency_target_threshold": float(
            row["daily_efficiency_target_threshold"]
        ),
    }


def settings_from_snapshot(raw_snapshot: str) -> dict[str, float]:
    """读取周计划设置，并兼容升级前尚未保存自定义缺口阈值的历史快照。"""
    settings = json.loads(raw_snapshot)
    settings.setdefault(
        "shortage_threshold",
        float(settings["daily_hours"]) * float(settings["efficiency"]) * 5 / 2,
    )
    settings.setdefault("daily_efficiency_low_threshold", 0.8)
    settings.setdefault("daily_efficiency_target_threshold", 0.9)
    settings.setdefault("overtime_block_hours", 4.0)
    settings["overtime_limit"] = float(settings["overtime_block_hours"])
    return settings


def week_row(connection: sqlite3.Connection, week_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM week_plans WHERE id = ?", (week_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="周计划不存在")
    return row


def ensure_editable(row: sqlite3.Row) -> None:
    if row["status"] == "confirmed":
        raise HTTPException(status_code=409, detail="已确认的周计划不能修改")


def employee_skills(
    connection: sqlite3.Connection, employee_ids: list[int] | None = None
) -> dict[int, set[int]]:
    params: list[Any] = []
    where = ""
    if employee_ids:
        placeholders = ",".join("?" for _ in employee_ids)
        where = f"WHERE employee_id IN ({placeholders})"
        params.extend(employee_ids)
    rows = connection.execute(
        f"SELECT employee_id, part_id FROM employee_skills {where}", params
    ).fetchall()
    result: dict[int, set[int]] = defaultdict(set)
    for row in rows:
        result[int(row["employee_id"])].add(int(row["part_id"]))
    return result


def employee_skill_priorities(
    connection: sqlite3.Connection,
    employee_ids: list[int] | None = None,
) -> dict[tuple[int, int], int]:
    params: list[Any] = []
    where = ""
    if employee_ids:
        placeholders = ",".join("?" for _ in employee_ids)
        where = f"WHERE employee_id IN ({placeholders})"
        params.extend(employee_ids)
    rows = connection.execute(
        f"""
        SELECT employee_id, part_id, priority_level
        FROM part_employee_priorities {where}
        """,
        params,
    ).fetchall()
    return {
        (int(row["employee_id"]), int(row["part_id"])): int(
            row["priority_level"]
        )
        for row in rows
    }


def selected_employees(
    connection: sqlite3.Connection, week_id: int
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT DISTINCT e.*
        FROM employees e
        LEFT JOIN week_members wm
          ON wm.employee_id = e.id AND wm.week_id = ?
        WHERE (e.active = 1 AND e.employee_type = 'core')
           OR wm.week_id IS NOT NULL
        ORDER BY CASE e.employee_type WHEN 'core' THEN 0 ELSE 1 END, e.id
        """,
        (week_id,),
    ).fetchall()


def default_availability_for_employee(
    employee: sqlite3.Row,
    days: list[str],
    daily_hours: float,
) -> dict[str, float]:
    # 员工基础资料统一为默认周一至周五；具体某周的上班日期完全由
    # daily_availability 管理，不再读取员工长期工作天数/固定休息日。
    return {
        day: daily_hours if datetime.fromisoformat(day).weekday() < 5 else 0.0
        for day in days
    }


def ensure_week_availability_snapshot(
    connection: sqlite3.Connection,
    week: sqlite3.Row,
    employees: list[sqlite3.Row],
    settings: dict[str, float],
) -> None:
    days = active_dates(week["week_start"], bool(week["include_weekend"]))
    entries: list[tuple[int, int, str, float]] = []
    for employee in employees:
        employee_id = int(employee["id"])
        defaults = default_availability_for_employee(
            employee, days, settings["daily_hours"]
        )
        entries.extend(
            (int(week["id"]), employee_id, day, hours)
            for day, hours in defaults.items()
        )
    connection.executemany(
        """
        INSERT OR IGNORE INTO daily_availability
            (week_id, employee_id, work_date, hours)
        VALUES (?, ?, ?, ?)
        """,
        entries,
    )


def availability_map(
    connection: sqlite3.Connection,
    week: sqlite3.Row,
    employees: list[sqlite3.Row],
    settings: dict[str, float],
) -> dict[tuple[int, str], float]:
    days = active_dates(week["week_start"], bool(week["include_weekend"]))
    values: dict[tuple[int, str], float] = {}
    for employee in employees:
        defaults = default_availability_for_employee(
            employee, days, settings["daily_hours"]
        )
        values.update(
            {(int(employee["id"]), day): hours for day, hours in defaults.items()}
        )
    rows = connection.execute(
        "SELECT * FROM daily_availability WHERE week_id = ?", (week["id"],)
    ).fetchall()
    for row in rows:
        key = (int(row["employee_id"]), row["work_date"])
        if key in values:
            values[key] = float(row["hours"])
    return values


def demand_rows(connection: sqlite3.Connection, week_id: int) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT pd.*, p.active AS part_active
        FROM part_demands pd
        JOIN parts p ON p.id = pd.part_id
        WHERE pd.week_id = ?
        ORDER BY pd.part_code_snapshot, pd.id
        """,
        (week_id,),
    ).fetchall()


def assignment_rows(
    connection: sqlite3.Connection, week_id: int
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT a.*, e.name AS employee_name,
               COALESCE(poi.part_code_snapshot, pd.part_code_snapshot) AS part_code,
               COALESCE(poi.part_name_snapshot, pd.part_name_snapshot) AS part_name,
               po.id AS production_order_id, po.order_type,
               poi.is_dual_usage_snapshot,
               po.source_code_snapshot AS source_code,
               po.source_name_snapshot AS source_name
        FROM assignments a
        JOIN employees e ON e.id = a.employee_id
        JOIN part_demands pd ON pd.week_id = a.week_id AND pd.part_id = a.part_id
        LEFT JOIN production_order_items poi ON poi.id = a.order_item_id
        LEFT JOIN production_orders po ON po.id = poi.order_id
        WHERE a.week_id = ?
        ORDER BY a.work_date, e.name, pd.part_code_snapshot
        """,
        (week_id,),
    ).fetchall()


def overtime_map(
    connection: sqlite3.Connection, week_id: int
) -> dict[tuple[int, str], float]:
    rows = connection.execute(
        "SELECT * FROM overtime_approvals WHERE week_id = ?", (week_id,)
    ).fetchall()
    return {
        (int(row["employee_id"]), row["work_date"]): float(row["hours"])
        for row in rows
    }


def manual_overtime_map(
    connection: sqlite3.Connection, week_id: int
) -> dict[tuple[int, str], float]:
    rows = connection.execute(
        """
        SELECT * FROM overtime_approvals
        WHERE week_id = ? AND is_manual = 1
        """,
        (week_id,),
    ).fetchall()
    return {
        (int(row["employee_id"]), row["work_date"]): float(row["hours"])
        for row in rows
    }


def _employee_limit(employee: sqlite3.Row, settings: dict[str, float]) -> float:
    # 员工不再拥有个人加班上限；全员统一使用0或一个完整固定班次。
    return float(settings.get("overtime_block_hours", 4.0))


def _solve_core_then_reinforcement(
    demands: list[Demand],
    workers: list[Worker],
    employee_by_id: dict[int, sqlite3.Row],
) -> list[Allocation]:
    """固定成员先排满，候补人员只承接固定成员无法完成的剩余数量。"""
    core_workers = [
        worker
        for worker in workers
        if employee_by_id[worker.employee_id]["employee_type"] == "core"
    ]
    reinforcement_workers = [
        worker
        for worker in workers
        if employee_by_id[worker.employee_id]["employee_type"] == "backup"
    ]
    core_allocations = solve_schedule(demands, core_workers)
    allocated_by_part: dict[int, int] = defaultdict(int)
    for allocation in core_allocations:
        allocated_by_part[allocation.part_id] += allocation.quantity
    remaining_demands = [
        Demand(
            part_id=demand.part_id,
            quantity=max(
                0,
                demand.quantity - allocated_by_part.get(demand.part_id, 0),
            ),
            minutes_per_unit=demand.minutes_per_unit,
        )
        for demand in demands
    ]
    reinforcement_allocations = solve_schedule(
        remaining_demands,
        reinforcement_workers,
    )
    return sorted(
        [*core_allocations, *reinforcement_allocations],
        key=lambda item: (item.work_date, item.employee_id, item.part_id),
    )


def run_generator(
    connection: sqlite3.Connection,
    week_id: int,
    overtime_employee_ids: list[int] | None = None,
) -> None:
    week = week_row(connection, week_id)
    ensure_editable(week)
    settings = settings_from_snapshot(week["settings_snapshot"])
    connection.execute(
        """
        INSERT OR IGNORE INTO week_members (week_id, employee_id, source)
        SELECT ?, id, 'core'
        FROM employees
        WHERE active = 1 AND employee_type = 'core'
        """,
        (week_id,),
    )
    employees = selected_employees(connection, week_id)
    skills = employee_skills(connection, [int(row["id"]) for row in employees])
    ensure_week_availability_snapshot(connection, week, employees, settings)
    availability = availability_map(connection, week, employees, settings)
    days = active_dates(week["week_start"], bool(week["include_weekend"]))
    overtime_ids = set(overtime_employee_ids or [])
    manual_overtime = manual_overtime_map(connection, week_id)

    workers: list[Worker] = []
    employee_by_id = {int(row["id"]): row for row in employees}
    for employee in employees:
        employee_id = int(employee["id"])
        capacities = {
            day: int(
                round(
                    (
                        availability[(employee_id, day)]
                        + manual_overtime.get(
                            (employee_id, day),
                            0,
                        )
                    )
                    * settings["efficiency"]
                    * 60
                )
            )
            for day in days
        }
        workers.append(
            Worker(
                employee_id=employee_id,
                skill_part_ids=frozenset(skills.get(employee_id, set())),
                capacities=capacities,
            )
        )

    demands_db = demand_rows(connection, week_id)
    demands = [
        Demand(
            part_id=int(row["part_id"]),
            quantity=int(row["quantity"]),
            minutes_per_unit=max(1, int(round(float(row["standard_hours_snapshot"]) * 60))),
        )
        for row in demands_db
        if int(row["quantity"]) > 0
    ]
    allocations = _solve_core_then_reinforcement(
        demands,
        workers,
        employee_by_id,
    )
    allocated_by_part: dict[int, int] = defaultdict(int)
    for allocation in allocations:
        allocated_by_part[allocation.part_id] += allocation.quantity
    overtime_allocations: list[Allocation] = []
    overtime_used_days: set[tuple[int, str]] = set()
    demand_minutes = {
        demand.part_id: demand.minutes_per_unit for demand in demands
    }
    block_hours = float(settings.get("overtime_block_hours", 4.0))
    if overtime_ids:
        for demand in demands:
            remaining = max(
                0,
                demand.quantity - allocated_by_part.get(demand.part_id, 0),
            )
            if remaining == 0:
                continue
            for day in reversed(days):
                for employee_id in sorted(overtime_ids):
                    employee = employee_by_id.get(employee_id)
                    if (
                        employee is None
                        or demand.part_id not in skills.get(employee_id, set())
                        or availability[(employee_id, day)] <= 0
                        or (employee_id, day) in manual_overtime
                        or _employee_limit(employee, settings) + 0.001
                        < block_hours
                    ):
                        continue
                    capacity = int(
                        round(block_hours * settings["efficiency"] * 60)
                    )
                    already_used = sum(
                        allocation.quantity * demand_minutes[allocation.part_id]
                        for allocation in overtime_allocations
                        if allocation.employee_id == employee_id
                        and allocation.work_date == day
                    )
                    fit = max(0, (capacity - already_used) // demand.minutes_per_unit)
                    quantity = min(remaining, fit)
                    if quantity <= 0:
                        continue
                    overtime_allocations.append(
                        Allocation(
                            employee_id=employee_id,
                            part_id=demand.part_id,
                            work_date=day,
                            quantity=quantity,
                        )
                    )
                    overtime_used_days.add((employee_id, day))
                    remaining -= quantity
                    if remaining == 0:
                        break
                if remaining == 0:
                    break
    allocations = sorted(
        [*allocations, *overtime_allocations],
        key=lambda item: (item.work_date, item.employee_id, item.part_id),
    )

    connection.execute("DELETE FROM assignments WHERE week_id = ?", (week_id,))
    connection.execute(
        "DELETE FROM overtime_approvals WHERE week_id = ? AND is_manual = 0",
        (week_id,),
    )
    hours_by_part = {
        int(row["part_id"]): float(row["standard_hours_snapshot"])
        for row in demands_db
    }
    for allocation in allocations:
        connection.execute(
            """
            INSERT INTO assignments
                (week_id, employee_id, part_id, work_date, quantity,
                 standard_hours_snapshot, source)
            VALUES (?, ?, ?, ?, ?, ?, 'generated')
            """,
            (
                week_id,
                allocation.employee_id,
                allocation.part_id,
                allocation.work_date,
                allocation.quantity,
                hours_by_part[allocation.part_id],
            ),
        )

    for employee_id, day in sorted(overtime_used_days):
        connection.execute(
            """
            INSERT INTO overtime_approvals
                (week_id, employee_id, work_date, hours, is_manual)
            VALUES (?, ?, ?, ?, 0)
            """,
            (week_id, employee_id, day, block_hours),
        )
    refresh_week_status(connection, week_id)


def _summary_data(
    connection: sqlite3.Connection, week_id: int
) -> tuple[
    sqlite3.Row,
    dict[str, float],
    list[sqlite3.Row],
    list[sqlite3.Row],
    list[sqlite3.Row],
    dict[tuple[int, str], float],
    dict[tuple[int, str], float],
]:
    week = week_row(connection, week_id)
    settings = settings_from_snapshot(week["settings_snapshot"])
    employees = selected_employees(connection, week_id)
    demands = demand_rows(connection, week_id)
    assignments = assignment_rows(connection, week_id)
    availability = availability_map(connection, week, employees, settings)
    overtime = overtime_map(connection, week_id)
    return week, settings, employees, demands, assignments, availability, overtime


def calculate_week(connection: sqlite3.Connection, week_id: int) -> dict[str, Any]:
    (
        week,
        settings,
        employees,
        demands,
        assignments,
        availability,
        overtime,
    ) = _summary_data(connection, week_id)
    days = active_dates(week["week_start"], bool(week["include_weekend"]))
    skills = employee_skills(connection)
    manual_overtime_keys = set(manual_overtime_map(connection, week_id))
    assigned_by_part: dict[int, int] = defaultdict(int)
    load: dict[tuple[int, str], float] = defaultdict(float)
    assignment_items: list[dict[str, Any]] = []
    for row in assignments:
        part_id = int(row["part_id"])
        employee_id = int(row["employee_id"])
        hours = int(row["quantity"]) * float(row["standard_hours_snapshot"])
        assigned_by_part[part_id] += int(row["quantity"])
        load[(employee_id, row["work_date"])] += hours
        item = row_dict(row)
        item["standard_hours"] = round(hours, 4)
        assignment_items.append(item)

    demand_sources: dict[int, list[dict[str, Any]]] = defaultdict(list)
    source_rows = connection.execute(
        """
        SELECT poi.part_id, wod.order_item_id, wod.quantity,
               poi.standard_hours_snapshot, poi.is_dual_usage_snapshot,
               po.id AS production_order_id, po.order_type,
               po.source_code_snapshot AS source_code,
               po.source_name_snapshot AS source_name,
               COALESCE((SELECT SUM(a.quantity) FROM assignments a
                         WHERE a.week_id = wod.week_id
                           AND a.order_item_id = wod.order_item_id), 0) AS assigned_quantity
        FROM week_order_demands wod
        JOIN production_order_items poi ON poi.id = wod.order_item_id
        JOIN production_orders po ON po.id = poi.order_id
        WHERE wod.week_id = ?
        ORDER BY po.order_type, po.source_code_snapshot, po.id
        """,
        (week_id,),
    ).fetchall()
    for source in source_rows:
        demand_sources[int(source["part_id"])].append(
            {
                "order_item_id": int(source["order_item_id"]),
                "production_order_id": int(source["production_order_id"]),
                "order_type": source["order_type"],
                "is_dual_usage": bool(source["is_dual_usage_snapshot"]),
                "source_code": source["source_code"],
                "source_name": source["source_name"],
                "quantity": int(source["quantity"]),
                "assigned_quantity": int(source["assigned_quantity"]),
                "standard_hours": float(source["standard_hours_snapshot"]),
            }
        )

    demand_items: list[dict[str, Any]] = []
    remaining_part_ids: set[int] = set()
    remaining_hours = 0.0
    total_required_hours = 0.0
    for row in demands:
        part_id = int(row["part_id"])
        sources = demand_sources.get(part_id, [])
        if sources:
            quantity = sum(source["quantity"] for source in sources)
            assigned = sum(source["assigned_quantity"] for source in sources)
            remaining = sum(
                max(0, source["quantity"] - source["assigned_quantity"])
                for source in sources
            )
            required_part_hours = sum(
                source["quantity"] * source["standard_hours"] for source in sources
            )
            remaining_part_hours = sum(
                max(0, source["quantity"] - source["assigned_quantity"])
                * source["standard_hours"]
                for source in sources
            )
            unit_hours = required_part_hours / quantity if quantity else 0.0
        else:
            quantity = int(row["quantity"])
            assigned = assigned_by_part[part_id]
            remaining = max(0, quantity - assigned)
            unit_hours = float(row["standard_hours_snapshot"])
            required_part_hours = quantity * unit_hours
            remaining_part_hours = remaining * unit_hours
        total_required_hours += required_part_hours
        remaining_hours += remaining_part_hours
        if remaining:
            remaining_part_ids.add(part_id)
        demand_items.append(
            {
                "id": int(row["id"]),
                "part_id": part_id,
                "part_code": row["part_code_snapshot"],
                "part_name": row["part_name_snapshot"],
                "standard_hours": unit_hours,
                "quantity": quantity,
                "assigned_quantity": assigned,
                "remaining_quantity": remaining,
                "sources": sources,
            }
        )

    employee_items: list[dict[str, Any]] = []
    unapproved_overload = False
    for employee in employees:
        employee_id = int(employee["id"])
        day_items = []
        for day in days:
            assigned_hours = load[(employee_id, day)]
            available_hours = availability[(employee_id, day)]
            normal_capacity = available_hours * settings["efficiency"]
            approved_overtime = overtime.get((employee_id, day), 0.0)
            required_overtime = (
                max(0.0, assigned_hours - normal_capacity) / settings["efficiency"]
            )
            if required_overtime > approved_overtime + 0.001:
                unapproved_overload = True
            utilization = (
                assigned_hours / normal_capacity
                if normal_capacity > 0
                else (999 if assigned_hours > 0 else 0)
            )
            day_items.append(
                {
                    "date": day,
                    "availability_hours": round(available_hours, 4),
                    "normal_capacity": round(normal_capacity, 4),
                    "assigned_hours": round(assigned_hours, 4),
                    "estimated_actual_hours": round(
                        assigned_hours / settings["efficiency"], 4
                    ),
                    "utilization": round(utilization, 4),
                    "approved_overtime_hours": round(approved_overtime, 4),
                    "overtime_is_manual": (employee_id, day)
                    in manual_overtime_keys,
                    "required_overtime_hours": round(required_overtime, 4),
                }
            )
        employee_data = row_dict(employee)
        employee_data["unavailable_weekdays"] = json.loads(
            employee["unavailable_weekdays"]
        )
        employee_items.append(
            {
                **employee_data,
                "skill_part_ids": sorted(skills.get(employee_id, set())),
                "days": day_items,
                "week_assigned_hours": round(
                    sum(item["assigned_hours"] for item in day_items), 4
                ),
            }
        )

    efficiency_employees = [
        employee for employee in employees if employee["employee_type"] == "core"
    ]
    daily_efficiency = []
    for day in days:
        available_hours = sum(
            availability[(int(employee["id"]), day)]
            for employee in efficiency_employees
        )
        assigned_hours = sum(
            load[(int(employee["id"]), day)]
            for employee in efficiency_employees
        )
        efficiency = (
            assigned_hours / available_hours if available_hours > 0 else 0.0
        )
        daily_efficiency.append(
            {
                "date": day,
                "assigned_hours": round(assigned_hours, 4),
                "available_hours": round(available_hours, 4),
                "efficiency": round(efficiency, 4),
            }
        )

    selected_ids = {int(row["id"]) for row in employees}
    priorities = employee_skill_priorities(connection)
    source_rules_by_part: dict[int, list[int | None]] = defaultdict(list)
    for demand in demand_items:
        for source in demand["sources"]:
            if source["quantity"] <= source["assigned_quantity"]:
                continue
            if source["order_type"] == "machine":
                source_rules_by_part[int(demand["part_id"])].extend([1, 2])
            elif source.get("is_dual_usage"):
                source_rules_by_part[int(demand["part_id"])].append(2)
            else:
                source_rules_by_part[int(demand["part_id"])].append(None)

    def covers_remaining(employee_id: int, part_id: int) -> bool:
        if part_id not in skills.get(employee_id, set()):
            return False
        rules = source_rules_by_part.get(part_id, [None])
        priority = priorities.get((employee_id, part_id))
        return any(rule is None or priority == rule for rule in rules)
    all_active = connection.execute(
        "SELECT * FROM employees WHERE active = 1 ORDER BY id"
    ).fetchall()
    part_name_by_id = {
        int(row["part_id"]): row["part_name_snapshot"] for row in demands
    }
    demand_item_by_part_id = {
        int(item["part_id"]): item for item in demand_items
    }

    def candidate_item(employee: sqlite3.Row, is_overtime: bool) -> dict[str, Any]:
        employee_id = int(employee["id"])
        coverage = sorted(
            part_id
            for part_id in remaining_part_ids
            if covers_remaining(employee_id, part_id)
        )
        if is_overtime:
            daily_limit = _employee_limit(employee, settings)
            if employee_id in selected_ids:
                working_day_count = sum(
                    availability[(employee_id, day)] > 0 for day in days
                )
            else:
                defaults = default_availability_for_employee(
                    employee, days, settings["daily_hours"]
                )
                working_day_count = sum(hours > 0 for hours in defaults.values())
            block_hours = float(settings.get("overtime_block_hours", 4.0))
            capacity = (
                block_hours * settings["efficiency"] * working_day_count
                if daily_limit + 0.001 >= block_hours
                else 0.0
            )
        else:
            defaults = default_availability_for_employee(
                employee, days, settings["daily_hours"]
            )
            capacity = sum(defaults.values()) * settings["efficiency"]
        return {
            "employee_id": employee_id,
            "name": employee["name"],
            "employee_type": employee["employee_type"],
            "coverage_part_ids": coverage,
            "coverage_parts": [part_name_by_id[part_id] for part_id in coverage],
            "available_capacity": round(capacity, 4),
            "overtime_block_hours": (
                round(float(settings.get("overtime_block_hours", 4.0)), 4)
                if is_overtime
                else None
            ),
        }

    reinforcement_candidates = [
        candidate_item(employee, False)
        for employee in all_active
        if employee["employee_type"] == "backup"
        and int(employee["id"]) not in selected_ids
        and any(
            covers_remaining(int(employee["id"]), part_id)
            for part_id in remaining_part_ids
        )
    ]
    overtime_candidates = [
        candidate_item(employee, True)
        for employee in employees
        if any(
            covers_remaining(int(employee["id"]), part_id)
            for part_id in remaining_part_ids
        )
        and _employee_limit(employee, settings) + 0.001
        >= float(settings.get("overtime_block_hours", 4.0))
    ]
    reinforcement_candidates.sort(
        key=lambda item: (-len(item["coverage_part_ids"]), -item["available_capacity"], item["employee_id"])
    )
    overtime_candidates.sort(
        key=lambda item: (-len(item["coverage_part_ids"]), -item["available_capacity"], item["employee_id"])
    )

    missing_current_skill = [
        part_id
        for part_id in remaining_part_ids
        if not any(covers_remaining(employee_id, part_id) for employee_id in selected_ids)
    ]
    threshold = settings["shortage_threshold"]
    suggestion: str | None = None
    if remaining_hours > 0:
        if missing_current_skill and reinforcement_candidates:
            suggestion = "reinforcement"
        elif remaining_hours > threshold:
            suggestion = "reinforcement" if reinforcement_candidates else "no_capacity"
        elif overtime_candidates:
            suggestion = "overtime"
        elif reinforcement_candidates:
            suggestion = "reinforcement"
        else:
            suggestion = "no_skill"
    adjustment_data = None
    adjustment = connection.execute(
        """
        SELECT id, status, created_at, snapshot_json
        FROM week_adjustments
        WHERE week_id = ? AND status = 'active'
        """,
        (week_id,),
    ).fetchone()
    if adjustment is not None:
        adjustment_data = {
            "id": int(adjustment["id"]),
            "status": adjustment["status"],
            "created_at": adjustment["created_at"],
            **json.loads(adjustment["snapshot_json"]).get(
                "adjustment_policy", {}
            ),
        }

    return {
        "id": int(week["id"]),
        "week_start": week["week_start"],
        "include_weekend": bool(week["include_weekend"]),
        "status": week["status"],
        "confirmed_at": week["confirmed_at"],
        "active_adjustment": adjustment_data,
        "settings": settings,
        "days": days,
        "demands": demand_items,
        "employees": employee_items,
        "daily_efficiency": daily_efficiency,
        "assignments": assignment_items,
        "summary": {
            "total_required_hours": round(total_required_hours, 4),
            "scheduled_hours": round(total_required_hours - remaining_hours, 4),
            "remaining_hours": round(remaining_hours, 4),
            "shortage_threshold": round(threshold, 4),
            "unapproved_overload": unapproved_overload,
            "has_schedule_data": bool(
                assignments
                or overtime
                or connection.execute(
                    "SELECT 1 FROM week_members WHERE week_id = ? LIMIT 1",
                    (week_id,),
                ).fetchone()
            ),
        },
        "shortage": {
            "suggestion": suggestion,
            "missing_skill_parts": [
                {
                    "part_id": part_id,
                    "part_code": demand_item_by_part_id[part_id]["part_code"],
                    "part_name": part_name_by_id[part_id],
                    "remaining_quantity": demand_item_by_part_id[part_id][
                        "remaining_quantity"
                    ],
                }
                for part_id in missing_current_skill
            ],
            "reinforcement_candidates": reinforcement_candidates,
            "overtime_candidates": overtime_candidates,
        },
    }


def refresh_week_status(connection: sqlite3.Connection, week_id: int) -> str:
    week = week_row(connection, week_id)
    if week["status"] == "confirmed":
        return "confirmed"
    detail = calculate_week(connection, week_id)
    has_demand = any(item["quantity"] > 0 for item in detail["demands"])
    if not has_demand and not detail["assignments"]:
        status = "draft"
    elif (
        detail["summary"]["remaining_hours"] > 0
        or detail["summary"]["unapproved_overload"]
    ):
        status = "shortage"
    else:
        status = "ready"
    connection.execute(
        "UPDATE week_plans SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, week_id),
    )
    return status


def unconfirm_week(connection: sqlite3.Connection, week_id: int) -> None:
    week = week_row(connection, week_id)
    if week["status"] != "confirmed":
        return
    connection.execute(
        """
        UPDATE week_plans
        SET status = 'ready', confirmed_at = NULL, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (week_id,),
    )
    refresh_week_status(connection, week_id)


def reset_week_schedule(connection: sqlite3.Connection, week_id: int) -> None:
    week = week_row(connection, week_id)
    ensure_editable(week)
    connection.execute("DELETE FROM assignments WHERE week_id = ?", (week_id,))
    connection.execute(
        "DELETE FROM overtime_approvals WHERE week_id = ?", (week_id,)
    )
    connection.execute("DELETE FROM week_members WHERE week_id = ?", (week_id,))
    connection.execute(
        """
        UPDATE week_plans
        SET status = 'draft', confirmed_at = NULL, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (week_id,),
    )


def replace_assignments(
    connection: sqlite3.Connection,
    week_id: int,
    items: list[dict[str, Any]],
) -> None:
    week = week_row(connection, week_id)
    ensure_editable(week)
    settings = settings_from_snapshot(week["settings_snapshot"])
    employees = selected_employees(connection, week_id)
    employee_by_id = {int(row["id"]): row for row in employees}
    skills = employee_skills(connection, list(employee_by_id))
    priorities = employee_skill_priorities(connection, list(employee_by_id))
    demands = demand_rows(connection, week_id)
    demand_by_id = {int(row["part_id"]): row for row in demands}
    days = set(active_dates(week["week_start"], bool(week["include_weekend"])))
    availability = availability_map(connection, week, employees, settings)
    manual_overtime = manual_overtime_map(connection, week_id)

    quantity_by_part: dict[int, int] = defaultdict(int)
    quantity_by_order_item: dict[int, int] = defaultdict(int)
    load: dict[tuple[int, str], float] = defaultdict(float)
    grouped: dict[tuple[int, int, int | None, str], int] = defaultdict(int)
    hours_by_group: dict[tuple[int, int, int | None, str], float] = {}
    for item in items:
        employee_id = int(item["employee_id"])
        part_id = int(item["part_id"])
        raw_order_item_id = item.get("order_item_id")
        order_item_id = int(raw_order_item_id) if raw_order_item_id is not None else None
        work_date = str(item["work_date"])
        quantity = int(item["quantity"])
        if employee_id not in employee_by_id:
            raise HTTPException(status_code=422, detail="员工尚未加入本周排班")
        if part_id not in demand_by_id:
            raise HTTPException(status_code=422, detail="零件不在本周需求中")
        if part_id not in skills.get(employee_id, set()):
            raise HTTPException(status_code=422, detail="员工不具备该零件技能")
        if work_date not in days:
            raise HTTPException(status_code=422, detail="排班日期不属于本周工作日")
        matching_sources = connection.execute(
            """
            SELECT wod.order_item_id, wod.quantity, po.start_date, po.end_date,
                   po.order_type, poi.is_dual_usage_snapshot,
                   poi.standard_hours_snapshot AS source_hours
            FROM week_order_demands wod
            JOIN production_order_items poi ON poi.id = wod.order_item_id
            JOIN production_orders po ON po.id = poi.order_id
            WHERE wod.week_id = ? AND poi.part_id = ?
            ORDER BY wod.order_item_id
            """,
            (week_id, part_id),
        ).fetchall()
        if order_item_id is None and len(matching_sources) == 1:
            order_item_id = int(matching_sources[0]["order_item_id"])
        elif order_item_id is None and len(matching_sources) > 1:
            raise HTTPException(status_code=422, detail="同一零件有多个任务来源，请选择具体来源")
        if order_item_id is not None:
            selected_source = next(
                (row for row in matching_sources if int(row["order_item_id"]) == order_item_id),
                None,
            )
            if selected_source is None:
                raise HTTPException(status_code=422, detail="任务来源不属于本周需求")
            if not (selected_source["start_date"] <= work_date <= selected_source["end_date"]):
                raise HTTPException(status_code=422, detail="手工排班日期超出生产任务日期范围")
            priority = priorities.get((employee_id, part_id))
            if selected_source["order_type"] == "machine" and priority not in {1, 2}:
                raise HTTPException(
                    status_code=422,
                    detail="整机任务只能分配给该零件配置的员工1或员工2",
                )
            if (
                selected_source["order_type"] == "accessory"
                and bool(selected_source["is_dual_usage_snapshot"])
                and priority != 2
            ):
                raise HTTPException(
                    status_code=422,
                    detail="双用途附件订单只能分配给该零件的员工2",
                )
            quantity_by_order_item[order_item_id] += quantity
            item_hours = float(selected_source["source_hours"])
        else:
            item_hours = float(demand_by_id[part_id]["standard_hours_snapshot"])
        group_key = (employee_id, part_id, order_item_id, work_date)
        grouped[group_key] += quantity
        hours_by_group[group_key] = item_hours
        quantity_by_part[part_id] += quantity
        load[(employee_id, work_date)] += quantity * item_hours

    for part_id, quantity in quantity_by_part.items():
        if quantity > int(demand_by_id[part_id]["quantity"]):
            raise HTTPException(status_code=422, detail="零件分配数量超过本周需求")
    for order_item_id, quantity in quantity_by_order_item.items():
        limit = connection.execute(
            "SELECT quantity FROM week_order_demands WHERE week_id = ? AND order_item_id = ?",
            (week_id, order_item_id),
        ).fetchone()
        if limit is None or quantity > int(limit["quantity"]):
            raise HTTPException(status_code=422, detail="任务来源分配数量超过本周需求")
    for (employee_id, work_date), assigned_hours in load.items():
        employee = employee_by_id[employee_id]
        overtime_limit = manual_overtime.get(
            (employee_id, work_date),
            _employee_limit(employee, settings),
        )
        maximum = (
            availability[(employee_id, work_date)]
            + overtime_limit
        ) * settings["efficiency"]
        if assigned_hours > maximum + 0.001:
            raise HTTPException(
                status_code=422,
                detail=f"{employee['name']}在{work_date}的任务超过正常工时与加班上限",
            )

    connection.execute("DELETE FROM assignments WHERE week_id = ?", (week_id,))
    for group_key, quantity in grouped.items():
        employee_id, part_id, order_item_id, work_date = group_key
        connection.execute(
            """
            INSERT INTO assignments
                (week_id, employee_id, part_id, work_date, quantity,
                standard_hours_snapshot, order_item_id, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'manual')
            """,
            (
                week_id,
                employee_id,
                part_id,
                work_date,
                quantity,
                hours_by_group[group_key],
                order_item_id,
            ),
        )
    refresh_week_status(connection, week_id)


def approve_required_overtime(connection: sqlite3.Connection, week_id: int) -> None:
    week = week_row(connection, week_id)
    ensure_editable(week)
    detail = calculate_week(connection, week_id)
    settings = detail["settings"]
    employees = {item["id"]: item for item in detail["employees"]}
    manual_overtime = manual_overtime_map(connection, week_id)
    connection.execute(
        "DELETE FROM overtime_approvals WHERE week_id = ? AND is_manual = 0",
        (week_id,),
    )
    for employee in detail["employees"]:
        block_hours = float(settings.get("overtime_block_hours", 4.0))
        for day in employee["days"]:
            required = day["required_overtime_hours"]
            if (employee["id"], day["date"]) in manual_overtime:
                continue
            if required > block_hours + 0.001:
                raise HTTPException(status_code=422, detail="所需加班超过一个固定班次")
            if required > 0:
                connection.execute(
                    """
                    INSERT INTO overtime_approvals
                        (week_id, employee_id, work_date, hours, is_manual)
                    VALUES (?, ?, ?, ?, 0)
                    """,
                    (week_id, employee["id"], day["date"], block_hours),
                )
    refresh_week_status(connection, week_id)


def confirm_week(connection: sqlite3.Connection, week_id: int) -> None:
    week = week_row(connection, week_id)
    ensure_editable(week)
    if connection.execute(
        "SELECT 1 FROM production_orders WHERE status != 'legacy' AND needs_generation = 1 LIMIT 1"
    ).fetchone():
        raise HTTPException(
            status_code=409,
            detail="生产任务已发生变化，请先一键重新生成跨周排班",
        )
    status = refresh_week_status(connection, week_id)
    if status != "ready":
        raise HTTPException(status_code=409, detail="仍有任务缺口或未批准加班，不能确认")
    connection.execute(
        """
        UPDATE week_plans
        SET status = 'confirmed', confirmed_at = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (datetime.now(timezone.utc).isoformat(), week_id),
    )
    connection.execute(
        """
        UPDATE week_adjustments
        SET status = 'applied', updated_at = CURRENT_TIMESTAMP
        WHERE week_id = ? AND status = 'active'
        """,
        (week_id,),
    )
