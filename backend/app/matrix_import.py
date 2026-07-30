from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import re
import sqlite3
from typing import Any

from fastapi import HTTPException

from .part_import import (
    CellValue,
    MAX_IMPORT_BYTES,
    MAX_IMPORT_ROWS,
    _text,
    _xlsx_rows,
)
from .production import generation_today


WEEKDAY_ALIASES = {
    "星期一": 0,
    "周一": 0,
    "星期二": 1,
    "周二": 1,
    "星期三": 2,
    "周三": 2,
    "星期四": 3,
    "周四": 3,
    "星期五": 4,
    "周五": 4,
    "星期六": 5,
    "周六": 5,
    "星期日": 6,
    "星期天": 6,
    "周日": 6,
    "周天": 6,
}
INCLUDED_VALUES = {"y", "yes", "是", "√", "✓"}


def _rows(filename: str, content: bytes) -> list[list[CellValue]]:
    if not content:
        raise HTTPException(status_code=422, detail="请选择非空Excel文件")
    if len(content) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="导入文件不能超过5MB")
    if Path(filename).suffix.lower() != ".xlsx":
        raise HTTPException(status_code=422, detail="矩阵导入仅支持.xlsx文件")
    rows = _xlsx_rows(content)
    if not rows:
        raise HTTPException(status_code=422, detail="Excel中没有可读取的数据")
    return rows


def _normalized(value: Any) -> str:
    return re.sub(r"[\s_()（）\-]+", "", _text(value)).lower()


def _cell(row: list[CellValue], index: int) -> CellValue:
    return row[index] if index < len(row) else CellValue(None)


def _parse_bom_quantity(cell: CellValue) -> tuple[int | None, str | None]:
    text = _text(cell.value)
    if not text:
        return None, None
    if cell.is_formula:
        return None, "不支持公式，请粘贴为数值"
    if text.lower() in INCLUDED_VALUES:
        return 1, None
    try:
        value = float(text)
        if not value.is_integer() or value <= 0 or value > 100_000:
            raise ValueError
        return int(value), None
    except (TypeError, ValueError):
        return None, "应填写Y或大于0且不超过100000的整数"


