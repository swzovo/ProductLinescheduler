from __future__ import annotations

import csv
import io
import posixpath
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

from fastapi import HTTPException


MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ROWS = 5000
REQUIRED_FIELDS = ("code", "name", "standard_hours")
HEADER_ALIASES = {
    "code": {"零件编号", "编号", "code", "partcode"},
    "name": {"零件名称", "名称", "name", "partname"},
    "standard_hours": {
        "单件标准工时小时",
        "单件标准工时",
        "标准工时",
        "工时",
        "standardhours",
        "hours",
    },
    "active": {"启用状态", "启用", "状态", "active"},
    "usage_types": {"零件用途", "用途", "usagetype", "usagetypes"},
    "employee_names": {"员工", "制作员工", "可制作员工", "employee", "employees"},
    "employee_level1_names": {"员工1", "一级员工", "优先员工", "employee1"},
    "employee_level2_names": {"员工2", "二级员工", "次选员工", "employee2"},
}
TRUE_VALUES = {"启用", "是", "1", "true", "yes", "y"}
FALSE_VALUES = {"停用", "否", "0", "false", "no", "n"}
USAGE_VALUES = {
    "附件": ["accessory"],
    "附件零件": ["accessory"],
    "整机装配": ["assembly"],
    "整机装配件": ["assembly"],
    "双用途": ["accessory", "assembly"],
    "附件+整机装配": ["accessory", "assembly"],
}


@dataclass
class CellValue:
    value: Any
    is_formula: bool = False


def _normalize_header(value: Any) -> str:
    text = "" if value is None else str(value)
    return re.sub(r"[\s_()（）\-]+", "", text).lower()


def _field_for_header(value: Any) -> str | None:
    normalized = _normalize_header(value)
    for field, aliases in HEADER_ALIASES.items():
        if normalized in aliases:
            return field
    return None


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Za-z]+", reference)
    if letters is None:
        return 0
    result = 0
    for letter in letters.group(0).upper():
        result = result * 26 + ord(letter) - ord("A") + 1
    return result - 1


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    return [
        "".join(node.text or "" for node in item.findall(".//x:t", namespace))
        for item in root.findall("x:si", namespace)
    ]


def _first_sheet_path(archive: zipfile.ZipFile) -> str:
    main_namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_namespace = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_namespace = "http://schemas.openxmlformats.org/package/2006/relationships"
    try:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationships = ElementTree.fromstring(
            archive.read("xl/_rels/workbook.xml.rels")
        )
    except (KeyError, ElementTree.ParseError) as error:
        raise HTTPException(status_code=422, detail="Excel文件结构无效") from error

    sheet = workbook.find(f".//{{{main_namespace}}}sheet")
    if sheet is None:
        raise HTTPException(status_code=422, detail="Excel文件中没有工作表")
    relationship_id = sheet.attrib.get(f"{{{rel_namespace}}}id")
    target = None
    for item in relationships.findall(f"{{{package_namespace}}}Relationship"):
        if item.attrib.get("Id") == relationship_id:
            target = item.attrib.get("Target")
            break
    if not target:
        raise HTTPException(status_code=422, detail="无法读取Excel工作表")
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(str(PurePosixPath("xl") / target))


