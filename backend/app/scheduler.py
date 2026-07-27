from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix


@dataclass(frozen=True)
class Demand:
    part_id: int
    quantity: int
    minutes_per_unit: int


@dataclass(frozen=True)
class Worker:
    employee_id: int
    skill_part_ids: frozenset[int]
    capacities: dict[str, int]


@dataclass(frozen=True)
class Allocation:
    employee_id: int
    part_id: int
    work_date: str
    quantity: int


def _balance_allocations_across_days(
    allocations: list[Allocation],
    demands: list[Demand],
    workers: list[Worker],
) -> list[Allocation]:
    """保持员工与零件总量不变，把每名员工的任务均匀铺到可用日期。"""
    minutes_by_part = {demand.part_id: demand.minutes_per_unit for demand in demands}
    worker_by_id = {worker.employee_id: worker for worker in workers}
    original_by_employee: dict[int, list[Allocation]] = defaultdict(list)
    quantities: dict[tuple[int, int], int] = defaultdict(int)
    for allocation in allocations:
        original_by_employee[allocation.employee_id].append(allocation)
        quantities[(allocation.employee_id, allocation.part_id)] += allocation.quantity

    result: list[Allocation] = []
    for employee_id in sorted(original_by_employee):
        worker = worker_by_id[employee_id]
        loads = {day: 0 for day in sorted(worker.capacities)}
        redistributed: dict[tuple[int, str], int] = defaultdict(int)
        failed = False
        employee_parts = sorted(
            [
                (part_id, quantity)
                for (candidate_id, part_id), quantity in quantities.items()
                if candidate_id == employee_id
            ],
            key=lambda item: (-minutes_by_part[item[0]], item[0]),
        )
        for part_id, quantity in employee_parts:
            unit_minutes = minutes_by_part[part_id]
            for _ in range(quantity):
                available_days = [
                    day
                    for day, capacity in worker.capacities.items()
                    if capacity - loads[day] >= unit_minutes
                ]
                if not available_days:
                    failed = True
                    break
                day = min(
                    available_days,
                    key=lambda candidate: (
                        loads[candidate] / max(worker.capacities[candidate], 1),
                        loads[candidate],
                        candidate,
                    ),
                )
                redistributed[(part_id, day)] += 1
                loads[day] += unit_minutes
            if failed:
                break
        if failed:
            # 极端装箱组合下保留求解器的原始可行方案，绝不丢失任务。
            result.extend(original_by_employee[employee_id])
            continue
        result.extend(
            Allocation(
                employee_id=employee_id,
                part_id=part_id,
                work_date=day,
                quantity=quantity,
            )
            for (part_id, day), quantity in redistributed.items()
            if quantity > 0
        )
    return result


def active_dates(week_start: str, include_weekend: bool) -> list[str]:
    """周排班统一展示并允许逐人设置周一至周日。

    include_weekend 参数为兼容历史接口保留；是否在周末出勤由个人当周
    daily_availability 决定，默认周六、周日均为0。
    """
    start = date.fromisoformat(week_start)
    return [(start + timedelta(days=offset)).isoformat() for offset in range(7)]


