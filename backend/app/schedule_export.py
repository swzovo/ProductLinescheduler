from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageDraw, ImageFont


PAGE_WIDTH = 2200
PAGE_HEIGHT = 1556
MARGIN = 70
BODY_TOP = 205
BODY_BOTTOM = PAGE_HEIGHT - 78

INK = "#18312d"
MUTED = "#647872"
DEEP = "#123d36"
GREEN = "#52a675"
YELLOW = "#dcae3f"
RED = "#dc6458"
ORANGE = "#e97b45"
LINE = "#dfe5df"
PALE = "#f4f7f3"
WHITE = "#ffffff"


def _font_path(bold: bool = False) -> str:
    candidates = (
        [
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
            "C:/Windows/Fonts/msyhbd.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        ]
        if bold
        else [
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simsun.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ]
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    raise RuntimeError("系统中未找到可用于导出中文排班表的字体")


_FONTS: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key not in _FONTS:
        _FONTS[key] = ImageFont.truetype(_font_path(bold), size=size)
    return _FONTS[key]


def _text_width(draw: ImageDraw.ImageDraw, text: str, text_font: ImageFont.FreeTypeFont) -> int:
    box = draw.textbbox((0, 0), text, font=text_font)
    return box[2] - box[0]


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    text_font: ImageFont.FreeTypeFont,
    width: int,
) -> str:
    if _text_width(draw, text, text_font) <= width:
        return text
    suffix = "…"
    result = text
    while result and _text_width(draw, result + suffix, text_font) > width:
        result = result[:-1]
    return result + suffix


def _new_page(week: dict[str, Any], section: str) -> Image.Image:
    image = Image.new("RGB", (PAGE_WIDTH, PAGE_HEIGHT), "#f2f5f1")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (34, 32, PAGE_WIDTH - 34, PAGE_HEIGHT - 32),
        radius=24,
        fill=WHITE,
        outline="#e5eae5",
        width=2,
    )
    draw.text((MARGIN, 64), "产线周排班明细", fill=INK, font=font(42, True))
    draw.text(
        (MARGIN, 124),
        f"{week['week_start']} 开始  ·  {len(week['days'])} 个工作日  ·  已确认",
        fill=MUTED,
        font=font(21),
    )
    section_font = font(24, True)
    section_width = _text_width(draw, section, section_font)
    draw.rounded_rectangle(
        (PAGE_WIDTH - MARGIN - section_width - 44, 72, PAGE_WIDTH - MARGIN, 118),
        radius=20,
        fill="#e8f2ed",
    )
    draw.text(
        (PAGE_WIDTH - MARGIN - section_width - 22, 81),
        section,
        fill=DEEP,
        font=section_font,
    )
    draw.line((MARGIN, 172, PAGE_WIDTH - MARGIN, 172), fill=LINE, width=2)
    return image


def _draw_footer(image: Image.Image, page_number: int, total_pages: int) -> None:
    draw = ImageDraw.Draw(image)
    confirmed = datetime.now().strftime("%Y-%m-%d %H:%M")
    draw.text(
        (MARGIN, PAGE_HEIGHT - 64),
        f"由产线排班系统导出 · {confirmed}",
        fill="#91a09c",
        font=font(16),
    )
    page_text = f"第 {page_number} / {total_pages} 页"
    page_font = font(16)
    draw.text(
        (PAGE_WIDTH - MARGIN - _text_width(draw, page_text, page_font), PAGE_HEIGHT - 64),
        page_text,
        fill="#91a09c",
        font=page_font,
    )


def _draw_table_header(
    draw: ImageDraw.ImageDraw,
    y: int,
    columns: list[tuple[str, int]],
) -> int:
    x = MARGIN
    height = 52
    for label, width in columns:
        draw.rectangle((x, y, x + width, y + height), fill=DEEP)
        draw.text((x + 14, y + 13), label, fill=WHITE, font=font(19, True))
        x += width
    return y + height