def _xlsx_rows(content: bytes) -> list[list[CellValue]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as error:
        raise HTTPException(status_code=422, detail="Excel文件损坏或格式不受支持") from error
    with archive:
        shared = _shared_strings(archive)
        path = _first_sheet_path(archive)
        try:
            root = ElementTree.fromstring(archive.read(path))
        except (KeyError, ElementTree.ParseError) as error:
            raise HTTPException(status_code=422, detail="无法读取Excel第一张工作表") from error

    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    result: list[list[CellValue]] = []
    for row in root.findall(".//x:sheetData/x:row", namespace):
        try:
            row_number = int(row.attrib.get("r", len(result) + 1))
        except ValueError:
            row_number = len(result) + 1
        if row_number > MAX_IMPORT_ROWS + 1:
            raise HTTPException(
                status_code=422, detail=f"导入数据不能超过{MAX_IMPORT_ROWS}行"
            )
        while len(result) < row_number - 1:
            result.append([])
        cells: dict[int, CellValue] = {}
        for cell in row.findall("x:c", namespace):
            index = _column_index(cell.attrib.get("r", "A1"))
            value_type = cell.attrib.get("t")
            formula = cell.find("x:f", namespace)
            value_node = cell.find("x:v", namespace)
            value: Any = None
            if value_type == "inlineStr":
                value = "".join(
                    node.text or "" for node in cell.findall(".//x:is/x:t", namespace)
                )
            elif value_node is not None and value_node.text is not None:
                raw = value_node.text
                if value_type == "s":
                    try:
                        value = shared[int(raw)]
                    except (ValueError, IndexError):
                        value = ""
                elif value_type in {"str", "e"}:
                    value = raw
                elif value_type == "b":
                    value = raw == "1"
                else:
                    try:
                        numeric = float(raw)
                        value = int(numeric) if numeric.is_integer() else numeric
                    except ValueError:
                        value = raw
            cells[index] = CellValue(value=value, is_formula=formula is not None)
        if cells:
            width = max(cells) + 1
            result.append([cells.get(index, CellValue(None)) for index in range(width)])
        else:
            result.append([])
        if len(result) > MAX_IMPORT_ROWS + 1:
            raise HTTPException(
                status_code=422, detail=f"导入数据不能超过{MAX_IMPORT_ROWS}行"
            )
    return result


def _csv_rows(content: bytes) -> list[list[CellValue]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("gb18030")
        except UnicodeDecodeError as error:
            raise HTTPException(
                status_code=422, detail="CSV编码无法识别，请使用UTF-8或GB18030"
            ) from error
    rows = [
        [CellValue(value=item, is_formula=item.lstrip().startswith("=")) for item in row]
        for row in csv.reader(io.StringIO(text))
    ]
    if len(rows) > MAX_IMPORT_ROWS + 1:
        raise HTTPException(
            status_code=422, detail=f"导入数据不能超过{MAX_IMPORT_ROWS}行"
        )
    return rows


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _parse_active(value: Any) -> tuple[bool | None, str | None]:
    text = _text(value).lower()
    if not text:
        return None, None
    if text in TRUE_VALUES:
        return True, None
    if text in FALSE_VALUES:
        return False, None
    return None, "启用状态应填写启用/停用、是/否、1/0或true/false"


def _parse_usage(value: Any) -> tuple[list[str] | None, str | None]:
    text = _text(value).replace(" ", "")
    if not text:
        return None, None
    if text in USAGE_VALUES:
        return USAGE_VALUES[text], None
    return None, "零件用途应填写附件、整机装配或双用途"


def _parse_employee_names(value: Any) -> tuple[list[str], list[str]]:
    text = _text(value)
    if not text:
        return [], []
    names = list(
        dict.fromkeys(
            name.strip()
            for name in re.split(r"[、,，;；|\n]+", text)
            if name.strip()
        )
    )
    errors: list[str] = []
    if len(names) > 200:
        errors.append("单个零件最多填写200名员工")
    oversized = [name for name in names if len(name) > 100]
    if oversized:
        errors.append("员工姓名不能超过100个字符")
    return names, errors


def _header_map(row: list[CellValue]) -> dict[str, int]:
    fields: dict[str, int] = {}
    for index, cell in enumerate(row):
        field = _field_for_header(cell.value)
        if field and field not in fields:
            fields[field] = index
    missing = [field for field in REQUIRED_FIELDS if field not in fields]
    if missing:
        labels = {
            "code": "零件编号",
            "name": "零件名称",
            "standard_hours": "单件标准工时（小时）",
        }
        raise HTTPException(
            status_code=422,
            detail=f"表头缺少：{'、'.join(labels[field] for field in missing)}",
        )
    return fields


def preview_import(
    connection: sqlite3.Connection, filename: str, content: bytes
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

    fields = _header_map(source_rows[0])
    existing_rows = connection.execute("SELECT * FROM parts").fetchall()
    existing = {str(row["code"]): row for row in existing_rows}
    existing_employee_names = {
        str(row["name"])
        for row in connection.execute("SELECT name FROM employees").fetchall()
    }
    result_rows: list[dict[str, Any]] = []
    positions: dict[str, list[int]] = {}

    for row_number, source in enumerate(source_rows[1:], start=2):
        values = {
            field: source[index] if index < len(source) else CellValue(None)
            for field, index in fields.items()
        }
        if not any(_text(cell.value) for cell in values.values()):
            continue
        errors: list[str] = []
        for field, cell in values.items():
            if cell.is_formula:
                errors.append("不支持公式单元格，请粘贴为数值")
                break
        code = _text(values["code"].value)
        name = _text(values["name"].value)
        hours_text = _text(values["standard_hours"].value)
        if not code:
            errors.append("零件编号不能为空")
        elif len(code) > 40:
            errors.append("零件编号不能超过40个字符")
        if not name:
            errors.append("零件名称不能为空")
        elif len(name) > 100:
            errors.append("零件名称不能超过100个字符")
        try:
            standard_hours = float(hours_text)
            if standard_hours <= 0 or standard_hours > 1000:
                raise ValueError
        except (TypeError, ValueError):
            standard_hours = 0.0
            errors.append("单件标准工时必须大于0且不超过1000小时")
        active_value = values.get("active", CellValue(None)).value
        active, active_error = _parse_active(active_value)
        if active_error:
            errors.append(active_error)
        usage_value = values.get("usage_types", CellValue(None)).value
        usage_types, usage_error = _parse_usage(usage_value)
        if usage_error:
            errors.append(usage_error)
        employee_names, employee_errors = _parse_employee_names(
            values.get("employee_names", CellValue(None)).value
        )
        errors.extend(employee_errors)
        employee_level1_names, level1_errors = _parse_employee_names(
            values.get("employee_level1_names", CellValue(None)).value
        )
        employee_level2_names, level2_errors = _parse_employee_names(
            values.get("employee_level2_names", CellValue(None)).value
        )
        errors.extend(level1_errors)
        errors.extend(level2_errors)
        if len(employee_level1_names) > 1:
            errors.append("员工1每格只能填写一名员工")
        if len(employee_level2_names) > 1:
            errors.append("员工2每格只能填写一名员工")
        overlap = set(employee_level1_names) & set(employee_level2_names)
        if overlap:
            errors.append(
                f"同一员工不能同时设置为员工1和员工2：{'、'.join(sorted(overlap))}"
            )
        current = existing.get(code)
        effective_active = (
            active
            if active is not None
            else (bool(current["active"]) if current is not None else True)
        )
        effective_usage = usage_types if usage_types is not None else (
            [
                usage
                for usage, enabled in (
                    ("accessory", bool(current["is_accessory"])),
                    ("assembly", bool(current["is_assembly"])),
                )
                if enabled
            ]
            if current is not None
            else ["accessory"]
        )
        item = {
            "row_number": row_number,
            "code": code,
            "name": name,
            "standard_hours": standard_hours,
            "active": effective_active,
            "usage_types": effective_usage,
            "employee_names": employee_names,
            "employee_level1_names": employee_level1_names,
            "employee_level2_names": employee_level2_names,
            "action": "update" if current is not None else "create",
            "errors": errors,
        }
        result_rows.append(item)
        if code:
            positions.setdefault(code, []).append(len(result_rows) - 1)

    if not result_rows:
        raise HTTPException(status_code=422, detail="表格中没有零件数据")
    if len(result_rows) > MAX_IMPORT_ROWS:
        raise HTTPException(
            status_code=422, detail=f"导入数据不能超过{MAX_IMPORT_ROWS}行"
        )
    for code, indexes in positions.items():
        if len(indexes) > 1:
            for index in indexes:
                result_rows[index]["errors"].append(f"表格内零件编号“{code}”重复")

    invalid_count = sum(bool(row["errors"]) for row in result_rows)
    valid_rows = [row for row in result_rows if not row["errors"]]
    imported_employee_names = {
        employee_name
        for row in valid_rows
        for employee_name in [
            *row["employee_names"],
            *row["employee_level1_names"],
            *row["employee_level2_names"],
        ]
    }
    new_employee_names = sorted(imported_employee_names - existing_employee_names)
    return {
        "filename": filename,
        "total_rows": len(result_rows),
        "valid_count": len(valid_rows),
        "invalid_count": invalid_count,
        "create_count": sum(row["action"] == "create" for row in valid_rows),
        "update_count": sum(row["action"] == "update" for row in valid_rows),
        "new_employee_count": len(new_employee_names),
        "new_employee_names": new_employee_names,
        "rows": result_rows,
    }
