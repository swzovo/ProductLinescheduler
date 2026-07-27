from backend.app.scheduler import Demand, Worker, active_dates, solve_schedule


def test_active_dates_include_weekend():
    assert active_dates("2026-07-20", False) == [
        "2026-07-20",
        "2026-07-21",
        "2026-07-22",
        "2026-07-23",
        "2026-07-24",
    ]
    assert active_dates("2026-07-20", True)[-2:] == [
        "2026-07-25",
        "2026-07-26",
    ]


def test_solver_obeys_skill_and_integer_capacity():
    allocations = solve_schedule(
        [Demand(part_id=1, quantity=10, minutes_per_unit=60)],
        [
            Worker(
                employee_id=1,
                skill_part_ids=frozenset({1}),
                capacities={"2026-07-20": 360},
            ),
            Worker(
                employee_id=2,
                skill_part_ids=frozenset(),
                capacities={"2026-07-20": 360},
            ),
        ],
    )
    assert sum(item.quantity for item in allocations) == 6
    assert {item.employee_id for item in allocations} == {1}


def test_solver_balances_equal_workers():
    allocations = solve_schedule(
        [Demand(part_id=1, quantity=10, minutes_per_unit=60)],
        [
            Worker(1, frozenset({1}), {"2026-07-20": 360}),
            Worker(2, frozenset({1}), {"2026-07-20": 360}),
        ],
    )
    by_employee = {
        employee_id: sum(
            item.quantity for item in allocations if item.employee_id == employee_id
        )
        for employee_id in (1, 2)
    }
    assert sum(by_employee.values()) == 10
    assert abs(by_employee[1] - by_employee[2]) <= 1


def test_solver_balances_work_across_available_days():
    days = [f"2026-07-{day}" for day in range(20, 25)]
    allocations = solve_schedule(
        [Demand(part_id=1, quantity=10, minutes_per_unit=60)],
        [Worker(1, frozenset({1}), {day: 360 for day in days})],
    )
    by_day = {
        day: sum(item.quantity for item in allocations if item.work_date == day)
        for day in days
    }
    assert by_day == {day: 2 for day in days}


def test_unit_that_cannot_fit_is_left_unassigned():
    allocations = solve_schedule(
        [Demand(part_id=1, quantity=1, minutes_per_unit=500)],
        [Worker(1, frozenset({1}), {"2026-07-20": 405})],
    )
    assert allocations == []
