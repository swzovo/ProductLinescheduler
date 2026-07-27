from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class SettingsUpdate(BaseModel):
    daily_hours: float = Field(gt=0, le=24)
    efficiency: float = Field(gt=0, le=1)
    overtime_limit: float = Field(ge=0, le=12)
    overtime_block_hours: float = Field(default=4.0, gt=0, le=12)
    shortage_threshold: float = Field(ge=0, le=10000)
    green_threshold: float = Field(gt=0, le=1)
    yellow_threshold: float = Field(gt=0, le=2)
    daily_efficiency_low_threshold: float = Field(ge=0, le=2)
    daily_efficiency_target_threshold: float = Field(gt=0, le=2)

    @field_validator("yellow_threshold")
    @classmethod
    def validate_yellow(cls, value: float, info):
        green = info.data.get("green_threshold")
        if green is not None and value <= green:
            raise ValueError("黄色阈值必须大于绿色阈值")
        return value

    @model_validator(mode="after")
    def validate_daily_efficiency_thresholds(self):
        if (
            self.daily_efficiency_target_threshold
            <= self.daily_efficiency_low_threshold
        ):
            raise ValueError("整体生产效率达标阈值必须大于预警阈值")
        return self


class PartSkillPriorityInput(BaseModel):
    employee_id: int
    priority_level: Literal[1, 2]


