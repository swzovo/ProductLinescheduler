export type EmployeeType = "core" | "backup";
export type WeekStatus = "draft" | "shortage" | "ready" | "confirmed";

export interface Settings {
  daily_hours: number;
  efficiency: number;
  overtime_limit: number;
  overtime_block_hours: number;
  shortage_threshold: number;
  green_threshold: number;
  yellow_threshold: number;
  daily_efficiency_low_threshold: number;
  daily_efficiency_target_threshold: number;
}

export interface Part {
  id: number;
  code: string;
  name: string;
  standard_hours: number;
  usage_types: ("accessory" | "assembly")[];
  active: boolean;
  level_1_employee_id: number | null;
  level_2_employee_id: number | null;
  level_3_employee_id: number | null;
  level_1_employee?: {
    employee_id: number;
    employee_name: string;
    priority_level: 1;
  } | null;
  level_2_employee?: {
    employee_id: number;
    employee_name: string;
    priority_level: 2;
  } | null;
  level_3_employee?: {
    employee_id: number;
    employee_name: string;
    priority_level: 3;
  } | null;
}

export interface PartImportRow {
  row_number: number;
  code: string;
  name: string;
  standard_hours: number;
  active: boolean;
  usage_types?: ("accessory" | "assembly")[];
  employee_names: string[];
  employee_level1_names: string[];
  employee_level2_names: string[];
  employee_level3_names: string[];
  action: "create" | "update";
  errors: string[];
}

export interface MachineBomItem {
  part_id: number;
  part_code: string;
  part_name: string;
  standard_hours: number;
  quantity_per_machine: number;
  part_active: boolean;
  part_is_assembly: boolean;
}

export interface Machine {
  id: number;
  code: string;
  name: string;
  active: boolean;
  bom_items: MachineBomItem[];
}

export interface MachineBomMatrixPreview {
  filename: string;
  total_machines: number;
  valid_count: number;
  invalid_count: number;
  machines: {
    column: number;
    code: string;
    name: string;
    action: "create" | "update";
    existing_active: boolean;
    bom_items: {
      part_id: number;
      part_code: string;
      part_name: string;
      quantity_per_machine: number;
    }[];
    errors: string[];
  }[];
}

export interface MachinePlanMatrixPreview {
  filename: string;
  week_start: string;
  total_cells: number;
  nonzero_count: number;
  invalid_count: number;
  entries: {
    row_number: number;
    column: number;
    weekday: number;
    target_date: string;
    machine_code: string;
    machine_id: number | null;
    machine_name: string;
    quantity: number;
    errors: string[];
  }[];
}

export interface ProductionOrderItem {
  id: number;
  part_id: number;
  part_code: string;
  part_name: string;
  standard_hours: number;
  quantity_per_unit: number;
  required_quantity: number;
  assigned_quantity: number;
  remaining_quantity: number;
}

export interface ProductionOrder {
  id: number;
  order_type: "machine" | "accessory";
  source_id: number;
  source_code: string;
  source_name: string;
  quantity: number;
  start_date: string;
  end_date: string;
  status: "active" | "cancelled" | "legacy";
  origin: "manual" | "accessory_import" | "machine_plan_import" | "legacy";
  import_week_start?: string | null;
  needs_generation: boolean;
  schedule_status: "completed" | "partial" | "unscheduled";
  required_hours: number;
  scheduled_hours: number;
  remaining_hours: number;
  remaining_quantity: number;
  confirmed_conflicts: { week_id: number; week_start: string }[];
  items: ProductionOrderItem[];
}

export interface PartImportPreview {
  filename: string;
  total_rows: number;
  valid_count: number;
  invalid_count: number;
  create_count: number;
  update_count: number;
  new_employee_count: number;
  new_employee_names: string[];
  rows: PartImportRow[];
}

export interface AccessoryOrderImportRow {
  row_number: number;
  part_code: string;
  part_id: number | null;
  part_name: string;
  quantity: number;
  start_date: string;
  end_date: string;
  errors: string[];
}

export interface AccessoryOrderImportPreview {
  filename: string;
  total_rows: number;
  valid_count: number;
  invalid_count: number;
  rows: AccessoryOrderImportRow[];
}

export interface Employee {
  id: number;
  name: string;
  employee_type: EmployeeType;
  overtime_limit: number | null;
  weekly_work_days: number;
  unavailable_weekdays: number[];
  active: boolean;
  skill_part_ids: number[];
  skill_priorities?: { part_id: number; priority_level: 1 | 2 | 3 }[];
}

export interface WeekListItem {
  id: number;
  week_start: string;
  include_weekend: boolean;
  status: WeekStatus;
  required_hours: number;
}

export interface WeekDemand {
  id: number;
  part_id: number;
  part_code: string;
  part_name: string;
  standard_hours: number;
  quantity: number;
  assigned_quantity: number;
  remaining_quantity: number;
  sources?: {
    order_item_id: number;
    production_order_id: number;
    order_type: "machine" | "accessory";
    start_date: string;
    end_date: string;
    source_code: string;
    source_name: string;
    quantity: number;
    assigned_quantity: number;
  }[];
}

export interface DayLoad {
  date: string;
  availability_hours: number;
  normal_capacity: number;
  assigned_hours: number;
  estimated_actual_hours: number;
  utilization: number;
  approved_overtime_hours: number;
  overtime_is_manual: boolean;
  required_overtime_hours: number;
}

export interface WeekEmployee extends Employee {
  days: DayLoad[];
  week_assigned_hours: number;
}

export interface DailyEfficiency {
  date: string;
  assigned_hours: number;
  available_hours: number;
  efficiency: number;
}

export interface Assignment {
  id: number;
  employee_id: number;
  employee_name: string;
  part_id: number;
  order_item_id?: number | null;
  production_order_id?: number | null;
  order_type?: "machine" | "accessory" | null;
  source_code?: string | null;
  source_name?: string | null;
  part_code: string;
  part_name: string;
  work_date: string;
  target_date: string;
  quantity: number;
  standard_hours: number;
  source: "generated" | "manual";
}

export interface Candidate {
  employee_id: number;
  name: string;
  employee_type: EmployeeType;
  coverage_part_ids: number[];
  coverage_parts: string[];
  available_capacity: number;
}

export interface WeekDetail {
  id: number;
  week_start: string;
  include_weekend: boolean;
  status: WeekStatus;
  confirmed_at: string | null;
  active_adjustment: {
    id: number;
    status: "active";
    created_at: string;
    employee_id?: number;
    leave_dates?: string[];
    use_overtime?: boolean;
    use_weekend?: boolean;
    released_quantity?: number;
  } | null;
  settings: Settings;
  days: string[];
  demands: WeekDemand[];
  employees: WeekEmployee[];
  daily_efficiency: DailyEfficiency[];
  assignments: Assignment[];
  summary: {
    total_required_hours: number;
    scheduled_hours: number;
    remaining_hours: number;
    shortage_threshold: number;
    unapproved_overload: boolean;
    has_schedule_data: boolean;
  };
  shortage: {
    suggestion: "reinforcement" | "overtime" | "no_capacity" | "no_skill" | null;
    missing_skill_parts: {
      part_id: number;
      part_code: string;
      part_name: string;
      remaining_quantity: number;
    }[];
    reinforcement_candidates: Candidate[];
    overtime_candidates: Candidate[];
  };
}
