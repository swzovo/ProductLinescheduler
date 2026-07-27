from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import re
import sqlite3
from typing import Any

from fastapi import HTTPException

from .part_import import (
    CellValue,
    MAX_IMPORT_BYTES,
    MAX_IMPORT_ROWS,
    _csv_rows,
    _text,
    _xlsx_rows,
)


HEADER_ALIASES = {
    "part_code": {"零件编号", "编号", "partcode", "code"},
    "quantity": {"数量", "订单数量", "quantity", "qty"},
    "start_date": {"开始日期", "起始日期", "startdate"},
    "end_date": {"截止日期", "结束日期", "enddate", "duedate"},
}


def _normalize_header(value: Any) -> str:
    return re.sub(r"[\s_()（）\-]+", "", _text(value)).lower()


def _field_for_header(value: Any) -> str | None:
    normalized = _normalize_header(value)
    return next(
        (
            field
            for field, aliases in HEADER_ALIASES.items()
            if normalized in aliases
        ),
        None,
    )


def _parse_date(value: Any) -> tuple[str | None, str | None]:
    if isinstance(value, datetime):
        return value.date().isoformat(), None
    if isinstance(value, date):
        return value.isoformat(), None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value <= 0 or value > 2958465:
            return None, "日期数值无效"
        return (date(1899, 12, 30) + timedelta(days=int(value))).isoformat(), None
    text = _text(value)
    if not text:
        return None, "日期不能为空"
    normalized = text.replace("/", "-").replace(".", "-")
    try:
        return date.fromisoformat(normalized).isoformat(), None
    except ValueError:
        return None, "日期应填写为YYYY-MM-DD或有效Excel日期"


def preview_accessory_order_import(
    connection: sqlite3.Connection,
    filename: str,
    content: bytes,
) -> dict[str, Any]:
    if not content:
        raise HTTPException(status_code=422, detail="请选择非空表格文件")
    if len(content) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="导入文件不能超过5MB")
    suffix = Path(filename).suffix.lower()
    if suffix == ".xlsx":
        source_rows = _xlsx_rows(content)
    elif suffix == ".csv":
        source_rows = _csv_rows(content)
    else:
        raise HTTPException(status_code=422, detail="仅支持.xlsx和.csv文件")
    if not source_rows:
        raise HTTPException(status_code=422, detail="表格中没有可读取的数据")

    fields: dict[str, int] = {}
    for index, cell in enumerate(source_rows[0]):
        field = _field_for_header(cell.value)
        if field and field not in fields:
            fields[field] = index
    missing = [field for field in HEADER_ALIASES if field not in fields]
    if missing:
        labels = {
            "part_code": "零件编号",
            "quantity": "数量",
            "start_date": "开始日期",
            "end_date": "截止日期",
        }
        raise HTTPException(
            status_code=422,
            detail=f"表头缺少：{'、'.join(labels[field] for field in missing)}",
        )

    parts = {
        str(row["code"]): row
        for row in connection.execute(
            "SELECT id, code, name, active, is_accessory FROM parts"
        ).fetchall()
    }
    result_rows: list[dict[str, Any]] = []
    for row_number, source in enumerate(source_rows[1:], start=2):
        values = {
            field: source[index] if index < len(source) else CellValue(None)
            for field, index in fields.items()
        }
        if not any(_text(cell.value) for cell in values.values()):
            continue
        errors: list[str] = []
        if any(cell.is_formula for cell in values.values()):
            errors.append("不支持公式单元格，请粘贴为数值")
        part_code = _text(values["part_code"].value)
        part = parts.get(part_code)
        if not part_code:
            errors.append("零件编号不能为空")
        elif part is None:
            errors.append(f"零件编号“{part_code}”不存在")
        elif not bool(part["active"]):
            errors.append(f"零件“{part_code}”已停用")
        elif not bool(part["is_accessory"]):
            errors.append(f"零件“{part_code}”不具备附件用途")
        quantity_text = _text(values["quantity"].value)
        try:
            numeric = float(quantity_text)
            if not numeric.is_integer() or numeric <= 0 or numeric > 10_000_000:
                raise ValueError
            quantity = int(numeric)
        except (TypeError, ValueError):
            quantity = 0
            errors.append("数量必须是大于0且不超过10000000的整数")
        start_date, start_error = _parse_date(values["start_date"].value)
        end_date, end_error = _parse_date(values["end_date"].value)
        if start_error:
            errors.append(f"开始日期：{start_error}")
        if end_error:
            errors.append(f"截止日期：{end_error}")
        if start_date and end_date and end_date < start_date:
            errors.append("截止日期不能早于开始日期")
        result_rows.append(
            {
                "row_number": row_number,
                "part_code": part_code,
                "part_id": int(part["id"]) if part is not None else None,
                "part_name": str(part["name"]) if part is not None else "",
                "quantity": quantity,
                "start_date": start_date or "",
                "end_date": end_date or "",
                "errors": errors,
            }
        )

    if not result_rows:
        raise HTTPException(status_code=422, detail="表格中没有附件订单数据")
    if len(result_rows) > MAX_IMPORT_ROWS:
        raise HTTPException(
            status_code=422, detail=f"导入数据不能超过{MAX_IMPORT_ROWS}行"
        )
    invalid_count = sum(bool(row["errors"]) for row in result_rows)
    return {
        "filename": filename,
        "total_rows": len(result_rows),
        "valid_count": len(result_rows) - invalid_count,
        "invalid_count": invalid_count,
        "rows": result_rows,
    }