class PartCreate(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=100)
    standard_hours: float = Field(gt=0, le=1000)
    usage_types: list[Literal["accessory", "assembly"]] = ["accessory"]
    employee_priorities: list[PartSkillPriorityInput] | None = None
    level_1_employee_id: int | None = None
    level_2_employee_id: int | None = None
    active: bool = True

    @field_validator("usage_types")
    @classmethod
    def validate_usage_types(cls, value: list[str]):
        normalized = list(dict.fromkeys(value))
        if not normalized:
            raise ValueError("零件至少选择一种用途")
        return normalized

    @field_validator("employee_priorities")
    @classmethod
    def validate_employee_priorities(
        cls,
        value: list[PartSkillPriorityInput] | None,
    ):
        if value is None:
            return value
        ids = [item.employee_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("同一员工不能同时设置为员工1和员工2")
        return value

    @model_validator(mode="after")
    def validate_priority_slots(self):
        if (
            self.level_1_employee_id is not None
            and self.level_1_employee_id == self.level_2_employee_id
        ):
            raise ValueError("同一员工不能同时设置为员工1和员工2")
        if self.employee_priorities is not None:
            by_level = [item.priority_level for item in self.employee_priorities]
            if len(by_level) != len(set(by_level)):
                raise ValueError("员工1和员工2每级最多选择一名员工")
        return self


class PartUpdate(PartCreate):
    pass


class PartImportItem(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=100)
    standard_hours: float = Field(gt=0, le=1000)
    usage_types: list[Literal["accessory", "assembly"]] | None = None
    active: bool | None = None
    employee_names: list[str] = Field(default_factory=list, max_length=200)
    employee_level1_names: list[str] = Field(default_factory=list, max_length=1)
    employee_level2_names: list[str] = Field(default_factory=list, max_length=1)

    @field_validator("employee_names")
    @classmethod
    def validate_employee_names(cls, value: list[str]):
        normalized = list(dict.fromkeys(name.strip() for name in value if name.strip()))
        if any(len(name) > 100 for name in normalized):
            raise ValueError("员工姓名不能超过100个字符")
        return normalized

    @field_validator("employee_level1_names", "employee_level2_names")
    @classmethod
    def validate_priority_employee_names(cls, value: list[str]):
        normalized = list(dict.fromkeys(name.strip() for name in value if name.strip()))
        if any(len(name) > 100 for name in normalized):
            raise ValueError("员工姓名不能超过100个字符")
        if len(normalized) > 1:
            raise ValueError("员工1和员工2每格只能填写一名员工")
        return normalized

    @model_validator(mode="after")
    def validate_priority_names(self):
        overlap = set(self.employee_level1_names) & set(self.employee_level2_names)
        if overlap:
            raise ValueError(
                f"员工不能同时属于员工1和员工2：{'、'.join(sorted(overlap))}"
            )
        return self


class PartImportCommit(BaseModel):
    rows: list[PartImportItem] = Field(min_length=1, max_length=5000)


class MachineBomInput(BaseModel):
    part_id: int
    quantity_per_machine: int = Field(gt=0, le=100000)


class MachineCreate(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=100)
    active: bool = True
    bom_items: list[MachineBomInput] = Field(min_length=1, max_length=1000)

    @field_validator("bom_items")
    @classmethod
    def validate_unique_bom(cls, value: list[MachineBomInput]):
        ids = [item.part_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("同一整机不能重复选择相同零件")
        return value


class MachineUpdate(MachineCreate):
    pass


class ProductionOrderCreate(BaseModel):
    order_type: Literal["machine", "accessory"]
    source_id: int
    quantity: int = Field(gt=0, le=10000000)
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_dates(self):
        if self.end_date < self.start_date:
            raise ValueError("截止日期不能早于开始日期")
        return self


class ProductionOrderUpdate(BaseModel):
    quantity: int = Field(gt=0, le=10000000)
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_dates(self):
        if self.end_date < self.start_date:
            raise ValueError("截止日期不能早于开始日期")
        return self


class AccessoryOrderImportItem(BaseModel):
    part_code: str = Field(min_length=1, max_length=40)
    quantity: int = Field(gt=0, le=10000000)
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_dates(self):
        if self.end_date < self.start_date:
            raise ValueError("截止日期不能早于开始日期")
        return self


class AccessoryOrderImportCommit(BaseModel):
    rows: list[AccessoryOrderImportItem] = Field(min_length=1, max_length=5000)


class EmployeeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    employee_type: Literal["core", "backup"]
    overtime_limit: float | None = Field(default=None, ge=0, le=12)
    weekly_work_days: int = Field(default=5, ge=1, le=7)
    unavailable_weekdays: list[int] = Field(default_factory=lambda: [5, 6])
    active: bool = True
    skill_part_ids: list[int] = []

    @field_validator("unavailable_weekdays")
    @classmethod
    def validate_weekdays(cls, value: list[int]):
        if len(value) != len(set(value)) or any(day < 0 or day > 6 for day in value):
            raise ValueError("不可上班星期必须是周一至周日且不能重复")
        return sorted(value)

    @model_validator(mode="after")
    def validate_working_day_count(self):
        if self.weekly_work_days > 7 - len(self.unavailable_weekdays):
            raise ValueError("每周工作天数不能超过可上班星期的数量")
        return self


class EmployeeUpdate(EmployeeCreate):
    pass


class DemandInput(BaseModel):
    part_id: int
    quantity: int = Field(ge=0)


class WeekCreate(BaseModel):
    week_start: date
    include_weekend: bool = False
    demands: list[DemandInput] = []

    @field_validator("week_start")
    @classmethod
    def monday_only(cls, value: date):
        if value.weekday() != 0:
            raise ValueError("周计划开始日期必须是周一")
        return value


class WeekUpdate(BaseModel):
    include_weekend: bool
    demands: list[DemandInput]


class WeekCalendarUpdate(BaseModel):
    include_weekend: bool


class AvailabilityInput(BaseModel):
    employee_id: int
    work_date: date
    hours: float = Field(ge=0, le=24)


class OvertimeAvailabilityInput(BaseModel):
    employee_id: int
    work_date: date
    hours: float = Field(ge=0, le=12)
    manual: bool = False


class AvailabilityUpdate(BaseModel):
    entries: list[AvailabilityInput]
    overtime_entries: list[OvertimeAvailabilityInput] = Field(default_factory=list)


class LeaveAdjustmentCreate(BaseModel):
    # entries 为3.0旧界面兼容字段；新界面直接提交员工和请假日期。
    entries: list[AvailabilityInput] = Field(default_factory=list, max_length=100)
    overtime_entries: list[OvertimeAvailabilityInput] = Field(default_factory=list)
    employee_id: int | None = None
    leave_dates: list[date] = Field(default_factory=list, max_length=7)
    use_overtime: bool = True
    use_weekend: bool = True

    @model_validator(mode="after")
    def validate_leave_selection(self):
        if self.employee_id is None and not self.entries:
            raise ValueError("请选择请假员工和日期")
        if self.employee_id is not None and not self.leave_dates:
            raise ValueError("请至少选择一个请假日期")
        if len(self.leave_dates) != len(set(self.leave_dates)):
            raise ValueError("请假日期不能重复")
        return self


class ResolveShortage(BaseModel):
    mode: Literal["reinforcement", "overtime"]
    employee_ids: list[int] = Field(min_length=1)


class ScheduleExport(BaseModel):
    format: Literal["pdf", "png"]


class AssignmentInput(BaseModel):
    employee_id: int
    part_id: int
    order_item_id: int | None = None
    work_date: date
    quantity: int = Field(gt=0)


class AssignmentsUpdate(BaseModel):
    assignments: list[AssignmentInput]