def preview_machine_bom_matrix(
    connection: sqlite3.Connection,
    filename: str,
    content: bytes,
) -> dict[str, Any]:
    rows = _rows(filename, content)
    header_index = -1
    code_column = -1
    name_column = -1
    for row_index, row in enumerate(rows[:10]):
        for column, cell in enumerate(row):
            value = _normalized(cell.value)
            if value in {"料号", "零件编号", "编号", "partcode"}:
                code_column = column
            if value in {"描述", "零件名称", "名称", "partname"}:
                name_column = column
        if code_column >= 0 and name_column >= 0:
            header_index = row_index
            break
    if header_index < 0:
        raise HTTPException(
            status_code=422,
            detail="表头必须包含“料号”和“描述”两列",
        )
    if header_index + 1 >= len(rows):
        raise HTTPException(status_code=422, detail="整机编号下一行必须填写整机名称")

    header = rows[header_index]
    name_row = rows[header_index + 1]
    first_machine_column = max(code_column, name_column) + 1
    machine_columns = [
        (column, _text(cell.value), cell.is_formula)
        for column, cell in enumerate(header)
        if column >= first_machine_column and _text(cell.value)
    ]
    if not machine_columns:
        raise HTTPException(status_code=422, detail="表头中没有整机编号")
    machine_codes = [code for _, code, _ in machine_columns]
    duplicate_codes = sorted(
        {code for code in machine_codes if machine_codes.count(code) > 1}
    )
    if duplicate_codes:
        raise HTTPException(
            status_code=422,
            detail=f"整机编号重复：{'、'.join(duplicate_codes)}",
        )

    parts = {
        str(row["code"]): row
        for row in connection.execute(
            "SELECT id, code, name, active, is_assembly FROM parts"
        ).fetchall()
    }
    machines = {
        str(row["code"]): row
        for row in connection.execute(
            "SELECT id, code, name, active FROM machines"
        ).fetchall()
    }
    preview_machines: list[dict[str, Any]] = []
    for column, machine_code, header_is_formula in machine_columns:
        errors: list[str] = []
        if header_is_formula:
            errors.append("整机编号不支持公式")
        machine_name_cell = _cell(name_row, column)
        machine_name = _text(machine_name_cell.value)
        if machine_name_cell.is_formula:
            errors.append("整机名称不支持公式")
        if not machine_name:
            errors.append("第二行必须填写整机名称")
        elif len(machine_name) > 100:
            errors.append("整机名称不能超过100个字符")
        if len(machine_code) > 40:
            errors.append("整机编号不能超过40个字符")
        bom_items: list[dict[str, Any]] = []
        for row_number, source in enumerate(
            rows[header_index + 2 :],
            start=header_index + 3,
        ):
            quantity, quantity_error = _parse_bom_quantity(
                _cell(source, column)
            )
            if quantity is None and quantity_error is None:
                continue
            part_code_cell = _cell(source, code_column)
            part_code = _text(part_code_cell.value)
            if part_code_cell.is_formula:
                errors.append(f"第{row_number}行零件编号不支持公式")
                continue
            if quantity_error:
                errors.append(f"第{row_number}行：{quantity_error}")
                continue
            part = parts.get(part_code)
            if not part_code:
                errors.append(f"第{row_number}行：料号不能为空")
            elif part is None:
                errors.append(f"第{row_number}行：零件“{part_code}”不存在")
            elif not bool(part["active"]):
                errors.append(f"第{row_number}行：零件“{part_code}”已停用")
            elif not bool(part["is_assembly"]):
                errors.append(
                    f"第{row_number}行：零件“{part_code}”不具备整机装配用途"
                )
            else:
                bom_items.append(
                    {
                        "part_id": int(part["id"]),
                        "part_code": part_code,
                        "part_name": str(part["name"]),
                        "quantity_per_machine": int(quantity),
                    }
                )
        part_ids = [item["part_id"] for item in bom_items]
        duplicate_part_ids = {
            part_id for part_id in part_ids if part_ids.count(part_id) > 1
        }
        if duplicate_part_ids:
            duplicate_codes = sorted(
                {
                    item["part_code"]
                    for item in bom_items
                    if item["part_id"] in duplicate_part_ids
                }
            )
            errors.append(
                f"BOM中零件重复：{'、'.join(duplicate_codes)}"
            )
        if not bom_items:
            errors.append("该整机没有任何有效BOM零件")
        existing = machines.get(machine_code)
        preview_machines.append(
            {
                "column": column + 1,
                "code": machine_code,
                "name": machine_name,
                "action": "update" if existing is not None else "create",
                "existing_active": (
                    bool(existing["active"]) if existing is not None else True
                ),
                "bom_items": bom_items,
                "errors": list(dict.fromkeys(errors)),
            }
        )
    invalid_count = sum(bool(item["errors"]) for item in preview_machines)
    return {
        "filename": filename,
        "total_machines": len(preview_machines),
        "valid_count": len(preview_machines) - invalid_count,
        "invalid_count": invalid_count,
        "machines": preview_machines,
    }


def _parse_plan_quantity(cell: CellValue) -> tuple[int, str | None]:
    text = _text(cell.value)
    if not text:
        return 0, None
    if cell.is_formula:
        return 0, "不支持公式，请粘贴为数值"
    try:
        value = float(text)
        if not value.is_integer() or value < 0 or value > 10_000_000:
            raise ValueError
        return int(value), None
    except (TypeError, ValueError):
        return 0, "数量必须是0至10000000之间的整数"


