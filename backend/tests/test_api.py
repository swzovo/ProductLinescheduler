from __future__ import annotations

import io
from pathlib import Path
from xml.sax.saxutils import escape
import zipfile

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app


def make_xlsx(rows: list[list[object]]) -> bytes:
    """构造测试用的最小xlsx，避免测试套件额外依赖Excel库。"""

    def column_name(index: int) -> str:
        result = ""
        value = index + 1
        while value:
            value, remainder = divmod(value - 1, 26)
            result = chr(65 + remainder) + result
        return result

    xml_rows: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells: list[str] = []
        for column_index, value in enumerate(row):
            if value is None or value == "":
                continue
            reference = f"{column_name(column_index)}{row_index}"
            if isinstance(value, (int, float)):
                cells.append(f'<c r="{reference}"><v>{value}</v></c>')
            elif isinstance(value, str) and value.startswith("="):
                cells.append(
                    f'<c r="{reference}"><f>{escape(value[1:])}</f><v>1</v></c>'
                )
            else:
                cells.append(
                    f'<c r="{reference}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
                )
        xml_rows.append(
            f'<row r="{row_index}">{"".join(cells)}</row>'
        )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        )
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCHEDULER_DB_PATH", str(tmp_path / "scheduler.db"))
    monkeypatch.setenv("SCHEDULER_DOWNLOAD_DIR", str(tmp_path / "Downloads"))
    monkeypatch.setenv("SCHEDULER_TODAY", "2026-07-20")
    with TestClient(app) as test_client:
        yield test_client