def _solve_once(
    variables: list[tuple[Demand, Worker, str]],
    demands: list[Demand],
    workers: list[Worker],
    target_minutes: int | None = None,
) -> tuple[np.ndarray | None, int]:
    variable_count = len(variables)
    if not variable_count:
        return None, 0

    include_balance = target_minutes is not None
    total_vars = variable_count + (1 if include_balance else 0)
    z_index = variable_count
    demand_rows = {d.part_id: index for index, d in enumerate(demands)}
    worker_day_keys = sorted(
        {(w.employee_id, day) for _, w, day in variables},
        key=lambda item: (item[0], item[1]),
    )
    worker_day_rows = {
        key: len(demand_rows) + index for index, key in enumerate(worker_day_keys)
    }
    base_rows = len(demand_rows) + len(worker_day_keys)
    worker_rows: dict[int, int] = {}
    if include_balance:
        worker_rows = {
            worker.employee_id: base_rows + index
            for index, worker in enumerate(sorted(workers, key=lambda w: w.employee_id))
        }
    target_row = base_rows + len(worker_rows) if include_balance else None
    row_count = base_rows + len(worker_rows) + (1 if include_balance else 0)

    matrix = lil_matrix((row_count, total_vars), dtype=float)
    lower = np.full(row_count, -np.inf)
    upper = np.full(row_count, np.inf)

    for index, demand in enumerate(demands):
        upper[index] = demand.quantity

    for key, row in worker_day_rows.items():
        employee_id, day = key
        worker = next(w for w in workers if w.employee_id == employee_id)
        upper[row] = worker.capacities[day]

    minutes_vector = np.zeros(variable_count, dtype=float)
    for column, (demand, worker, day) in enumerate(variables):
        matrix[demand_rows[demand.part_id], column] = 1
        matrix[worker_day_rows[(worker.employee_id, day)], column] = demand.minutes_per_unit
        minutes_vector[column] = demand.minutes_per_unit

    if include_balance:
        for worker in workers:
            row = worker_rows[worker.employee_id]
            weekly_capacity = sum(worker.capacities.values())
            for column, (demand, candidate, _) in enumerate(variables):
                if candidate.employee_id == worker.employee_id:
                    matrix[row, column] = demand.minutes_per_unit
            matrix[row, z_index] = -max(weekly_capacity, 1)
            upper[row] = 0
        assert target_row is not None
        matrix[target_row, :variable_count] = minutes_vector
        lower[target_row] = target_minutes

    c = np.zeros(total_vars, dtype=float)
    if include_balance:
        c[z_index] = 1
        for column in range(variable_count):
            c[column] = (column + 1) * 1e-8
    else:
        c[:variable_count] = -minutes_vector

    integrality = np.zeros(total_vars, dtype=int)
    integrality[:variable_count] = 1
    lower_bounds = np.zeros(total_vars)
    upper_bounds = np.full(total_vars, np.inf)
    if include_balance:
        upper_bounds[z_index] = 2

    result = milp(
        c=c,
        integrality=integrality,
        bounds=Bounds(lower_bounds, upper_bounds),
        constraints=LinearConstraint(matrix.tocsr(), lower, upper),
        options={"time_limit": 10, "mip_rel_gap": 0},
    )
    if not result.success or result.x is None:
        return None, 0
    scheduled_minutes = int(round(float(np.dot(minutes_vector, result.x[:variable_count]))))
    return result.x[:variable_count], scheduled_minutes


def solve_schedule(
    demands: Iterable[Demand],
    workers: Iterable[Worker],
) -> list[Allocation]:
    demand_list = sorted(
        [d for d in demands if d.quantity > 0],
        key=lambda d: (-d.minutes_per_unit, d.part_id),
    )
    worker_list = sorted(workers, key=lambda w: w.employee_id)
    variables: list[tuple[Demand, Worker, str]] = []
    for demand in demand_list:
        for worker in worker_list:
            if demand.part_id not in worker.skill_part_ids:
                continue
            for day in sorted(worker.capacities):
                if worker.capacities[day] >= demand.minutes_per_unit:
                    variables.append((demand, worker, day))

    first_solution, best_minutes = _solve_once(variables, demand_list, worker_list)
    if first_solution is None:
        return []
    balanced_solution, _ = _solve_once(
        variables, demand_list, worker_list, target_minutes=best_minutes
    )
    values = balanced_solution if balanced_solution is not None else first_solution
    allocations: list[Allocation] = []
    for value, (demand, worker, day) in zip(values, variables, strict=True):
        quantity = int(round(value))
        if quantity > 0:
            allocations.append(
                Allocation(
                    employee_id=worker.employee_id,
                    part_id=demand.part_id,
                    work_date=day,
                    quantity=quantity,
                )
            )
    balanced_allocations = _balance_allocations_across_days(
        allocations, demand_list, worker_list
    )
    return sorted(
        balanced_allocations,
        key=lambda item: (item.work_date, item.employee_id, item.part_id),
    )


def load_by_employee_day(
    allocations: Iterable[Allocation], minutes_by_part: dict[int, int]
) -> dict[tuple[int, str], int]:
    result: dict[tuple[int, str], int] = defaultdict(int)
    for allocation in allocations:
        result[(allocation.employee_id, allocation.work_date)] += (
            allocation.quantity * minutes_by_part[allocation.part_id]
        )
    return dict(result)