def preview_machine_plan_matrix(
    connection: sqlite3.Connection,
    filename: str,
    content: bytes,
    week_start: date,
) -> dict[str, Any]:
    if week_start.weekday() != 0:
        raise HTTPException(status_code=422, detail="目标周必须选择周一")
    rows = _rows(filename, content)
    day_rows: dict[int, tuple[int, int, list[CellValue]]] = {}
    for row_number, row in enumerate(rows, start=1):
        first_value_cell = next(
            (cell for cell in row if _text(cell.value)),
            None,
        )
        if first_value_cell is not None:
            first_value = _text(first_value_cell.value)
            if (
                (first_value.startswith("星期") or first_value.startswith("周"))
                and first_value not in {"星期", "周次"}
                and first_value not in WEEKDAY_ALIASES
            ):
                raise HTTPException(
                    status_code=422,
                    detail=f"第{row_number}行星期名称无效：{first_value}",
                )
        for column, cell in enumerate(row):
            weekday = WEEKDAY_ALIASES.get(_text(cell.value))
            if weekday is None:
                continue
            if cell.is_formula:
                raise HTTPException(
                    status_code=422,
                    detail=f"第{row_number}行星期名称不支持公式",
                )
            if weekday in day_rows:
                raise HTTPException(
                    status_code=422,
                    detail=f"{_text(cell.value)}在表格中重复",
                )
            day_rows[weekday] = (row_number, column, row)
            break
    missing = [index for index in range(7) if index not in day_rows]
    if missing:
        labels = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        raise HTTPException(
            status_code=422,
            detail=f"缺少日期行：{'、'.join(labels[index] for index in missing)}",
        )
    first_row_number = min(item[0] for item in day_rows.values())
    if first_row_number <= 1:
        raise HTTPException(status_code=422, detail="星期行上方必须有整机编号表头")
    header = rows[first_row_number - 2]
    day_column = next(iter(day_rows.values()))[1]
    machine_columns = [
        (column, _text(cell.value), cell.is_formula)
        for column, cell in enumerate(header)
        if column != day_column and _text(cell.value)
    ]
    if not machine_columns:
        raise HTTPException(status_code=422, detail="表头中没有整机编号")
    codes = [code for _, code, _ in machine_columns]
    duplicate_codes = sorted({code for code in codes if codes.count(code) > 1})
    if duplicate_codes:
        raise HTTPException(
            status_code=422,
            detail=f"整机编号重复：{'、'.join(duplicate_codes)}",
        )
    machines = {
        str(row["code"]): row
        for row in connection.execute(
            "SELECT id, code, name, active FROM machines"
        ).fetchall()
    }
    column_errors: dict[str, list[str]] = {}
    for _, code, is_formula in machine_columns:
        machine = machines.get(code)
        if is_formula:
            column_errors[code] = ["整机编号不支持公式"]
        elif machine is None:
            column_errors[code] = [f"整机“{code}”不存在"]
        elif not bool(machine["active"]):
            column_errors[code] = [f"整机“{code}”已停用"]

    today = generation_today()
    entries: list[dict[str, Any]] = []
    for weekday in range(7):
        row_number, _, row = day_rows[weekday]
        target_date = week_start + timedelta(days=weekday)
        for column, machine_code, _ in machine_columns:
            quantity, quantity_error = _parse_plan_quantity(
                _cell(row, column)
            )
            errors = [*column_errors.get(machine_code, [])]
            if quantity_error:
                errors.append(quantity_error)
            if quantity > 0 and target_date < today:
                errors.append("目标日期早于系统当天")
            machine = machines.get(machine_code)
            entries.append(
                {
                    "row_number": row_number,
                    "column": column + 1,
                    "weekday": weekday,
                    "target_date": target_date.isoformat(),
                    "machine_code": machine_code,
                    "machine_id": (
                        int(machine["id"]) if machine is not None else None
                    ),
                    "machine_name": (
                        str(machine["name"]) if machine is not None else ""
                    ),
                    "quantity": quantity,
                    "errors": list(dict.fromkeys(errors)),
                }
            )
    invalid_count = sum(bool(item["errors"]) for item in entries)
    nonzero_count = sum(item["quantity"] > 0 for item in entries)
    return {
        "filename": filename,
        "week_start": week_start.isoformat(),
        "total_cells": len(entries),
        "nonzero_count": nonzero_count,
        "invalid_count": invalid_count,
        "entries": entries,
    }