def _overview_pages(week: dict[str, Any]) -> list[Image.Image]:
    pages: list[Image.Image] = []
    image = _new_page(week, "需求汇总")
    draw = ImageDraw.Draw(image)
    summary = week["summary"]
    cards = [
        ("需求标准工时", f"{summary['total_required_hours']:.2f} h", DEEP),
        ("已排标准工时", f"{summary['scheduled_hours']:.2f} h", GREEN),
        ("剩余缺口", f"{summary['remaining_hours']:.2f} h", RED if summary["remaining_hours"] else GREEN),
        ("排班人数", f"{len(week['employees'])} 人", ORANGE),
    ]
    gap = 20
    card_width = (PAGE_WIDTH - MARGIN * 2 - gap * 3) // 4
    for index, (label, value, color) in enumerate(cards):
        x = MARGIN + index * (card_width + gap)
        draw.rounded_rectangle((x, BODY_TOP, x + card_width, BODY_TOP + 120), radius=16, fill=PALE)
        draw.text((x + 22, BODY_TOP + 18), label, fill=MUTED, font=font(18))
        draw.text((x + 22, BODY_TOP + 53), value, fill=color, font=font(35, True))

    y = BODY_TOP + 165
    draw.text((MARGIN, y), "零件需求完成情况", fill=INK, font=font(28, True))
    y += 52
    columns = [
        ("零件编号", 310),
        ("零件名称", 610),
        ("单件工时", 260),
        ("需求数量", 240),
        ("已排数量", 240),
        ("未排数量", 260),
    ]
    y = _draw_table_header(draw, y, columns)
    row_height = 54
    for demand in week["demands"]:
        if y + row_height > BODY_BOTTOM:
            pages.append(image)
            image = _new_page(week, "需求汇总（续）")
            draw = ImageDraw.Draw(image)
            y = _draw_table_header(draw, BODY_TOP, columns)
        values = [
            demand["part_code"],
            demand["part_name"],
            f"{demand['standard_hours']:.2f} h",
            f"{demand['quantity']} 件",
            f"{demand['assigned_quantity']} 件",
            f"{demand['remaining_quantity']} 件",
        ]
        x = MARGIN
        for column_index, ((_, width), value) in enumerate(zip(columns, values)):
            fill = "#fbfcfa" if (y // row_height) % 2 else WHITE
            draw.rectangle((x, y, x + width, y + row_height), fill=fill, outline=LINE, width=1)
            color = RED if column_index == 5 and demand["remaining_quantity"] else INK
            draw.text(
                (x + 14, y + 14),
                _fit_text(draw, str(value), font(18, column_index == 0), width - 28),
                fill=color,
                font=font(18, column_index == 0),
            )
            x += width
        y += row_height
    pages.append(image)
    return pages


def _schedule_pages(week: dict[str, Any]) -> list[Image.Image]:
    assignments_by_day: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for assignment in week["assignments"]:
        assignments_by_day[(assignment["employee_id"], assignment["work_date"])].append(assignment)

    pages: list[Image.Image] = []
    image = _new_page(week, "员工日排班图")
    draw = ImageDraw.Draw(image)
    y = BODY_TOP
    employee_width = 265
    day_width = (PAGE_WIDTH - MARGIN * 2 - employee_width) // max(1, len(week["days"]))

    def draw_header(current_y: int) -> int:
        x = MARGIN
        draw.rectangle((x, current_y, x + employee_width, current_y + 64), fill=DEEP)
        draw.text((x + 16, current_y + 18), "员工 / 周负荷", fill=WHITE, font=font(19, True))
        x += employee_width
        for day in week["days"]:
            date_text = f"{int(day[5:7])}/{int(day[8:10])}"
            draw.rectangle((x, current_y, x + day_width, current_y + 64), fill=DEEP)
            draw.text((x + 14, current_y + 9), date_text, fill=WHITE, font=font(19, True))
            draw.text((x + 14, current_y + 34), day, fill="#b9d0c9", font=font(15))
            x += day_width
        return current_y + 64

    y = draw_header(y)
    for employee in week["employees"]:
        max_tasks = max(
            (len(assignments_by_day[(employee["id"], day)]) for day in week["days"]),
            default=0,
        )
        row_height = min(480, max(154, 100 + max_tasks * 30))
        if y + row_height > BODY_BOTTOM:
            pages.append(image)
            image = _new_page(week, "员工日排班图（续）")
            draw = ImageDraw.Draw(image)
            y = draw_header(BODY_TOP)
        x = MARGIN
        draw.rectangle((x, y, x + employee_width, y + row_height), fill="#f8faf7", outline=LINE, width=1)
        draw.text((x + 17, y + 18), employee["name"], fill=INK, font=font(23, True))
        employee_type = "固定成员" if employee["employee_type"] == "core" else "候补增援"
        draw.text((x + 17, y + 55), employee_type, fill=MUTED, font=font(16))
        draw.text(
            (x + 17, y + 84),
            f"周 {employee['week_assigned_hours']:.2f} h",
            fill=DEEP,
            font=font(19, True),
        )
        x += employee_width
        days_by_date = {item["date"]: item for item in employee["days"]}
        for day in week["days"]:
            day_load = days_by_date[day]
            unavailable = day_load["availability_hours"] == 0
            draw.rectangle(
                (x, y, x + day_width, y + row_height),
                fill="#f4f5f3" if unavailable else WHITE,
                outline=LINE,
                width=1,
            )
            utilization = day_load["utilization"]
            if utilization <= week["settings"]["green_threshold"]:
                load_color = GREEN
            elif utilization <= week["settings"]["yellow_threshold"]:
                load_color = YELLOW
            else:
                load_color = RED
            load_text = (
                "超载"
                if utilization >= 999
                else f"{round(utilization * 100)}%"
            )
            draw.text(
                (x + 12, y + 12),
                f"{day_load['assigned_hours']:.2f} / {day_load['normal_capacity']:.2f} h",
                fill=MUTED,
                font=font(15),
            )
            percent_width = _text_width(draw, load_text, font(16, True))
            draw.text((x + day_width - percent_width - 12, y + 10), load_text, fill=load_color, font=font(16, True))
            bar_left, bar_top = x + 12, y + 40
            bar_width = day_width - 24
            draw.rounded_rectangle((bar_left, bar_top, bar_left + bar_width, bar_top + 10), radius=5, fill="#e7ece8")
            if utilization > 0:
                draw.rounded_rectangle(
                    (bar_left, bar_top, bar_left + max(7, int(bar_width * min(1, utilization))), bar_top + 10),
                    radius=5,
                    fill=load_color,
                )
            task_y = y + 64
            tasks = assignments_by_day[(employee["id"], day)]
            visible_count = min(len(tasks), 13)
            for assignment in tasks[:visible_count]:
                source = (
                    f"整机:{assignment.get('source_code')} "
                    if assignment.get("order_type") == "machine"
                    else "附件 " if assignment.get("order_type") == "accessory" else ""
                )
                task_text = f"{source}{assignment['part_code']} × {assignment['quantity']}  {assignment['part_name']}"
                draw.text(
                    (x + 12, task_y),
                    _fit_text(draw, task_text, font(15), day_width - 24),
                    fill=INK,
                    font=font(15),
                )
                task_y += 29
            if len(tasks) > visible_count:
                draw.text((x + 12, task_y), f"另有 {len(tasks) - visible_count} 项，见任务清单", fill=ORANGE, font=font(14, True))
            if not tasks:
                draw.text((x + 12, task_y), "不可用" if unavailable else "—", fill="#a5b1ad", font=font(16))
            if day_load.get("overtime_is_manual"):
                overtime_text = (
                    f"人工加班 {day_load['approved_overtime_hours']:.2f} h"
                )
                draw.text((x + 12, y + row_height - 28), overtime_text, fill=RED, font=font(14, True))
            elif day_load["required_overtime_hours"] > 0:
                overtime_text = f"加班 {day_load['required_overtime_hours']:.2f} h"
                draw.text((x + 12, y + row_height - 28), overtime_text, fill=RED, font=font(14, True))
            x += day_width
        y += row_height

    efficiency_row_height = 104
    if y + efficiency_row_height > BODY_BOTTOM:
        pages.append(image)
        image = _new_page(week, "员工日排班图（效率汇总）")
        draw = ImageDraw.Draw(image)
        y = draw_header(BODY_TOP)
    x = MARGIN
    draw.rectangle(
        (x, y, x + employee_width, y + efficiency_row_height),
        fill="#edf5f1",
        outline=LINE,
        width=1,
    )
    draw.text(
        (x + 17, y + 21),
        "每日整体生产效率",
        fill=INK,
        font=font(19, True),
    )
    draw.text(
        (x + 17, y + 55),
        "固定成员已排工时 ÷ 固定成员可用工时",
        fill=MUTED,
        font=font(13),
    )
    x += employee_width
    daily_efficiency = {
        item["date"]: item for item in week.get("daily_efficiency", [])
    }
    low_threshold = week["settings"].get(
        "daily_efficiency_low_threshold", 0.8
    )
    target_threshold = week["settings"].get(
        "daily_efficiency_target_threshold", 0.9
    )
    for day in week["days"]:
        item = daily_efficiency.get(
            day,
            {
                "assigned_hours": 0,
                "available_hours": 0,
                "efficiency": 0,
            },
        )
        available = float(item["available_hours"])
        efficiency = float(item["efficiency"])
        if available <= 0:
            efficiency_color = MUTED
            efficiency_text = "无出勤"
            fill = "#f4f5f3"
        elif efficiency < low_threshold:
            efficiency_color = RED
            efficiency_text = f"{round(efficiency * 100)}%"
            fill = "#fff7f5"
        elif efficiency < target_threshold:
            efficiency_color = YELLOW
            efficiency_text = f"{round(efficiency * 100)}%"
            fill = "#fffbef"
        else:
            efficiency_color = GREEN
            efficiency_text = f"{round(efficiency * 100)}%"
            fill = "#f2f9f5"
        draw.rectangle(
            (x, y, x + day_width, y + efficiency_row_height),
            fill=fill,
            outline=LINE,
            width=1,
        )
        draw.text(
            (x + 12, y + 15),
            f"{item['assigned_hours']:.2f} / {available:.2f} h",
            fill=MUTED,
            font=font(14),
        )
        text_width = _text_width(draw, efficiency_text, font(16, True))
        draw.text(
            (x + day_width - text_width - 12, y + 13),
            efficiency_text,
            fill=efficiency_color,
            font=font(16, True),
        )
        bar_left, bar_top = x + 12, y + 49
        bar_width = day_width - 24
        draw.rounded_rectangle(
            (bar_left, bar_top, bar_left + bar_width, bar_top + 10),
            radius=5,
            fill="#e7ece8",
        )
        if available > 0 and efficiency > 0:
            draw.rounded_rectangle(
                (
                    bar_left,
                    bar_top,
                    bar_left + max(7, int(bar_width * min(1, efficiency))),
                    bar_top + 10,
                ),
                radius=5,
                fill=efficiency_color,
            )
        status_text = (
            "无可用员工"
            if available <= 0
            else "已达标"
            if efficiency >= target_threshold
            else "接近目标"
            if efficiency >= low_threshold
            else "低于预警线"
        )
        draw.text(
            (x + 12, y + 72),
            status_text,
            fill=efficiency_color,
            font=font(13, True),
        )
        x += day_width
    pages.append(image)
    return pages


def _assignment_pages(week: dict[str, Any]) -> list[Image.Image]:
    pages: list[Image.Image] = []
    image = _new_page(week, "任务分配清单")
    draw = ImageDraw.Draw(image)
    columns = [
        ("日期", 260),
        ("员工", 260),
        ("来源", 260),
        ("零件编号", 330),
        ("零件名称", 500),
        ("数量", 180),
        ("标准工时", 270),
    ]
    y = _draw_table_header(draw, BODY_TOP, columns)
    row_height = 52
    assignments = sorted(
        week["assignments"],
        key=lambda item: (item["work_date"], item["employee_name"], item["part_code"]),
    )
    for assignment in assignments:
        if y + row_height > BODY_BOTTOM:
            pages.append(image)
            image = _new_page(week, "任务分配清单（续）")
            draw = ImageDraw.Draw(image)
            y = _draw_table_header(draw, BODY_TOP, columns)
        total_hours = assignment["standard_hours"]
        values = [
            assignment["work_date"],
            assignment["employee_name"],
            (
                f"整机：{assignment.get('source_code')}"
                if assignment.get("order_type") == "machine"
                else "附件订单" if assignment.get("order_type") == "accessory"
                else "历史周需求"
            ),
            assignment["part_code"],
            assignment["part_name"],
            f"{assignment['quantity']} 件",
            f"{total_hours:.2f} h",
        ]
        x = MARGIN
        for (_, width), value in zip(columns, values):
            draw.rectangle((x, y, x + width, y + row_height), fill=WHITE, outline=LINE, width=1)
            draw.text((x + 13, y + 13), _fit_text(draw, str(value), font(17), width - 26), fill=INK, font=font(17))
            x += width
        y += row_height
    if not assignments:
        draw.text((MARGIN + 20, y + 30), "本周没有任务分配记录。", fill=MUTED, font=font(20))
    pages.append(image)
    return pages


def render_schedule(week: dict[str, Any], target: Path, output_format: Literal["pdf", "png"]) -> int:
    pages = _overview_pages(week) + _schedule_pages(week) + _assignment_pages(week)
    for index, page in enumerate(pages, start=1):
        _draw_footer(page, index, len(pages))
    target.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "pdf":
        pages[0].save(
            target,
            "PDF",
            resolution=150.0,
            save_all=True,
            append_images=pages[1:],
        )
    else:
        separator = 24
        full_height = sum(page.height for page in pages) + separator * (len(pages) - 1)
        combined = Image.new("RGB", (PAGE_WIDTH, full_height), "#dfe5df")
        y = 0
        for page in pages:
            combined.paste(page, (0, y))
            y += page.height + separator
        combined.save(target, "PNG", optimize=True)
    return len(pages)