def create_part(client: TestClient, code="P1", hours=1.0) -> int:
    response = client.post(
        "/api/parts",
        json={
            "code": code,
            "name": f"零件{code}",
            "standard_hours": hours,
            "active": True,
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def create_employee(
    client: TestClient,
    name: str,
    employee_type: str,
    skill_part_ids: list[int],
    overtime_limit=None,
) -> int:
    response = client.post(
        "/api/employees",
        json={
            "name": name,
            "employee_type": employee_type,
            "skill_part_ids": skill_part_ids,
            "overtime_limit": overtime_limit,
            "active": True,
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def create_week(client: TestClient, part_id: int, quantity: int) -> int:
    response = client.post(
        "/api/weeks",
        json={
            "week_start": "2026-07-20",
            "include_weekend": False,
            "demands": [{"part_id": part_id, "quantity": quantity}],
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_defaults_and_capacity_formula(client: TestClient):
    settings = client.get("/api/settings").json()
    assert settings["daily_hours"] == 7.5
    assert settings["efficiency"] == 0.9
    assert settings["overtime_limit"] == 4
    assert settings["overtime_block_hours"] == 4
    assert settings["daily_hours"] * settings["efficiency"] == 6.75
    assert settings["shortage_threshold"] == 16.875
    assert settings["daily_efficiency_low_threshold"] == 0.8
    assert settings["daily_efficiency_target_threshold"] == 0.9

    part_id = create_part(client)
    create_employee(client, "张三", "core", [part_id])
    week_id = create_week(client, part_id, 5)
    detail = client.post(f"/api/weeks/{week_id}/generate").json()
    assert detail["summary"]["shortage_threshold"] == 16.875
    employee_days = detail["employees"][0]["days"]
    assert [day["normal_capacity"] for day in employee_days] == [
        6.75, 6.75, 6.75, 6.75, 6.75, 0, 0
    ]
    assert all(
        item["efficiency"] == pytest.approx(1 / 7.5, abs=0.0001)
        for item in detail["daily_efficiency"]
        if item["available_hours"] > 0
    )
    assert [item["available_hours"] for item in detail["daily_efficiency"][-2:]] == [0, 0]


def test_settings_sync_unconfirmed_default_hours_but_preserve_manual_leave(
    client: TestClient,
):
    part_id = create_part(client, code="SYNC-HOURS", hours=1)
    employee_id = create_employee(client, "工时同步员工", "core", [part_id])

    confirmed_week_id = create_week(client, part_id, 5)
    client.post(f"/api/weeks/{confirmed_week_id}/generate")
    assert client.post(
        f"/api/weeks/{confirmed_week_id}/confirm"
    ).status_code == 200

    draft = client.post(
        "/api/weeks",
        json={
            "week_start": "2026-07-27",
            "include_weekend": False,
            "demands": [{"part_id": part_id, "quantity": 5}],
        },
    )
    assert draft.status_code == 201
    draft_week_id = draft.json()["id"]
    leave = client.put(
        f"/api/weeks/{draft_week_id}/availability",
        json={
            "entries": [
                {
                    "employee_id": employee_id,
                    "work_date": "2026-07-29",
                    "hours": 0,
                }
            ]
        },
    )
    assert leave.status_code == 200

    settings = client.get("/api/settings").json()
    settings["daily_hours"] = 8.5
    saved = client.put("/api/settings", json=settings)
    assert saved.status_code == 200

    confirmed = client.get(f"/api/weeks/{confirmed_week_id}").json()
    assert [
        day["availability_hours"]
        for day in confirmed["employees"][0]["days"]
    ] == [7.5, 7.5, 7.5, 7.5, 7.5, 0, 0]

    draft_detail = client.get(f"/api/weeks/{draft_week_id}").json()
    assert draft_detail["settings"]["daily_hours"] == 8.5
    assert [
        day["availability_hours"]
        for day in draft_detail["employees"][0]["days"]
    ] == [8.5, 8.5, 0, 8.5, 8.5, 0, 0]


def test_custom_shortage_threshold_controls_reinforcement_suggestion(client: TestClient):
    settings = client.get("/api/settings").json()
    settings["shortage_threshold"] = 4
    saved = client.put("/api/settings", json=settings)
    assert saved.status_code == 200
    assert saved.json()["shortage_threshold"] == 4

    part_id = create_part(client)
    create_employee(client, "固定阈值测试", "core", [part_id])
    create_employee(client, "候补阈值测试", "backup", [part_id])
    week_id = create_week(client, part_id, 35)
    generated = client.post(f"/api/weeks/{week_id}/generate").json()

    assert generated["summary"]["remaining_hours"] == 5
    assert generated["summary"]["shortage_threshold"] == 4
    assert generated["shortage"]["suggestion"] == "reinforcement"


def test_large_shortage_requires_human_selected_backup(client: TestClient):
    part_id = create_part(client)
    create_employee(client, "固定一", "core", [part_id])
    backup_id = create_employee(client, "候补一", "backup", [part_id])
    week_id = create_week(client, part_id, 60)

    generated = client.post(f"/api/weeks/{week_id}/generate").json()
    assert generated["summary"]["remaining_hours"] == 30
    assert generated["shortage"]["suggestion"] == "reinforcement"
    assert {item["id"] for item in generated["employees"]} != {backup_id}

    resolved = client.post(
        f"/api/weeks/{week_id}/resolve",
        json={"mode": "reinforcement", "employee_ids": [backup_id]},
    ).json()
    assert backup_id in {item["id"] for item in resolved["employees"]}
    assert resolved["summary"]["remaining_hours"] == 0


def test_reinforcement_only_receives_core_shortage_and_is_excluded_from_efficiency(
    client: TestClient,
):
    part_id = create_part(client, code="REINFORCE-ONLY", hours=1)
    core_id = create_employee(client, "固定满负荷", "core", [part_id])
    backup_id = create_employee(client, "仅接缺口候补", "backup", [part_id])
    week_id = create_week(client, part_id, 35)

    before = client.post(f"/api/weeks/{week_id}/generate").json()
    core_before = {
        item["work_date"]: item["quantity"]
        for item in before["assignments"]
        if item["employee_id"] == core_id
    }
    assert sum(core_before.values()) == 30

    resolved = client.post(
        f"/api/weeks/{week_id}/resolve",
        json={"mode": "reinforcement", "employee_ids": [backup_id]},
    ).json()
    core_after = {
        item["work_date"]: item["quantity"]
        for item in resolved["assignments"]
        if item["employee_id"] == core_id
    }
    backup_quantity = sum(
        item["quantity"]
        for item in resolved["assignments"]
        if item["employee_id"] == backup_id
    )

    assert core_after == core_before
    assert backup_quantity == 5
    assert all(
        item["assigned_hours"] == 6
        and item["available_hours"] == 7.5
        and item["efficiency"] == 0.8
        for item in resolved["daily_efficiency"]
        if item["available_hours"] > 0
    )
    assert all(
        item["available_hours"] == 0
        for item in resolved["daily_efficiency"][-2:]
    )


def test_equal_threshold_uses_overtime_rule(client: TestClient):
    part_id = create_part(client, hours=0.125)
    employee_id = create_employee(client, "固定一", "core", [part_id])
    week_id = create_week(client, part_id, 135)
    entries = [
        {"employee_id": employee_id, "work_date": f"2026-07-{day}", "hours": 0}
        for day in range(20, 25)
    ]
    client.put(f"/api/weeks/{week_id}/availability", json={"entries": entries})
    result = client.post(f"/api/weeks/{week_id}/generate").json()
    assert result["summary"]["remaining_hours"] == 16.875
    assert result["shortage"]["suggestion"] == "overtime"


def test_missing_core_skill_overrides_small_shortage(client: TestClient):
    part_id = create_part(client, hours=1)
    create_employee(client, "固定一", "core", [])
    backup_id = create_employee(client, "候补一", "backup", [part_id])
    week_id = create_week(client, part_id, 3)
    result = client.post(f"/api/weeks/{week_id}/generate").json()
    assert result["shortage"]["suggestion"] == "reinforcement"
    assert result["shortage"]["reinforcement_candidates"][0]["employee_id"] == backup_id
    assert result["shortage"]["missing_skill_parts"][0]["part_id"] == part_id
    assert result["shortage"]["missing_skill_parts"][0]["part_code"] == "P1"
    assert result["shortage"]["missing_skill_parts"][0]["part_name"] == "零件P1"
    assert result["shortage"]["missing_skill_parts"][0]["remaining_quantity"] == 3


def test_small_shortage_can_be_assigned_to_selected_overtime(client: TestClient):
    part_id = create_part(client, hours=1)
    employee_id = create_employee(client, "固定一", "core", [part_id])
    week_id = create_week(client, part_id, 35)
    generated = client.post(f"/api/weeks/{week_id}/generate").json()
    assert generated["summary"]["remaining_hours"] == 5
    assert generated["shortage"]["suggestion"] == "overtime"

    resolved = client.post(
        f"/api/weeks/{week_id}/resolve",
        json={"mode": "overtime", "employee_ids": [employee_id]},
    ).json()
    assert resolved["summary"]["remaining_hours"] == 0
    assert resolved["status"] == "ready"
    assert sum(
        day["approved_overtime_hours"] for day in resolved["employees"][0]["days"]
    ) > 0


def test_employee_defaults_to_weekdays_and_week_can_enable_any_day(client: TestClient):
    part_id = create_part(client, code="SHIFT", hours=1)
    employee = client.post(
        "/api/employees",
        json={
            "name": "三天班员工",
            "employee_type": "core",
            "skill_part_ids": [part_id],
            "overtime_limit": None,
            "weekly_work_days": 3,
            "unavailable_weekdays": [5, 6],
            "active": True,
        },
    )
    assert employee.status_code == 201
    employee_id = employee.json()["id"]
    # 旧客户端仍可发送长期班次字段，但服务端统一忽略并保存周一至周五。
    assert employee.json()["weekly_work_days"] == 5
    assert employee.json()["unavailable_weekdays"] == [5, 6]

    week_id = create_week(client, part_id, 30)
    generated = client.post(f"/api/weeks/{week_id}/generate").json()
    days = generated["employees"][0]["days"]
    assert [day["availability_hours"] for day in days] == [
        7.5, 7.5, 7.5, 7.5, 7.5, 0, 0
    ]
    assert [day["assigned_hours"] for day in days] == [6, 6, 6, 6, 6, 0, 0]

    changed_week = client.put(
        f"/api/weeks/{week_id}/availability",
        json={
            "entries": [
                {
                    "employee_id": employee_id,
                    "work_date": "2026-07-21",
                    "hours": 0,
                },
                {
                    "employee_id": employee_id,
                    "work_date": "2026-07-25",
                    "hours": 7.5,
                }
            ]
        },
    )
    assert changed_week.status_code == 200
    regenerated = client.post(f"/api/weeks/{week_id}/generate").json()
    days = regenerated["employees"][0]["days"]
    assert [day["availability_hours"] for day in days] == [
        7.5, 0, 7.5, 7.5, 7.5, 7.5, 0
    ]
    assert [day["assigned_hours"] for day in days] == [6, 0, 6, 6, 6, 6, 0]


def test_daily_overtime_is_fixed_block_or_zero_and_survives_regeneration(
    client: TestClient,
):
    part_id = create_part(client, code="MANUAL-OT", hours=1)
    employee_id = create_employee(
        client,
        "人工加班员工",
        "core",
        [part_id],
        overtime_limit=None,
    )
    week_id = create_week(client, part_id, 33)
    invalid = client.put(
        f"/api/weeks/{week_id}/availability",
        json={
            "entries": [],
            "overtime_entries": [
                {
                    "employee_id": employee_id,
                    "work_date": "2026-07-20",
                    "hours": 3,
                    "manual": True,
                }
            ],
        },
    )
    assert invalid.status_code == 422
    assert "完整的4小时固定班次" in invalid.json()["detail"]

    manual = client.put(
        f"/api/weeks/{week_id}/availability",
        json={
            "entries": [],
            "overtime_entries": [
                {
                    "employee_id": employee_id,
                    "work_date": "2026-07-20",
                    "hours": 4,
                    "manual": True,
                }
            ],
        },
    )
    assert manual.status_code == 200
    monday = manual.json()["employees"][0]["days"][0]
    assert monday["approved_overtime_hours"] == 4
    assert monday["overtime_is_manual"] is True

    generated = client.post(f"/api/weeks/{week_id}/generate").json()
    assert generated["summary"]["scheduled_hours"] == 33
    monday = generated["employees"][0]["days"][0]
    assert monday["approved_overtime_hours"] == 4
    assert monday["overtime_is_manual"] is True

    settings = client.get("/api/settings").json()
    settings["overtime_limit"] = 0  # 旧客户端字段会被固定班次覆盖。
    settings["overtime_block_hours"] = 5
    saved_settings = client.put("/api/settings", json=settings)
    assert saved_settings.status_code == 200
    assert saved_settings.json()["overtime_limit"] == 5
    regenerated = client.post(f"/api/weeks/{week_id}/generate").json()
    assert regenerated["summary"]["scheduled_hours"] == 33
    monday = regenerated["employees"][0]["days"][0]
    assert monday["approved_overtime_hours"] == 5
    assert monday["overtime_is_manual"] is True

    reverted = client.put(
        f"/api/weeks/{week_id}/availability",
        json={
            "entries": [],
            "overtime_entries": [
                {
                    "employee_id": employee_id,
                    "work_date": "2026-07-20",
                    "hours": 0,
                    "manual": False,
                }
            ],
        },
    )
    monday = reverted.json()["employees"][0]["days"][0]
    assert monday["approved_overtime_hours"] == 0
    assert monday["overtime_is_manual"] is False
    regenerated = client.post(f"/api/weeks/{week_id}/generate").json()
    assert regenerated["summary"]["scheduled_hours"] == 30


def test_manual_daily_overtime_is_preserved_by_cross_week_generation(
    client: TestClient,
):
    part = client.post(
        "/api/parts",
        json={
            "code": "CROSS-OT",
            "name": "跨周人工加班件",
            "standard_hours": 1,
            "usage_types": ["accessory"],
            "active": True,
        },
    ).json()
    employee_id = create_employee(
        client, "跨周人工加班员工", "core", [part["id"]]
    )
    client.post(
        "/api/production-orders",
        json={
            "order_type": "accessory",
            "source_id": part["id"],
            "quantity": 33,
            "start_date": "2026-07-20",
            "end_date": "2026-07-24",
        },
    )
    week_id = client.post(
        "/api/production-orders/generate"
    ).json()["affected_week_ids"][0]
    assert client.put(
        f"/api/weeks/{week_id}/availability",
        json={
            "entries": [],
            "overtime_entries": [
                {
                    "employee_id": employee_id,
                    "work_date": "2026-07-20",
                    "hours": 4,
                    "manual": True,
                }
            ],
        },
    ).status_code == 200

    client.post("/api/production-orders/generate")
    detail = client.get(f"/api/weeks/{week_id}").json()
    assert detail["summary"]["scheduled_hours"] == 33
    monday = detail["employees"][0]["days"][0]
    assert monday["approved_overtime_hours"] == 4
    assert monday["overtime_is_manual"] is True


def test_employee_custom_overtime_value_is_ignored_in_favor_of_fixed_block(
    client: TestClient,
):
    part_id = create_part(client, code="FOLLOW-OT", hours=1)
    employee_id = create_employee(
        client, "跟随系统加班员工", "core", [part_id], overtime_limit=None
    )
    settings = client.get("/api/settings").json()
    settings["overtime_limit"] = 1
    saved = client.put("/api/settings", json=settings).json()
    assert saved["overtime_limit"] == saved["overtime_block_hours"] == 4
    week_id = create_week(client, part_id, 40)
    client.post(f"/api/weeks/{week_id}/generate")
    first = client.post(
        f"/api/weeks/{week_id}/resolve",
        json={"mode": "overtime", "employee_ids": [employee_id]},
    )
    assert first.status_code == 200
    assert first.json()["summary"]["remaining_hours"] == 0
    friday = next(
        day
        for day in first.json()["employees"][0]["days"]
        if day["date"] == "2026-07-24"
    )
    assert friday["approved_overtime_hours"] == 4

    employee = client.get("/api/employees").json()[0]
    employee["overtime_limit"] = 1
    updated = client.put(f"/api/employees/{employee_id}", json=employee)
    assert updated.status_code == 200
    assert updated.json()["overtime_limit"] is None


def test_employee_long_term_work_day_fields_are_ignored(client: TestClient):
    response = client.post(
        "/api/employees",
        json={
            "name": "错误班次员工",
            "employee_type": "core",
            "skill_part_ids": [],
            "weekly_work_days": 5,
            "unavailable_weekdays": [3, 4, 5, 6],
            "active": True,
        },
    )
    assert response.status_code == 201
    assert response.json()["weekly_work_days"] == 5
    assert response.json()["unavailable_weekdays"] == [5, 6]


def test_manual_assignment_validates_skill_and_approval(client: TestClient):
    part_id = create_part(client)
    other_part = create_part(client, code="P2")
    employee_id = create_employee(client, "固定一", "core", [part_id])
    week_id = create_week(client, part_id, 8)
    client.post(f"/api/weeks/{week_id}/generate")

    invalid = client.put(
        f"/api/weeks/{week_id}/assignments",
        json={
            "assignments": [
                {
                    "employee_id": employee_id,
                    "part_id": other_part,
                    "work_date": "2026-07-20",
                    "quantity": 1,
                }
            ]
        },
    )
    assert invalid.status_code == 422

    overloaded = client.put(
        f"/api/weeks/{week_id}/assignments",
        json={
            "assignments": [
                {
                    "employee_id": employee_id,
                    "part_id": part_id,
                    "work_date": "2026-07-20",
                    "quantity": 8,
                }
            ]
        },
    ).json()
    assert overloaded["summary"]["unapproved_overload"] is True
    approved = client.post(f"/api/weeks/{week_id}/approve-overtime").json()
    assert approved["summary"]["unapproved_overload"] is False
    assert approved["status"] == "ready"



def test_confirmed_week_keeps_inactive_employee_and_part_snapshot(client: TestClient):
    part_id = create_part(client, code="OLD", hours=1)
    employee_id = create_employee(client, "历史员工", "core", [part_id])
    week_id = create_week(client, part_id, 5)
    generated = client.post(f"/api/weeks/{week_id}/generate").json()
    assert generated["status"] == "ready"
    confirmed = client.post(f"/api/weeks/{week_id}/confirm")
    assert confirmed.status_code == 200

    client.delete(f"/api/employees/{employee_id}")
    client.put(
        f"/api/parts/{part_id}",
        json={
            "code": "NEW",
            "name": "改名零件",
            "standard_hours": 9,
            "active": True,
        },
    )
    history = client.get(f"/api/weeks/{week_id}").json()
    assert history["employees"][0]["name"] == "历史员工"
    assert history["demands"][0]["part_code"] == "OLD"
    assert history["demands"][0]["standard_hours"] == 1
    assert history["status"] == "confirmed"


def test_unconfirm_preserves_schedule_and_reset_clears_only_schedule(client: TestClient):
    part_id = create_part(client)
    create_employee(client, "固定一", "core", [part_id])
    backup_id = create_employee(client, "候补一", "backup", [part_id])
    week_id = create_week(client, part_id, 60)
    client.post(f"/api/weeks/{week_id}/generate")
    resolved = client.post(
        f"/api/weeks/{week_id}/resolve",
        json={"mode": "reinforcement", "employee_ids": [backup_id]},
    ).json()
    assert resolved["status"] == "ready"
    assignment_count = len(resolved["assignments"])

    confirmed = client.post(f"/api/weeks/{week_id}/confirm").json()
    assert confirmed["status"] == "confirmed"
    assert client.post(f"/api/weeks/{week_id}/reset").status_code == 409

    unlocked = client.post(f"/api/weeks/{week_id}/unconfirm").json()
    assert unlocked["status"] == "ready"
    assert unlocked["confirmed_at"] is None
    assert len(unlocked["assignments"]) == assignment_count
    assert backup_id in {employee["id"] for employee in unlocked["employees"]}

    reset = client.post(f"/api/weeks/{week_id}/reset").json()
    assert reset["status"] == "draft"
    assert reset["assignments"] == []
    assert reset["demands"][0]["quantity"] == 60
    assert reset["summary"]["has_schedule_data"] is False
    assert backup_id not in {employee["id"] for employee in reset["employees"]}


def test_permanent_employee_delete_only_when_never_used(client: TestClient):
    part_id = create_part(client)
    unused_id = create_employee(client, "未使用员工", "backup", [part_id])
    deleted = client.delete(f"/api/employees/{unused_id}/permanent")
    assert deleted.status_code == 204
    assert unused_id not in {item["id"] for item in client.get("/api/employees").json()}

    used_id = create_employee(client, "已排班员工", "core", [part_id])
    week_id = create_week(client, part_id, 1)
    client.post(f"/api/weeks/{week_id}/generate")
    blocked = client.delete(f"/api/employees/{used_id}/permanent")
    assert blocked.status_code == 409
    assert "只能停用" in blocked.json()["detail"]


def test_delete_all_employees_is_atomic_and_protects_schedule_history(
    client: TestClient,
):
    part_id = create_part(client, code="EMP-BULK")
    create_employee(client, "批量员工一", "backup", [part_id])
    create_employee(client, "批量员工二", "backup", [])

    deleted = client.delete("/api/employees/all/permanent")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] == 2
    assert client.get("/api/employees").json() == []

    protected_id = create_employee(client, "历史员工", "core", [part_id])
    unused_id = create_employee(client, "未使用员工", "backup", [])
    week_id = create_week(client, part_id, 1)
    client.post(f"/api/weeks/{week_id}/generate")

    blocked = client.delete("/api/employees/all/permanent")
    assert blocked.status_code == 409
    assert "本次未删除任何员工" in blocked.json()["detail"]
    remaining = {item["id"] for item in client.get("/api/employees").json()}
    assert remaining == {protected_id, unused_id}


def test_permanent_part_delete_removes_skills_but_protects_week_history(
    client: TestClient,
):
    unused_part_id = create_part(client, code="UNUSED")
    employee_id = create_employee(client, "技能员工", "backup", [unused_part_id])
    deleted = client.delete(f"/api/parts/{unused_part_id}/permanent")
    assert deleted.status_code == 204
    assert unused_part_id not in {item["id"] for item in client.get("/api/parts").json()}
    employees = {item["id"]: item for item in client.get("/api/employees").json()}
    assert employees[employee_id]["skill_part_ids"] == []

    used_part_id = create_part(client, code="USED")
    create_week(client, used_part_id, 1)
    blocked = client.delete(f"/api/parts/{used_part_id}/permanent")
    assert blocked.status_code == 409
    assert "只能停用" in blocked.json()["detail"]


def test_delete_all_parts_is_atomic_and_removes_employee_skills(
    client: TestClient,
):
    first_part_id = create_part(client, code="PART-BULK-1")
    create_part(client, code="PART-BULK-2")
    employee_id = create_employee(client, "批量技能员工", "backup", [first_part_id])

    deleted = client.delete("/api/parts/all/permanent")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] == 2
    assert client.get("/api/parts").json() == []
    employees = {item["id"]: item for item in client.get("/api/employees").json()}
    assert employees[employee_id]["skill_part_ids"] == []

    protected_id = create_part(client, code="PART-HISTORY")
    unused_id = create_part(client, code="PART-UNUSED")
    create_week(client, protected_id, 1)

    blocked = client.delete("/api/parts/all/permanent")
    assert blocked.status_code == 409
    assert "本次未删除任何零件" in blocked.json()["detail"]
    remaining = {item["id"] for item in client.get("/api/parts").json()}
    assert remaining == {protected_id, unused_id}


def test_csv_part_import_previews_and_upserts_atomically(client: TestClient):
    existing_id = create_part(client, code="P-001", hours=1)
    existing_employee_id = create_employee(client, "张三", "core", [])
    content = (
        "零件编号,零件名称,单件标准工时（小时）,启用状态,零件用途,员工\n"
        "P-001,更新零件,1.5,启用,双用途,张三、李四\n"
        "P-002,新增零件,0.25,停用,整机装配,\n"
    ).encode("utf-8-sig")
    response = client.post(
        "/api/parts/import/preview",
        files={"file": ("parts.csv", content, "text/csv")},
    )
    assert response.status_code == 200
    preview = response.json()
    assert preview["create_count"] == 1
    assert preview["update_count"] == 1
    assert preview["invalid_count"] == 0
    assert preview["new_employee_count"] == 1
    assert preview["new_employee_names"] == ["李四"]
    assert preview["rows"][0]["employee_names"] == ["张三", "李四"]
    assert preview["rows"][1]["employee_names"] == []

    committed = client.post(
        "/api/parts/import/commit",
        json={
            "rows": [
                {
                    "code": row["code"],
                    "name": row["name"],
                    "standard_hours": row["standard_hours"],
                    "active": row["active"],
                    "usage_types": row["usage_types"],
                    "employee_names": row["employee_names"],
                }
                for row in preview["rows"]
            ]
        },
    )
    assert committed.status_code == 200
    assert committed.json() == {
        "created": 1,
        "updated": 1,
        "total": 2,
        "employees_created": 1,
        "skills_updated": 2,
    }
    parts = {part["code"]: part for part in client.get("/api/parts").json()}
    assert parts["P-001"]["id"] == existing_id
    assert parts["P-001"]["standard_hours"] == 1.5
    assert parts["P-002"]["active"] is False
    assert parts["P-001"]["usage_types"] == ["accessory", "assembly"]
    assert parts["P-002"]["usage_types"] == ["assembly"]
    assert parts["P-001"]["level_1_employee"]["employee_name"] == "张三"
    assert parts["P-001"]["level_2_employee"]["employee_name"] == "李四"
    employees = {item["name"]: item for item in client.get("/api/employees").json()}
    assert employees["张三"]["id"] == existing_employee_id
    assert employees["张三"]["skill_part_ids"] == [existing_id]
    assert employees["李四"]["skill_part_ids"] == [existing_id]
    client.post(
        "/api/parts/import/commit",
        json={
            "rows": [
                {
                    "code": "P-002",
                    "name": "再次更新",
                    "standard_hours": 0.5,
                }
            ]
        },
    )
    parts = {part["code"]: part for part in client.get("/api/parts").json()}
    assert parts["P-002"]["active"] is False
    assert parts["P-002"]["usage_types"] == ["assembly"]


def test_permanent_production_order_delete_cleans_draft_but_protects_confirmed(
    client: TestClient,
):
    part_id = create_part(client, code="DELETE-ORDER", hours=0.5)
    create_employee(client, "任务删除员工", "core", [part_id])

    draft_order = client.post(
        "/api/production-orders",
        json={
            "order_type": "accessory",
            "source_id": part_id,
            "quantity": 4,
            "start_date": "2026-07-20",
            "end_date": "2026-07-24",
        },
    ).json()
    week_id = client.post("/api/production-orders/generate").json()["affected_week_ids"][0]
    deleted = client.delete(f"/api/production-orders/{draft_order['id']}/permanent")
    assert deleted.status_code == 204
    assert client.get("/api/production-orders").json() == []
    draft_week = client.get(f"/api/weeks/{week_id}").json()
    assert draft_week["assignments"] == []
    assert draft_week["demands"] == []

    confirmed_order = client.post(
        "/api/production-orders",
        json={
            "order_type": "accessory",
            "source_id": part_id,
            "quantity": 2,
            "start_date": "2026-07-20",
            "end_date": "2026-07-24",
        },
    ).json()
    client.post("/api/production-orders/generate")
    assert client.post(f"/api/weeks/{week_id}/confirm").status_code == 200
    blocked = client.delete(f"/api/production-orders/{confirmed_order['id']}/permanent")
    assert blocked.status_code == 409
    assert "取消确认" in blocked.json()["detail"]


def test_part_import_rejects_duplicate_rows_and_serves_template(client: TestClient):
    content = (
        "零件编号,零件名称,单件标准工时（小时）\n"
        "DUP,零件一,1\n"
        "DUP,零件二,2\n"
    ).encode()
    preview = client.post(
        "/api/parts/import/preview",
        files={"file": ("parts.csv", content, "text/csv")},
    ).json()
    assert preview["invalid_count"] == 2
    assert all("重复" in row["errors"][-1] for row in preview["rows"])

    template = client.get("/api/parts/import/template")
    assert template.status_code == 200
    assert template.content.startswith(b"PK")
    saved = client.post("/api/parts/import/template/save")
    assert saved.status_code == 200
    saved_path = Path(saved.json()["path"])
    assert saved_path.name == "零件导入模板.xlsx"
    assert saved_path.read_bytes() == template.content
    saved_again = client.post("/api/parts/import/template/save")
    assert Path(saved_again.json()["path"]).name == "零件导入模板 (1).xlsx"
    empty_template = client.post(
        "/api/parts/import/preview",
        files={
            "file": (
                "零件导入模板.xlsx",
                template.content,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert empty_template.status_code == 422
    assert "没有零件数据" in empty_template.json()["detail"]


def test_part_import_supports_three_employee_levels_and_reports_conflicts(
    client: TestClient,
):
    valid = client.post(
        "/api/parts/import/preview",
        files={
            "file": (
                "priority.csv",
                (
                    "零件编号,零件名称,单件标准工时（小时）,员工1,员工2,员工3\n"
                    "PRI-IMPORT,优先导入件,0.5,主员工,次员工,三级员工\n"
                ).encode(),
                "text/csv",
            )
        },
    ).json()
    assert valid["invalid_count"] == 0
    assert valid["rows"][0]["employee_level1_names"] == ["主员工"]
    assert valid["rows"][0]["employee_level2_names"] == ["次员工"]
    assert valid["rows"][0]["employee_level3_names"] == ["三级员工"]

    conflict = client.post(
        "/api/parts/import/preview",
        files={
            "file": (
                "priority-error.csv",
                (
                    "零件编号,零件名称,单件标准工时（小时）,员工1,员工2,员工3\n"
                    "PRI-ERROR,错误优先件,0.5,同一员工,次员工,同一员工\n"
                ).encode(),
                "text/csv",
            )
        },
    ).json()
    assert conflict["invalid_count"] == 1
    assert "不能同时设置" in conflict["rows"][0]["errors"][0]


def test_part_import_returns_clear_format_errors(client: TestClient):
    missing_header = (
        "编号,名称\n"
        "P-001,测试零件\n"
    ).encode("utf-8-sig")
    response = client.post(
        "/api/parts/import/preview",
        files={"file": ("错误表头.csv", missing_header, "text/csv")},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "表头缺少：单件标准工时（小时）"

    invalid_rows = (
        "零件编号,零件名称,单件标准工时（小时）,启用状态\n"
        "P-001,测试零件,不是数字,未知状态\n"
    ).encode("utf-8-sig")
    preview = client.post(
        "/api/parts/import/preview",
        files={"file": ("错误内容.csv", invalid_rows, "text/csv")},
    )
    assert preview.status_code == 200
    row = preview.json()["rows"][0]
    assert row["row_number"] == 2
    assert "单件标准工时必须大于0且不超过1000小时" in row["errors"]
    assert any("启用状态应填写" in error for error in row["errors"])


def test_confirmed_week_exports_pdf_and_png_to_downloads(client: TestClient):
    part_id = create_part(client, code="EXPORT", hours=0.5)
    create_employee(client, "导出员工", "core", [part_id])
    week_id = create_week(client, part_id, 4)

    blocked = client.post(
        f"/api/weeks/{week_id}/export",
        json={"format": "pdf"},
    )
    assert blocked.status_code == 409
    assert "确认后" in blocked.json()["detail"]

    assert client.post(f"/api/weeks/{week_id}/generate").status_code == 200
    assert client.post(f"/api/weeks/{week_id}/confirm").status_code == 200

    pdf = client.post(
        f"/api/weeks/{week_id}/export",
        json={"format": "pdf"},
    )
    assert pdf.status_code == 200
    pdf_result = pdf.json()
    pdf_path = Path(pdf_result["path"])
    assert pdf_path.name == "周排班明细_2026-07-20.pdf"
    assert pdf_path.read_bytes().startswith(b"%PDF")
    assert pdf_result["page_count"] >= 3
    assert pdf_path.stat().st_size > 100_000

    png = client.post(
        f"/api/weeks/{week_id}/export",
        json={"format": "png"},
    )
    assert png.status_code == 200
    png_result = png.json()
    png_path = Path(png_result["path"])
    assert png_path.name == "周排班明细_2026-07-20.png"
    assert png_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert png_result["page_count"] == pdf_result["page_count"]
    assert png_path.stat().st_size > 100_000


def test_machine_bom_snapshot_and_balanced_cross_week_schedule(client: TestClient):
    part = client.post(
        "/api/parts",
        json={
            "code": "DUAL-001",
            "name": "双用途装配件",
            "standard_hours": 1,
            "usage_types": ["accessory", "assembly"],
            "active": True,
        },
    )
    assert part.status_code == 201
    part_id = part.json()["id"]
    assert part.json()["usage_types"] == ["accessory", "assembly"]
    employee_id = create_employee(client, "跨周装配员工", "core", [part_id])

    machine = client.post(
        "/api/machines",
        json={
            "code": "M-001",
            "name": "测试整机",
            "active": True,
            "bom_items": [{"part_id": part_id, "quantity_per_machine": 2}],
        },
    )
    assert machine.status_code == 201
    machine_id = machine.json()["id"]
    order = client.post(
        "/api/production-orders",
        json={
            "order_type": "machine",
            "source_id": machine_id,
            "quantity": 5,
            "start_date": "2026-07-20",
            "end_date": "2026-07-24",
        },
    )
    assert order.status_code == 201
    order_id = order.json()["id"]
    assert order.json()["items"][0]["required_quantity"] == 10

    # 修改主数据BOM不追溯改变已经建立的任务快照。
    updated = client.put(
        f"/api/machines/{machine_id}",
        json={
            "code": "M-001",
            "name": "测试整机",
            "active": True,
            "bom_items": [{"part_id": part_id, "quantity_per_machine": 3}],
        },
    )
    assert updated.status_code == 200
    assert client.get("/api/production-orders").json()[0]["items"][0]["required_quantity"] == 10

    generated = client.post("/api/production-orders/generate")
    assert generated.status_code == 200
    assert len(generated.json()["affected_week_ids"]) == 1
    week_id = generated.json()["affected_week_ids"][0]
    detail = client.get(f"/api/weeks/{week_id}").json()
    by_day = {
        day: sum(item["quantity"] for item in detail["assignments"] if item["work_date"] == day)
        for day in detail["days"]
    }
    assert [by_day[day] for day in detail["days"]] == [2, 2, 2, 2, 2, 0, 0]
    assert {item["employee_id"] for item in detail["assignments"]} == {employee_id}
    assert all(item["order_type"] == "machine" for item in detail["assignments"])
    assert detail["demands"][0]["sources"][0]["production_order_id"] == order_id

    accessory = client.post(
        "/api/production-orders",
        json={
            "order_type": "accessory",
            "source_id": part_id,
            "quantity": 2,
            "start_date": "2026-07-20",
            "end_date": "2026-07-24",
        },
    )
    assert accessory.status_code == 201
    client.post("/api/production-orders/generate")
    mixed = client.get(f"/api/weeks/{week_id}").json()
    assert {item["order_type"] for item in mixed["assignments"]} == {
        "machine", "accessory"
    }
    assert {
        item["employee_id"]
        for item in mixed["assignments"]
        if item["order_type"] == "accessory"
    } == {employee_id}
    assert mixed["summary"]["remaining_hours"] == 0
    assert len(mixed["demands"][0]["sources"]) == 2


def test_accessory_fills_earliest_capacity_and_confirmed_week_is_locked(client: TestClient):
    part = client.post(
        "/api/parts",
        json={
            "code": "ORDER-001",
            "name": "跨周附件",
            "standard_hours": 1,
            "usage_types": ["accessory"],
            "active": True,
        },
    ).json()
    create_employee(client, "附件订单员工", "core", [part["id"]])
    first = client.post(
        "/api/production-orders",
        json={
            "order_type": "accessory",
            "source_id": part["id"],
            "quantity": 35,
            "start_date": "2026-07-20",
            "end_date": "2026-07-31",
        },
    )
    assert first.status_code == 201
    result = client.post("/api/production-orders/generate").json()
    assert len(result["affected_week_ids"]) == 2
    first_week = client.get(f"/api/weeks/{result['affected_week_ids'][0]}").json()
    second_week = client.get(f"/api/weeks/{result['affected_week_ids'][1]}").json()
    assert sum(item["quantity"] for item in first_week["assignments"]) == 30
    assert [
        day["assigned_hours"] for day in first_week["employees"][0]["days"]
    ] == [6, 6, 6, 6, 6, 0, 0]
    assert sum(item["quantity"] for item in second_week["assignments"]) == 5
    assert {item["work_date"] for item in second_week["assignments"]} == {"2026-07-27"}

    assert client.post(f"/api/weeks/{first_week['id']}/confirm").status_code == 200
    locked = client.get(f"/api/weeks/{first_week['id']}").json()["assignments"]
    new_order = client.post(
        "/api/production-orders",
        json={
            "order_type": "accessory",
            "source_id": part["id"],
            "quantity": 2,
            "start_date": "2026-07-20",
            "end_date": "2026-07-31",
        },
    ).json()
    assert new_order["confirmed_conflicts"][0]["week_id"] == first_week["id"]
    stale = client.post(f"/api/weeks/{second_week['id']}/confirm")
    assert stale.status_code == 409
    assert "一键重新生成" in stale.json()["detail"]
    client.post("/api/production-orders/generate")
    assert client.get(f"/api/weeks/{first_week['id']}").json()["assignments"] == locked
    assert client.delete(f"/api/production-orders/{new_order['id']}").status_code == 204
    client.post("/api/production-orders/generate")
    cleaned = client.get(f"/api/weeks/{second_week['id']}").json()
    assert sum(item["quantity"] for item in cleaned["assignments"]) == 5


def test_cross_week_reinforcement_only_takes_accessory_remainder(
    client: TestClient,
):
    part_id = create_part(client, code="ORDER-REINFORCE", hours=1)
    core_id = create_employee(client, "跨周固定员工", "core", [part_id])
    backup_id = create_employee(client, "跨周候补员工", "backup", [part_id])
    client.post(
        "/api/production-orders",
        json={
            "order_type": "accessory",
            "source_id": part_id,
            "quantity": 35,
            "start_date": "2026-07-20",
            "end_date": "2026-07-24",
        },
    )
    week_id = client.post(
        "/api/production-orders/generate"
    ).json()["affected_week_ids"][0]
    before = client.get(f"/api/weeks/{week_id}").json()
    core_before = {
        item["work_date"]: item["quantity"]
        for item in before["assignments"]
        if item["employee_id"] == core_id
    }

    resolved = client.post(
        f"/api/weeks/{week_id}/resolve",
        json={"mode": "reinforcement", "employee_ids": [backup_id]},
    ).json()
    core_after = {
        item["work_date"]: item["quantity"]
        for item in resolved["assignments"]
        if item["employee_id"] == core_id
    }
    backup_assignments = [
        item
        for item in resolved["assignments"]
        if item["employee_id"] == backup_id
    ]

    assert core_after == core_before
    assert sum(item["quantity"] for item in backup_assignments) == 5
    assert {item["work_date"] for item in backup_assignments} == {"2026-07-20"}
    assert all(
        item["efficiency"] == 0.8
        for item in resolved["daily_efficiency"]
        if item["available_hours"] > 0
    )


def test_accessory_uses_employee1_capacity_before_employee2(
    client: TestClient,
):
    part_id = create_part(client, code="ACCESSORY-BALANCE", hours=1)
    first_id = create_employee(client, "附件均分一", "core", [part_id])
    second_id = create_employee(client, "附件均分二", "core", [part_id])
    client.post(
        "/api/production-orders",
        json={
            "order_type": "accessory",
            "source_id": part_id,
            "quantity": 10,
            "start_date": "2026-07-20",
            "end_date": "2026-07-20",
        },
    )

    week_id = client.post(
        "/api/production-orders/generate"
    ).json()["affected_week_ids"][0]
    detail = client.get(f"/api/weeks/{week_id}").json()
    quantities = {
        employee_id: sum(
            item["quantity"]
            for item in detail["assignments"]
            if item["employee_id"] == employee_id
        )
        for employee_id in (first_id, second_id)
    }
    assert quantities == {first_id: 6, second_id: 4}
    assert {item["work_date"] for item in detail["assignments"]} == {"2026-07-20"}


def test_accessory_stays_with_employee1_when_period_capacity_is_sufficient(
    client: TestClient,
):
    part_id = create_part(client, code="ACCESSORY-EMPLOYEE1", hours=1)
    first_id = create_employee(client, "附件员工1", "core", [part_id])
    second_id = create_employee(client, "附件员工2", "core", [part_id])
    client.post(
        "/api/production-orders",
        json={
            "order_type": "accessory",
            "source_id": part_id,
            "quantity": 10,
            "start_date": "2026-07-20",
            "end_date": "2026-07-21",
        },
    )

    week_id = client.post(
        "/api/production-orders/generate"
    ).json()["affected_week_ids"][0]
    detail = client.get(f"/api/weeks/{week_id}").json()
    quantities = {
        employee_id: sum(
            item["quantity"]
            for item in detail["assignments"]
            if item["employee_id"] == employee_id
        )
        for employee_id in (first_id, second_id)
    }
    assert quantities == {first_id: 10, second_id: 0}
    assert {
        item["work_date"]
        for item in detail["assignments"]
        if item["employee_id"] == first_id
    } == {"2026-07-20", "2026-07-21"}


def test_accessory_uses_employee1_remaining_capacity_before_employee2(
    client: TestClient,
):
    accessory = client.post(
        "/api/parts",
        json={
            "code": "ACCESSORY-LOW-LOAD",
            "name": "负荷优先附件",
            "standard_hours": 1,
            "usage_types": ["accessory"],
            "active": True,
        },
    ).json()
    assembly = client.post(
        "/api/parts",
        json={
            "code": "ASSEMBLY-LOAD",
            "name": "已有负荷装配件",
            "standard_hours": 1,
            "usage_types": ["assembly"],
            "active": True,
        },
    ).json()
    busy_id = create_employee(
        client,
        "已有任务员工",
        "core",
        [accessory["id"], assembly["id"]],
    )
    idle_id = create_employee(client, "空闲员工", "core", [accessory["id"]])
    machine = client.post(
        "/api/machines",
        json={
            "code": "LOAD-MACHINE",
            "name": "负荷测试整机",
            "active": True,
            "bom_items": [
                {"part_id": assembly["id"], "quantity_per_machine": 1}
            ],
        },
    ).json()
    client.post(
        "/api/production-orders",
        json={
            "order_type": "machine",
            "source_id": machine["id"],
            "quantity": 4,
            "start_date": "2026-07-20",
            "end_date": "2026-07-20",
        },
    )
    client.post(
        "/api/production-orders",
        json={
            "order_type": "accessory",
            "source_id": accessory["id"],
            "quantity": 6,
            "start_date": "2026-07-20",
            "end_date": "2026-07-20",
        },
    )

    week_id = client.post(
        "/api/production-orders/generate"
    ).json()["affected_week_ids"][0]
    detail = client.get(f"/api/weeks/{week_id}").json()
    accessory_quantities = {
        employee_id: sum(
            item["quantity"]
            for item in detail["assignments"]
            if item["employee_id"] == employee_id
            and item["order_type"] == "accessory"
        )
        for employee_id in (busy_id, idle_id)
    }
    total_hours = {
        employee["id"]: employee["week_assigned_hours"]
        for employee in detail["employees"]
    }
    assert accessory_quantities == {busy_id: 2, idle_id: 4}
    assert total_hours == {busy_id: 6, idle_id: 4}


def test_machine_remainder_is_spread_and_order_hour_snapshots_stay_exact(client: TestClient):
    part = client.post(
        "/api/parts",
        json={
            "code": "SNAP-001",
            "name": "快照双用途件",
            "standard_hours": 1,
            "usage_types": ["accessory", "assembly"],
            "active": True,
        },
    ).json()
    create_employee(client, "快照员工", "core", [part["id"]])
    machine = client.post(
        "/api/machines",
        json={
            "code": "M-SPREAD",
            "name": "余数均分整机",
            "active": True,
            "bom_items": [{"part_id": part["id"], "quantity_per_machine": 1}],
        },
    ).json()
    client.post(
        "/api/production-orders",
        json={
            "order_type": "machine",
            "source_id": machine["id"],
            "quantity": 7,
            "start_date": "2026-07-20",
            "end_date": "2026-07-24",
        },
    )
    result = client.post("/api/production-orders/generate").json()
    week_id = result["affected_week_ids"][0]
    detail = client.get(f"/api/weeks/{week_id}").json()
    by_day = [
        sum(
            item["quantity"]
            for item in detail["assignments"]
            if item["work_date"] == day
        )
        for day in detail["days"]
    ]
    assert by_day == [1, 2, 1, 2, 1, 0, 0]

    # 新任务读取修改后的主数据工时，旧任务继续保留建立时的1小时快照。
    client.put(
        f"/api/parts/{part['id']}",
        json={
            "code": "SNAP-001",
            "name": "快照双用途件",
            "standard_hours": 2,
            "usage_types": ["accessory", "assembly"],
            "active": True,
        },
    )
    client.post(
        "/api/production-orders",
        json={
            "order_type": "accessory",
            "source_id": part["id"],
            "quantity": 1,
            "start_date": "2026-07-20",
            "end_date": "2026-07-24",
        },
    )
    client.post("/api/production-orders/generate")
    detail = client.get(f"/api/weeks/{week_id}").json()
    source_hours = sorted(
        source["standard_hours"] for source in detail["demands"][0]["sources"]
    )
    assert source_hours == [1, 2]
    assert detail["summary"]["total_required_hours"] == 9
    assert detail["summary"]["scheduled_hours"] == 9
    assert detail["summary"]["remaining_hours"] == 0


def test_machine_uses_employee2_only_after_admin_authorizes_alternate(
    client: TestClient,
):
    part = client.post(
        "/api/parts",
        json={
            "code": "PRIORITY-MACHINE",
            "name": "优先级整机件",
            "standard_hours": 1,
            "usage_types": ["assembly"],
            "active": True,
        },
    ).json()
    employee1 = create_employee(client, "优先员工1", "core", [part["id"]])
    employee2 = create_employee(client, "优先员工2", "core", [part["id"]])
    updated = client.put(
        f"/api/parts/{part['id']}",
        json={
            "code": part["code"],
            "name": part["name"],
            "standard_hours": 1,
            "usage_types": ["assembly"],
            "level_1_employee_id": employee1,
            "level_2_employee_id": employee2,
            "active": True,
        },
    )
    assert updated.status_code == 200
    machine = client.post(
        "/api/machines",
        json={
            "code": "PRIORITY-M",
            "name": "优先整机",
            "active": True,
            "bom_items": [{"part_id": part["id"], "quantity_per_machine": 1}],
        },
    ).json()
    client.post(
        "/api/production-orders",
        json={
            "order_type": "machine",
            "source_id": machine["id"],
            "quantity": 40,
            "start_date": "2026-07-20",
            "end_date": "2026-07-24",
        },
    )
    week_id = client.post(
        "/api/production-orders/generate"
    ).json()["affected_week_ids"][0]
    detail = client.get(f"/api/weeks/{week_id}").json()
    initial_quantities = {
        employee_id: sum(
            assignment["quantity"]
            for assignment in detail["assignments"]
            if assignment["employee_id"] == employee_id
        )
        for employee_id in (employee1, employee2)
    }
    assert initial_quantities == {employee1: 30, employee2: 0}
    assert detail["summary"]["remaining_hours"] == 10
    assert detail["machine_resolution"] == {
        "allow_alternates": False,
        "allow_advance": False,
    }

    detail = client.post(
        f"/api/weeks/{week_id}/resolve",
        json={"mode": "alternate", "employee_ids": []},
    ).json()
    quantities = {
        employee_id: sum(
            assignment["quantity"]
            for assignment in detail["assignments"]
            if assignment["employee_id"] == employee_id
        )
        for employee_id in (employee1, employee2)
    }
    assert quantities == {employee1: 30, employee2: 10}
    assert detail["machine_resolution"]["allow_alternates"] is True
    assert [
        sum(
            assignment["quantity"]
            for assignment in detail["assignments"]
            if assignment["work_date"] == day
        )
        for day in detail["days"]
    ] == [8, 8, 8, 8, 8, 0, 0]


def test_dual_usage_accessory_uses_employee1_remainder_then_employee2(
    client: TestClient,
):
    part = client.post(
        "/api/parts",
        json={
            "code": "DUAL-PRIORITY",
            "name": "双用途优先件",
            "standard_hours": 1,
            "usage_types": ["accessory", "assembly"],
            "active": True,
        },
    ).json()
    employee1 = create_employee(client, "双用途员工1", "core", [part["id"]])
    employee2 = create_employee(client, "双用途员工2", "core", [part["id"]])
    client.put(
        f"/api/parts/{part['id']}",
        json={
            "code": part["code"],
            "name": part["name"],
            "standard_hours": 1,
            "usage_types": ["accessory", "assembly"],
            "level_1_employee_id": employee1,
            "level_2_employee_id": employee2,
            "active": True,
        },
    )
    machine = client.post(
        "/api/machines",
        json={
            "code": "DUAL-PRIORITY-M",
            "name": "双用途优先整机",
            "active": True,
            "bom_items": [{"part_id": part["id"], "quantity_per_machine": 1}],
        },
    ).json()
    client.post(
        "/api/production-orders",
        json={
            "order_type": "machine",
            "source_id": machine["id"],
            "quantity": 5,
            "start_date": "2026-07-20",
            "end_date": "2026-07-20",
        },
    )
    client.post(
        "/api/production-orders",
        json={
            "order_type": "accessory",
            "source_id": part["id"],
            "quantity": 4,
            "start_date": "2026-07-20",
            "end_date": "2026-07-20",
        },
    )
    week_id = client.post(
        "/api/production-orders/generate"
    ).json()["affected_week_ids"][0]
    detail = client.get(f"/api/weeks/{week_id}").json()
    quantities = {
        (order_type, employee_id): sum(
            assignment["quantity"]
            for assignment in detail["assignments"]
            if assignment["order_type"] == order_type
            and assignment["employee_id"] == employee_id
        )
        for order_type in ("machine", "accessory")
        for employee_id in (employee1, employee2)
    }
    assert quantities == {
        ("machine", employee1): 5,
        ("machine", employee2): 0,
        ("accessory", employee1): 1,
        ("accessory", employee2): 3,
    }


def test_fixed_four_hour_overtime_uses_latest_dates_first(client: TestClient):
    part_id = create_part(client, code="BLOCK-OT", hours=1)
    employee_id = create_employee(client, "四小时倒排员工", "core", [part_id])
    client.post(
        "/api/production-orders",
        json={
            "order_type": "accessory",
            "source_id": part_id,
            "quantity": 33,
            "start_date": "2026-07-20",
            "end_date": "2026-07-24",
        },
    )
    week_id = client.post(
        "/api/production-orders/generate"
    ).json()["affected_week_ids"][0]
    before = client.get(f"/api/weeks/{week_id}").json()
    assert before["summary"]["remaining_hours"] == 3
    resolved = client.post(
        f"/api/weeks/{week_id}/resolve",
        json={"mode": "overtime", "employee_ids": [employee_id]},
    ).json()
    assert resolved["summary"]["remaining_hours"] == 0
    friday = next(
        day
        for day in resolved["employees"][0]["days"]
        if day["date"] == "2026-07-24"
    )
    assert friday["approved_overtime_hours"] == 4
    assert friday["assigned_hours"] == 9
    assert all(
        day["approved_overtime_hours"] == 0
        for day in resolved["employees"][0]["days"]
        if day["date"] != "2026-07-24"
    )


def test_accessory_order_csv_import_preview_and_commit(client: TestClient):
    create_part(client, code="IMPORT-ORDER", hours=0.5)
    preview = client.post(
        "/api/production-orders/import/preview",
        files={
            "file": (
                "附件订单.csv",
                "零件编号,数量,开始日期,截止日期\nIMPORT-ORDER,12,2026/07/20,2026/07/24\n".encode(),
                "text/csv",
            )
        },
    )
    assert preview.status_code == 200
    row = preview.json()["rows"][0]
    assert row["start_date"] == "2026-07-20"
    committed = client.post(
        "/api/production-orders/import/commit",
        json={
            "rows": [
                {
                    "part_code": row["part_code"],
                    "quantity": row["quantity"],
                    "start_date": row["start_date"],
                    "end_date": row["end_date"],
                }
            ]
        },
    )
    assert committed.status_code == 200
    assert committed.json()["created"] == 1
    assert client.get("/api/production-orders").json()[0]["quantity"] == 12


def test_confirmed_leave_adjustment_locks_unaffected_tasks_and_can_restore(
    client: TestClient,
):
    first_part = create_part(client, code="LEAVE-A", hours=1)
    second_part = create_part(client, code="LEAVE-B", hours=1)
    first_employee = create_employee(client, "请假员工", "core", [first_part])
    second_employee = create_employee(
        client, "不受影响员工", "core", [first_part, second_part]
    )
    for part_id in (first_part, second_part):
        client.post(
            "/api/production-orders",
            json={
                "order_type": "accessory",
                "source_id": part_id,
                "quantity": 2,
                "start_date": "2026-07-20",
                "end_date": "2026-07-20",
            },
        )
    week_id = client.post(
        "/api/production-orders/generate"
    ).json()["affected_week_ids"][0]
    assert client.post(f"/api/weeks/{week_id}/confirm").status_code == 200
    original = client.get(f"/api/weeks/{week_id}").json()
    unaffected = next(
        item
        for item in original["assignments"]
        if item["employee_id"] == second_employee
    )
    adjusted = client.post(
        f"/api/weeks/{week_id}/leave-adjustments",
        json={
            "employee_id": first_employee,
            "leave_dates": ["2026-07-20"],
            "use_overtime": True,
            "use_weekend": True,
        },
    )
    assert adjusted.status_code == 200
    detail = adjusted.json()
    assert detail["active_adjustment"] is not None
    assert detail["active_adjustment"]["employee_id"] == first_employee
    assert detail["active_adjustment"]["leave_dates"] == ["2026-07-20"]
    assert detail["summary"]["remaining_hours"] == 0
    locked = next(
        item
        for item in detail["assignments"]
        if item["id"] == unaffected["id"]
    )
    assert locked["id"] == unaffected["id"]
    assert locked["source"] == "manual"
    recovered = [
        item
        for item in detail["assignments"]
        if item["employee_id"] == first_employee
    ]
    assert recovered
    assert all(item["work_date"] != "2026-07-20" for item in recovered)
    # 即使其他员工现在也具备该零件技能，请假任务仍只交还本人补班。
    assert sum(
        item["quantity"]
        for item in detail["assignments"]
        if item["employee_id"] == second_employee
        and item["part_id"] == first_part
    ) == sum(
        item["quantity"]
        for item in original["assignments"]
        if item["employee_id"] == second_employee
        and item["part_id"] == first_part
    )

    restored = client.post(
        f"/api/weeks/{week_id}/leave-adjustments/cancel"
    ).json()
    assert restored["status"] == "confirmed"
    assert restored["active_adjustment"] is None
    assert {
        (item["id"], item["source"]) for item in restored["assignments"]
    } == {
        (item["id"], item["source"]) for item in original["assignments"]
    }


def test_confirmed_leave_merges_into_existing_same_order_task(
    client: TestClient,
):
    part = client.post(
        "/api/parts",
        json={
            "code": "LEAVE-MERGE",
            "name": "同订单补班零件",
            "standard_hours": 1,
            "usage_types": ["assembly"],
            "active": True,
        },
    )
    assert part.status_code == 201
    part_id = part.json()["id"]
    employee_id = create_employee(
        client, "同订单补班员工", "core", [part_id]
    )
    machine = client.post(
        "/api/machines",
        json={
            "code": "LEAVE-MACHINE",
            "name": "请假合并测试整机",
            "active": True,
            "bom_items": [
                {"part_id": part_id, "quantity_per_machine": 1}
            ],
        },
    )
    assert machine.status_code == 201
    order = client.post(
        "/api/production-orders",
        json={
            "order_type": "machine",
            "source_id": machine.json()["id"],
            "quantity": 5,
            "start_date": "2026-07-20",
            "end_date": "2026-07-24",
        },
    )
    assert order.status_code == 201
    week_id = client.post(
        "/api/production-orders/generate"
    ).json()["affected_week_ids"][0]
    original = client.get(f"/api/weeks/{week_id}").json()
    assert [
        sum(
            assignment["quantity"]
            for assignment in original["assignments"]
            if assignment["work_date"] == day
        )
        for day in original["days"]
    ] == [1, 1, 1, 1, 1, 0, 0]
    assert client.post(f"/api/weeks/{week_id}/confirm").status_code == 200

    adjusted = client.post(
        f"/api/weeks/{week_id}/leave-adjustments",
        json={
            "employee_id": employee_id,
            "leave_dates": ["2026-07-23"],
            "use_overtime": True,
            "use_weekend": True,
        },
    )

    assert adjusted.status_code == 200
    detail = adjusted.json()
    assert detail["summary"]["remaining_hours"] == 0
    assert sum(
        assignment["quantity"]
        for assignment in detail["assignments"]
        if assignment["employee_id"] == employee_id
    ) == 5
    assert all(
        assignment["work_date"] != "2026-07-23"
        for assignment in detail["assignments"]
        if assignment["employee_id"] == employee_id
    )
    assert max(
        assignment["quantity"]
        for assignment in detail["assignments"]
        if assignment["employee_id"] == employee_id
    ) == 1
    assert any(
        assignment["target_date"] != assignment["work_date"]
        for assignment in detail["assignments"]
        if assignment["employee_id"] == employee_id
    )


def test_data_maintenance_clears_cache_and_schedule_history_but_keeps_master_data(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import main as main_module

    monkeypatch.setattr(main_module.sys, "platform", "linux")
    part_id = create_part(client, code="KEEP-PART", hours=0.5)
    employee_id = create_employee(client, "保留员工", "core", [part_id])
    client.post(
        "/api/production-orders",
        json={
            "order_type": "accessory",
            "source_id": part_id,
            "quantity": 4,
            "start_date": "2026-07-20",
            "end_date": "2026-07-24",
        },
    )
    week_id = client.post("/api/production-orders/generate").json()["affected_week_ids"][0]

    cache_result = client.post("/api/maintenance/clear-cache")
    assert cache_result.status_code == 200
    assert cache_result.json()["status"] == "cleared"
    assert client.get(f"/api/weeks/{week_id}").status_code == 200

    cleared = client.delete("/api/maintenance/schedule-history")
    assert cleared.status_code == 200
    assert cleared.json()["status"] == "cleared"
    assert cleared.json()["weeks"] == 1
    assert cleared.json()["orders"] == 1
    assert cleared.json()["assignments"] > 0
    assert client.get("/api/weeks").json() == []
    assert any(item["id"] == part_id for item in client.get("/api/parts").json())
    assert any(item["id"] == employee_id for item in client.get("/api/employees").json())


def test_employee3_and_advance_require_separate_admin_authorization(
    client: TestClient,
):
    part = client.post(
        "/api/parts",
        json={
            "code": "LEVEL-3-MACHINE",
            "name": "三级整机零件",
            "standard_hours": 1,
            "usage_types": ["assembly"],
            "active": True,
        },
    ).json()
    employee_ids = [
        create_employee(client, f"整机员工{level}", "core", [part["id"]])
        for level in (1, 2, 3)
    ]
    updated = client.put(
        f"/api/parts/{part['id']}",
        json={
            "code": part["code"],
            "name": part["name"],
            "standard_hours": 1,
            "usage_types": ["assembly"],
            "level_1_employee_id": employee_ids[0],
            "level_2_employee_id": employee_ids[1],
            "level_3_employee_id": employee_ids[2],
            "active": True,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["level_3_employee"]["employee_name"] == "整机员工3"

    machine = client.post(
        "/api/machines",
        json={
            "code": "LEVEL-3-M",
            "name": "三级整机",
            "active": True,
            "bom_items": [{"part_id": part["id"], "quantity_per_machine": 1}],
        },
    ).json()
    client.post(
        "/api/production-orders",
        json={
            "order_type": "machine",
            "source_id": machine["id"],
            "quantity": 20,
            "start_date": "2026-07-22",
            "end_date": "2026-07-22",
        },
    )
    week_id = client.post(
        "/api/production-orders/generate"
    ).json()["affected_week_ids"][0]
    detail = client.get(f"/api/weeks/{week_id}").json()

    target_day = [
        assignment
        for assignment in detail["assignments"]
        if assignment["work_date"] == "2026-07-22"
    ]
    assert {
        employee_id: sum(
            assignment["quantity"]
            for assignment in target_day
            if assignment["employee_id"] == employee_id
        )
        for employee_id in employee_ids
    } == {
        employee_ids[0]: 6,
        employee_ids[1]: 0,
        employee_ids[2]: 0,
    }
    assert detail["summary"]["remaining_hours"] == 14
    assert all(
        assignment["work_date"] == assignment["target_date"]
        for assignment in detail["assignments"]
    )

    alternated = client.post(
        f"/api/weeks/{week_id}/resolve",
        json={"mode": "alternate", "employee_ids": []},
    )
    assert alternated.status_code == 200
    detail = alternated.json()
    assert {
        employee_id: sum(
            assignment["quantity"]
            for assignment in detail["assignments"]
            if assignment["employee_id"] == employee_id
            and assignment["work_date"] == "2026-07-22"
        )
        for employee_id in employee_ids
    } == {
        employee_ids[0]: 6,
        employee_ids[1]: 6,
        employee_ids[2]: 6,
    }
    assert detail["summary"]["remaining_hours"] == 2
    assert not any(
        assignment["work_date"] != assignment["target_date"]
        for assignment in detail["assignments"]
    )

    advanced = client.post(
        f"/api/weeks/{week_id}/resolve",
        json={"mode": "advance", "employee_ids": []},
    )
    assert advanced.status_code == 200
    detail = advanced.json()
    earlier = [
        assignment
        for assignment in detail["assignments"]
        if assignment["work_date"] != "2026-07-22"
    ]
    assert len(earlier) == 1
    assert earlier[0]["work_date"] == "2026-07-21"
    assert earlier[0]["employee_id"] == employee_ids[0]
    assert earlier[0]["quantity"] == 2
    assert earlier[0]["target_date"] == "2026-07-22"
    assert all(
        assignment["work_date"] >= "2026-07-20"
        for assignment in detail["assignments"]
    )
    assert detail["machine_resolution"] == {
        "allow_alternates": True,
        "allow_advance": True,
    }

    preserved = client.put(
        f"/api/weeks/{week_id}/assignments",
        json={
            "assignments": [
                {
                    "employee_id": assignment["employee_id"],
                    "part_id": assignment["part_id"],
                    "order_item_id": assignment["order_item_id"],
                    "work_date": assignment["work_date"],
                    "target_date": assignment["target_date"],
                    "quantity": assignment["quantity"],
                }
                for assignment in detail["assignments"]
            ]
        },
    )
    assert preserved.status_code == 200
    assert any(
        assignment["work_date"] == "2026-07-21"
        and assignment["target_date"] == "2026-07-22"
        for assignment in preserved.json()["assignments"]
    )


def test_accessory_uses_three_priority_levels_across_whole_period(
    client: TestClient,
):
    part_id = create_part(client, code="LEVEL-3-ACCESSORY", hours=1)
    employee_ids = [
        create_employee(client, f"附件三级员工{level}", "core", [part_id])
        for level in (1, 2, 3)
    ]
    part = client.get("/api/parts").json()[0]
    response = client.put(
        f"/api/parts/{part_id}",
        json={
            "code": part["code"],
            "name": part["name"],
            "standard_hours": 1,
            "usage_types": ["accessory"],
            "level_1_employee_id": employee_ids[0],
            "level_2_employee_id": employee_ids[1],
            "level_3_employee_id": employee_ids[2],
            "active": True,
        },
    )
    assert response.status_code == 200
    client.post(
        "/api/production-orders",
        json={
            "order_type": "accessory",
            "source_id": part_id,
            "quantity": 40,
            "start_date": "2026-07-20",
            "end_date": "2026-07-22",
        },
    )
    week_id = client.post(
        "/api/production-orders/generate"
    ).json()["affected_week_ids"][0]
    detail = client.get(f"/api/weeks/{week_id}").json()
    quantities = {
        employee_id: sum(
            assignment["quantity"]
            for assignment in detail["assignments"]
            if assignment["employee_id"] == employee_id
        )
        for employee_id in employee_ids
    }
    assert quantities == {
        employee_ids[0]: 18,
        employee_ids[1]: 18,
        employee_ids[2]: 4,
    }


def test_machine_bom_matrix_preview_commit_and_atomic_errors(
    client: TestClient,
):
    assembly_a = client.post(
        "/api/parts",
        json={
            "code": "BOM-A",
            "name": "装配件A",
            "standard_hours": 0.5,
            "usage_types": ["assembly"],
            "active": True,
        },
    ).json()
    assembly_b = client.post(
        "/api/parts",
        json={
            "code": "BOM-B",
            "name": "装配件B",
            "standard_hours": 0.25,
            "usage_types": ["assembly"],
            "active": True,
        },
    ).json()
    content = make_xlsx(
        [
            ["料号", "描述", "MATRIX-A", "MATRIX-B"],
            ["", "", "矩阵整机A", "矩阵整机B"],
            ["BOM-A", "装配件A", "Y", 2],
            ["BOM-B", "装配件B", "", "√"],
        ]
    )
    preview_response = client.post(
        "/api/machines/import/preview",
        files={
            "file": (
                "整机BOM.xlsx",
                content,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["valid_count"] == 2
    assert preview["invalid_count"] == 0
    committed = client.post(
        "/api/machines/import/commit",
        json={
            "machines": [
                {
                    "code": machine["code"],
                    "name": machine["name"],
                    "bom_items": [
                        {
                            "part_id": item["part_id"],
                            "quantity_per_machine": item["quantity_per_machine"],
                        }
                        for item in machine["bom_items"]
                    ],
                }
                for machine in preview["machines"]
            ]
        },
    )
    assert committed.status_code == 200
    assert committed.json() == {"created": 2, "updated": 0, "total": 2}
    machines = {item["code"]: item for item in client.get("/api/machines").json()}
    assert {
        item["part_id"]: item["quantity_per_machine"]
        for item in machines["MATRIX-B"]["bom_items"]
    } == {assembly_a["id"]: 2, assembly_b["id"]: 1}

    invalid = client.post(
        "/api/machines/import/preview",
        files={
            "file": (
                "整机BOM错误.xlsx",
                make_xlsx(
                    [
                        ["料号", "描述", "BAD-MACHINE"],
                        ["", "", "错误整机"],
                        ["BOM-A", "装配件A", "=1+1"],
                    ]
                ),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    ).json()
    assert invalid["invalid_count"] == 1
    assert any("不支持公式" in error for error in invalid["machines"][0]["errors"])
    assert "BAD-MACHINE" not in {
        item["code"] for item in client.get("/api/machines").json()
    }


def test_machine_plan_matrix_replaces_same_week_and_keeps_manual_orders(
    client: TestClient,
):
    part = client.post(
        "/api/parts",
        json={
            "code": "PLAN-BOM",
            "name": "计划装配件",
            "standard_hours": 0.1,
            "usage_types": ["assembly"],
            "active": True,
        },
    ).json()
    machine = client.post(
        "/api/machines",
        json={
            "code": "PLAN-MACHINE",
            "name": "计划整机",
            "active": True,
            "bom_items": [{"part_id": part["id"], "quantity_per_machine": 1}],
        },
    ).json()
    manual_order = client.post(
        "/api/production-orders",
        json={
            "order_type": "machine",
            "source_id": machine["id"],
            "quantity": 1,
            "start_date": "2026-07-21",
            "end_date": "2026-07-21",
        },
    ).json()
    first_matrix = make_xlsx(
        [
            ["星期", "PLAN-MACHINE"],
            ["星期一", 2],
            ["星期二", ""],
            ["星期三", ""],
            ["星期四", ""],
            ["星期五", ""],
            ["星期六", 1],
            ["星期日", ""],
        ]
    )
    preview = client.post(
        "/api/production-orders/machine-plan-import/preview",
        data={"week_start": "2026-07-20"},
        files={
            "file": (
                "整机周计划.xlsx",
                first_matrix,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert preview.status_code == 200
    preview_data = preview.json()
    assert preview_data["nonzero_count"] == 2
    assert preview_data["invalid_count"] == 0
    entries = [
        {
            "machine_code": item["machine_code"],
            "target_date": item["target_date"],
            "quantity": item["quantity"],
        }
        for item in preview_data["entries"]
        if item["quantity"] > 0
    ]
    committed = client.post(
        "/api/production-orders/machine-plan-import/commit",
        json={"week_start": "2026-07-20", "entries": entries},
    )
    assert committed.status_code == 200
    assert committed.json()["created"] == 2
    assert committed.json()["weekend_enabled"] is True
    week_id = committed.json()["week_id"]
    week = client.get(f"/api/weeks/{week_id}").json()
    assert week["include_weekend"] is True

    client.post("/api/production-orders/generate")
    replacement = client.post(
        "/api/production-orders/machine-plan-import/commit",
        json={
            "week_start": "2026-07-20",
            "entries": [
                {
                    "machine_code": "PLAN-MACHINE",
                    "target_date": "2026-07-20",
                    "quantity": 3,
                }
            ],
        },
    )
    assert replacement.status_code == 200
    assert replacement.json()["replaced"] == 2
    orders = client.get("/api/production-orders").json()
    assert {item["id"] for item in orders if item["origin"] == "manual"} == {
        manual_order["id"]
    }
    imported = [
        item for item in orders if item["origin"] == "machine_plan_import"
    ]
    assert len(imported) == 1
    assert imported[0]["quantity"] == 3
    assert imported[0]["start_date"] == imported[0]["end_date"] == "2026-07-20"
    assert imported[0]["import_week_start"] == "2026-07-20"

    template = client.get(
        "/api/production-orders/machine-plan-import/template"
    )
    bom_template = client.get("/api/machines/import/template")
    assert template.status_code == 200 and template.content.startswith(b"PK")
    assert bom_template.status_code == 200 and bom_template.content.startswith(b"PK")


def test_confirmed_machine_leave_transfers_same_day_to_employee2_then_employee3(
    client: TestClient,
):
    part = client.post(
        "/api/parts",
        json={
            "code": "LEAVE-LEVEL-3",
            "name": "请假三级装配件",
            "standard_hours": 1,
            "usage_types": ["assembly"],
            "active": True,
        },
    ).json()
    employees = [
        create_employee(client, f"请假优先员工{level}", "core", [part["id"]])
        for level in (1, 2, 3)
    ]
    client.put(
        f"/api/parts/{part['id']}",
        json={
            "code": part["code"],
            "name": part["name"],
            "standard_hours": 1,
            "usage_types": ["assembly"],
            "level_1_employee_id": employees[0],
            "level_2_employee_id": employees[1],
            "level_3_employee_id": employees[2],
            "active": True,
        },
    )
    machine = client.post(
        "/api/machines",
        json={
            "code": "LEAVE-LEVEL-3-M",
            "name": "请假三级整机",
            "active": True,
            "bom_items": [{"part_id": part["id"], "quantity_per_machine": 1}],
        },
    ).json()
    client.post(
        "/api/production-orders",
        json={
            "order_type": "machine",
            "source_id": machine["id"],
            "quantity": 10,
            "start_date": "2026-07-20",
            "end_date": "2026-07-20",
        },
    )
    week_id = client.post(
        "/api/production-orders/generate"
    ).json()["affected_week_ids"][0]
    assert client.post(
        f"/api/weeks/{week_id}/resolve",
        json={"mode": "alternate", "employee_ids": []},
    ).status_code == 200
    assert client.post(f"/api/weeks/{week_id}/confirm").status_code == 200
    adjusted = client.post(
        f"/api/weeks/{week_id}/leave-adjustments",
        json={
            "employee_id": employees[0],
            "leave_dates": ["2026-07-20"],
            "use_overtime": False,
            "use_weekend": False,
        },
    )
    assert adjusted.status_code == 200
    detail = adjusted.json()
    assert detail["summary"]["remaining_hours"] == 0
    assert {
        employee_id: sum(
            assignment["quantity"]
            for assignment in detail["assignments"]
            if assignment["employee_id"] == employee_id
            and assignment["part_id"] == part["id"]
        )
        for employee_id in employees
    } == {
        employees[0]: 0,
        employees[1]: 6,
        employees[2]: 4,
    }
    assert {
        assignment["work_date"]
        for assignment in detail["assignments"]
        if assignment["part_id"] == part["id"]
    } == {"2026-07-20"}
