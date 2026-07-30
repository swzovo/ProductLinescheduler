import { FormEvent, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { Modal } from "../components/Modal";
import { Toast } from "../components/Toast";
import type {
  Assignment,
  Candidate,
  Employee,
  Part,
  WeekDetail,
  WeekEmployee,
  WeekListItem,
  WeekStatus,
} from "../types";

const STATUS: Record<WeekStatus, string> = {
  draft: "草稿",
  shortage: "缺口待处理",
  ready: "可确认",
  confirmed: "已确认",
};

const weekday = (value: string) =>
  new Intl.DateTimeFormat("zh-CN", { weekday: "short" }).format(
    new Date(`${value}T00:00:00`),
  );
const shortDate = (value: string) => `${Number(value.slice(5, 7))}/${Number(value.slice(8, 10))}`;

function nextMonday() {
  const now = new Date();
  const distance = (8 - now.getDay()) % 7 || 7;
  now.setDate(now.getDate() + distance);
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function loadClass(utilization: number, green: number, yellow: number) {
  if (utilization <= green) return "green";
  if (utilization <= yellow) return "yellow";
  return "red";
}

function efficiencyClass(efficiency: number, low: number, target: number) {
  if (efficiency < low) return "red";
  if (efficiency < target) return "yellow";
  return "green";
}

export function WeekPlanner({ onOpenProduction }: { onOpenProduction: (weekStart?: string) => void }) {
  const [weeks, setWeeks] = useState<WeekListItem[]>([]);
  const [parts, setParts] = useState<Part[]>([]);
  const [allEmployees, setAllEmployees] = useState<Employee[]>([]);
  const [week, setWeek] = useState<WeekDetail | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState<"employee" | "part">("employee");
  const [newWeekOpen, setNewWeekOpen] = useState(false);
  const [newWeekDate, setNewWeekDate] = useState(nextMonday());
  const [demandOpen, setDemandOpen] = useState(false);
  const [demandValues, setDemandValues] = useState<Record<number, number>>({});
  const [shortageOpen, setShortageOpen] = useState(false);
  const [resolutionMode, setResolutionMode] = useState<"reinforcement" | "overtime">("overtime");
  const [selectedCandidates, setSelectedCandidates] = useState<number[]>([]);
  const [assignmentOpen, setAssignmentOpen] = useState<Assignment | "new" | null>(null);
  const [assignmentForm, setAssignmentForm] = useState({
    employee_id: 0,
    part_id: 0,
    order_item_id: null as number | null,
    work_date: "",
    target_date: "",
    quantity: 1,
  });
  const [availabilityEmployee, setAvailabilityEmployee] = useState<WeekEmployee | null>(null);
  const [leavePickerOpen, setLeavePickerOpen] = useState(false);
  const [leaveEmployee, setLeaveEmployee] = useState<WeekEmployee | null>(null);
  const [leaveDates, setLeaveDates] = useState<string[]>([]);
  const [leaveUseOvertime, setLeaveUseOvertime] = useState(true);
  const [leaveUseWeekend, setLeaveUseWeekend] = useState(true);
  const [availabilityValues, setAvailabilityValues] = useState<Record<string, number>>({});
  const [overtimeManualValues, setOvertimeManualValues] = useState<Record<string, boolean>>({});
  const [exportBusy, setExportBusy] = useState<"pdf" | "png" | null>(null);
  const [exportPath, setExportPath] = useState<string | null>(null);
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(null);
  const overtimeBlockHours =
    week?.settings.overtime_block_hours ?? week?.settings.overtime_limit ?? 4;

  const loadLists = async (preferredId?: number) => {
    const [weekData, partData, employeeData] = await Promise.all([
      api<WeekListItem[]>("/weeks"),
      api<Part[]>("/parts"),
      api<Employee[]>("/employees"),
    ]);
    setWeeks(weekData);
    setParts(partData);
    setAllEmployees(employeeData);
    const target = preferredId ?? selectedId ?? weekData[0]?.id ?? null;
    setSelectedId(target);
    if (target) setWeek(await api<WeekDetail>(`/weeks/${target}`));
    else setWeek(null);
  };

  useEffect(() => {
    loadLists()
      .catch((error) => setMessage({ text: error.message, error: true }))
      .finally(() => setLoading(false));
    // Initial data load should only run once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectWeek = async (id: number) => {
    setSelectedId(id);
    setLoading(true);
    try {
      setWeek(await api<WeekDetail>(`/weeks/${id}`));
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setLoading(false);
    }
  };

  const refresh = async (showShortage = false) => {
    if (!selectedId) return;
    const result = await api<WeekDetail>(`/weeks/${selectedId}`);
    setWeek(result);
    const list = await api<WeekListItem[]>("/weeks");
    setWeeks(list);
    if (showShortage && result.summary.remaining_hours > 0) {
      const recommendation =
        result.shortage.suggestion === "reinforcement" ? "reinforcement" : "overtime";
      setResolutionMode(recommendation);
      setSelectedCandidates([]);
      setShortageOpen(true);
    }
  };

  const createWeek = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    try {
      const result = await api<WeekDetail>("/weeks", {
        method: "POST",
        body: JSON.stringify({
          week_start: newWeekDate,
          include_weekend: true,
          demands: [],
        }),
      });
      setSelectedId(result.id);
      setWeek(result);
      setNewWeekOpen(false);
      setDemandValues({});
      await loadLists(result.id);
      setMessage({ text: "周计划已创建，请在生产需求中录入整机计划或附件订单" });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  };

  const openDemands = () => {
    if (!week) return;
    setDemandValues(
      Object.fromEntries(week.demands.map((item) => [item.part_id, item.quantity])),
    );
    setDemandOpen(true);
  };

  const saveDemands = async (event: FormEvent) => {
    event.preventDefault();
    if (!week) return;
    setBusy(true);
    try {
      const result = await api<WeekDetail>(`/weeks/${week.id}`, {
        method: "PUT",
        body: JSON.stringify({
          include_weekend: true,
          demands: Object.entries(demandValues)
            .filter(([, quantity]) => Number(quantity) > 0)
            .map(([partId, quantity]) => ({
              part_id: Number(partId),
              quantity: Number(quantity),
            })),
        }),
      });
      setWeek(result);
      setDemandOpen(false);
      await refresh();
      setMessage({ text: "本周生产需求已保存，原排班草案已清空" });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  };

  const generate = async () => {
    if (!week) return;
    setBusy(true);
    try {
      const result = await api<WeekDetail>(`/weeks/${week.id}/generate`, {
        method: "POST",
      });
      setWeek(result);
      await refresh(true);
      setMessage({
        text:
          result.summary.remaining_hours > 0
            ? "正常工时草案已生成，请处理剩余任务"
            : "正常工时排班草案已生成",
      });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  };

  const candidates: Candidate[] = week
    ? resolutionMode === "reinforcement"
      ? week.shortage.reinforcement_candidates
      : week.shortage.overtime_candidates
    : [];

  const resolve = async () => {
    if (!week || !selectedCandidates.length) return;
    setBusy(true);
    try {
      const result = await api<WeekDetail>(`/weeks/${week.id}/resolve`, {
        method: "POST",
        body: JSON.stringify({
          mode: resolutionMode,
          employee_ids: selectedCandidates,
        }),
      });
      setWeek(result);
      setShortageOpen(false);
      await refresh(result.summary.remaining_hours > 0);
      setMessage({
        text:
          result.summary.remaining_hours > 0
            ? "已按所选人员重排，仍有任务缺口"
            : resolutionMode === "reinforcement"
              ? "增援人员已加入并完成重排"
              : "加班方案已生成并记录",
      });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  };

  const openAssignment = (assignment?: Assignment) => {
    if (!week) return;
    if (assignment) {
      setAssignmentOpen(assignment);
      setAssignmentForm({
        employee_id: assignment.employee_id,
        part_id: assignment.part_id,
        order_item_id: assignment.order_item_id ?? null,
        work_date: assignment.work_date,
        target_date: assignment.target_date,
        quantity: assignment.quantity,
      });
    } else {
      const firstSource = week.demands[0]?.sources?.[0];
      const firstWorkDate = week.days[0] ?? "";
      setAssignmentOpen("new");
      setAssignmentForm({
        employee_id: week.employees[0]?.id ?? 0,
        part_id: week.demands[0]?.part_id ?? 0,
        order_item_id: firstSource?.order_item_id ?? null,
        work_date: firstWorkDate,
        target_date:
          firstSource?.order_type === "machine" &&
          firstSource.start_date > firstWorkDate
            ? firstSource.start_date
            : firstWorkDate,
        quantity: 1,
      });
    }
  };

  const saveAssignments = async (
    next: { employee_id: number; part_id: number; order_item_id?: number | null; work_date: string; target_date?: string; quantity: number }[],
  ) => {
    if (!week) return;
    const result = await api<WeekDetail>(`/weeks/${week.id}/assignments`, {
      method: "PUT",
      body: JSON.stringify({ assignments: next }),
    });
    setWeek(result);
    await refresh();
  };

  const saveAssignment = async (event: FormEvent) => {
    event.preventDefault();
    if (!week || !assignmentOpen) return;
    setBusy(true);
    try {
      const base = week.assignments
        .filter((item) => assignmentOpen === "new" || item.id !== assignmentOpen.id)
        .map((item) => ({
          employee_id: item.employee_id,
          part_id: item.part_id,
          order_item_id: item.order_item_id ?? null,
          work_date: item.work_date,
          target_date: item.target_date,
          quantity: item.quantity,
        }));
      await saveAssignments([...base, assignmentForm]);
      setAssignmentOpen(null);
      setMessage({ text: "任务分配已更新，工时与缺口已重新计算" });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  };

  const deleteAssignment = async (assignment: Assignment) => {
    if (!week || !window.confirm(`移除 ${assignment.employee_name} 的“${assignment.part_name}”任务？`))
      return;
    setBusy(true);
    try {
      await saveAssignments(
        week.assignments
          .filter((item) => item.id !== assignment.id)
          .map((item) => ({
            employee_id: item.employee_id,
            part_id: item.part_id,
            order_item_id: item.order_item_id ?? null,
            work_date: item.work_date,
            target_date: item.target_date,
            quantity: item.quantity,
          })),
      );
      setMessage({ text: "任务已移除" });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  };

  const openAvailability = (employee: WeekEmployee) => {
    setAvailabilityEmployee(employee);
    setAvailabilityValues(
      Object.fromEntries(employee.days.map((day) => [day.date, day.availability_hours])),
    );
    setOvertimeManualValues(
      Object.fromEntries(employee.days.map((day) => [day.date, day.approved_overtime_hours > 0])),
    );
  };

  const openLeaveAdjustment = (employee: WeekEmployee) => {
    setLeavePickerOpen(false);
    setLeaveEmployee(employee);
    setLeaveDates([]);
    setLeaveUseOvertime(true);
    setLeaveUseWeekend(true);
  };

  const saveAvailability = async (event: FormEvent) => {
    event.preventDefault();
    if (!week || !availabilityEmployee) return;
    setBusy(true);
    try {
      const result = await api<WeekDetail>(
        `/weeks/${week.id}/availability`,
        {
          method: "PUT",
          body: JSON.stringify({
            entries: week.days.map((day) => ({
              employee_id: availabilityEmployee.id,
              work_date: day,
              hours: availabilityValues[day] ?? 0,
            })),
            overtime_entries: week.days.map((day) => ({
              employee_id: availabilityEmployee.id,
              work_date: day,
              hours: overtimeManualValues[day] ? overtimeBlockHours : 0,
              manual: overtimeManualValues[day] ?? false,
            })),
          }),
        },
      );
      setWeek(result);
      setAvailabilityEmployee(null);
      await refresh();
      setMessage({ text: "本周逐日出勤与加班设置已保存，请重新生成排班" });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  };

  const createLeaveAdjustment = async (event: FormEvent) => {
    event.preventDefault();
    if (!week || !leaveEmployee) return;
    if (leaveDates.length === 0) {
      setMessage({ text: "请至少选择一个有任务的请假日期", error: true });
      return;
    }
    setBusy(true);
    try {
      const result = await api<WeekDetail>(
        `/weeks/${week.id}/leave-adjustments`,
        {
          method: "POST",
          body: JSON.stringify({
            employee_id: leaveEmployee.id,
            leave_dates: leaveDates,
            use_overtime: leaveUseOvertime,
            use_weekend: leaveUseWeekend,
          }),
        },
      );
      setWeek(result);
      setLeaveEmployee(null);
      await refresh();
      setMessage({
        text:
          result.summary.remaining_hours > 0
            ? "请假已登记，但仍有任务无法在目标期限内完成；可取消后调整补班方式"
            : "请假已登记；整机任务已按优先员工转派或提前生产，附件任务已由本人补做",
        error: result.summary.remaining_hours > 0,
      });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  };

  const cancelLeaveAdjustment = async () => {
    if (
      !week ||
      !window.confirm("取消本次请假调整并完整恢复调整前的已确认排班吗？")
    )
      return;
    setBusy(true);
    try {
      const result = await api<WeekDetail>(
        `/weeks/${week.id}/leave-adjustments/cancel`,
        { method: "POST" },
      );
      setWeek(result);
      await refresh();
      setMessage({ text: "已取消请假调整，并恢复原已确认排班" });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  };

  const approveOvertime = async () => {
    if (!week) return;
    setBusy(true);
    try {
      const result = await api<WeekDetail>(`/weeks/${week.id}/approve-overtime`, {
        method: "POST",
      });
      setWeek(result);
      await refresh();
      setMessage({ text: "人工调整产生的加班工时已批准" });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    if (!week || !window.confirm("确认后本周排班将锁定，不能继续修改。是否确认？")) return;
    setBusy(true);
    try {
      const result = await api<WeekDetail>(`/weeks/${week.id}/confirm`, {
        method: "POST",
      });
      setWeek(result);
      await refresh();
      setMessage({ text: "周排班已确认并锁定" });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  };

  const unconfirm = async () => {
    if (
      !week ||
      !window.confirm("取消确认后将保留当前排班，并恢复需求、任务和出勤编辑。是否继续？")
    )
      return;
    setBusy(true);
    try {
      const result = await api<WeekDetail>(`/weeks/${week.id}/unconfirm`, {
        method: "POST",
      });
      setWeek(result);
      await refresh();
      setMessage({ text: "已取消确认，当前排班已恢复编辑" });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  };

  const resetSchedule = async () => {
    if (
      !week ||
      !window.confirm(
        "一键清除将删除本周任务分配、加班批准和临时增援人员；生产需求与出勤设置会保留。是否继续？",
      )
    )
      return;
    setBusy(true);
    try {
      const result = await api<WeekDetail>(`/weeks/${week.id}/reset`, {
        method: "POST",
      });
      setWeek(result);
      await refresh();
      setMessage({ text: "本周排班已清除，可以重新生成草案" });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  };

  const exportSchedule = async (format: "pdf" | "png") => {
    if (!week || week.status !== "confirmed") return;
    setExportBusy(format);
    setExportPath(null);
    try {
      const result = await api<{
        filename: string;
        path: string;
        format: "pdf" | "png";
        page_count: number;
      }>(`/weeks/${week.id}/export`, {
        method: "POST",
        body: JSON.stringify({ format }),
      });
      setExportPath(result.path);
      setMessage({
        text: `${format === "pdf" ? "PDF" : "图片"}已保存：${result.filename}`,
      });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setExportBusy(null);
    }
  };

  const assignmentsByDay = useMemo(() => {
    const result = new Map<string, Assignment[]>();
    week?.assignments.forEach((assignment) => {
      const key = `${assignment.employee_id}-${assignment.work_date}`;
      result.set(key, [...(result.get(key) ?? []), assignment]);
    });
    return result;
  }, [week]);

  const coveredPartIds = useMemo(
    () =>
      new Set(
        allEmployees
          .filter((employee) => employee.active)
          .flatMap((employee) => employee.skill_part_ids),
      ),
    [allEmployees],
  );
  const demandParts = useMemo(
    () =>
      parts.filter(
        (part) =>
          (part.active && coveredPartIds.has(part.id)) ||
          Number(demandValues[part.id] ?? 0) > 0,
      ),
    [coveredPartIds, demandValues, parts],
  );
  const assignmentSources = useMemo(
    () =>
      (week?.demands ?? []).flatMap((demand) =>
        (demand.sources ?? []).map((source) => ({
          ...source,
          part_id: demand.part_id,
          part_code: demand.part_code,
          part_name: demand.part_name,
        })),
      ),
    [week],
  );
  const selectedAssignmentSource = assignmentSources.find(
    (source) => source.order_item_id === assignmentForm.order_item_id,
  );

  if (loading) return <div className="loading-page"><span /><p>正在载入排班数据…</p></div>;

  return (
    <>
      <section className="schedule-toolbar">
        <div>
          <span className="eyebrow">WEEKLY PRODUCTION PLAN</span>
          <div className="week-select-row">
            <select
              value={selectedId ?? ""}
              onChange={(event) => void selectWeek(Number(event.target.value))}
              aria-label="选择周计划"
            >
              {weeks.length === 0 && <option value="">尚无周计划</option>}
              {weeks.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.week_start} 开始 · {STATUS[item.status]}
                </option>
              ))}
            </select>
            {week && <span className={`status-pill ${week.status}`}>{STATUS[week.status]}</span>}
          </div>
        </div>
        <button className="primary-button" onClick={() => setNewWeekOpen(true)}>
          <span>＋</span> 新建周计划
        </button>
      </section>

      {!week ? (
        <section className="panel empty-state hero-empty">
          <div className="empty-icon">▦</div>
          <h2>从第一个周计划开始</h2>
          <p>请先在零件管理和员工管理中准备基础资料，然后录入本周生产需求。</p>
          <button className="primary-button" onClick={() => setNewWeekOpen(true)}>新建周计划</button>
        </section>
      ) : (
        <>
          <section className="summary-strip">
            <div><span>本周需求工时</span><strong>{week.summary.total_required_hours.toFixed(2)}<small>h</small></strong></div>
            <div><span>已排标准工时</span><strong>{week.summary.scheduled_hours.toFixed(2)}<small>h</small></strong></div>
            <div className={week.summary.remaining_hours > 0 ? "attention" : ""}>
              <span>剩余缺口</span><strong>{week.summary.remaining_hours.toFixed(2)}<small>h</small></strong>
            </div>
            <div><span>本周排班人数</span><strong>{week.employees.length}<small>人</small></strong></div>
          </section>

          <section className="action-ribbon">
            <div>
              <button
                className="secondary-button"
                onClick={() => onOpenProduction(week.week_start)}
              >
                管理生产需求
              </button>
              <button
                className="primary-button"
                onClick={() => void generate()}
                disabled={busy || week.status === "confirmed" || Boolean(week.active_adjustment) || week.demands.length === 0}
              >
                {busy ? "计算中…" : "重新计算相关周排班"}
              </button>
              {week.summary.remaining_hours > 0 && !week.active_adjustment && (
                <button className="warning-button" onClick={() => {
                  setResolutionMode(week.shortage.suggestion === "reinforcement" ? "reinforcement" : "overtime");
                  setSelectedCandidates([]);
                  setShortageOpen(true);
                }}>
                  处理 {week.summary.remaining_hours.toFixed(2)}h 缺口
                </button>
              )}
              {week.summary.unapproved_overload && (
                <button className="warning-button" onClick={() => void approveOvertime()}>
                  批准人工调整加班
                </button>
              )}
            </div>
            <div className="schedule-final-actions">
              {week.status === "confirmed" ? (
                <>
                  <button
                    className="secondary-button"
                    disabled={exportBusy !== null}
                    onClick={() => void exportSchedule("pdf")}
                  >
                    {exportBusy === "pdf" ? "正在生成…" : "导出 PDF"}
                  </button>
                  <button
                    className="secondary-button"
                    disabled={exportBusy !== null}
                    onClick={() => void exportSchedule("png")}
                  >
                    {exportBusy === "png" ? "正在生成…" : "导出图片"}
                  </button>
                  <button
                    className="warning-button"
                    disabled={busy || exportBusy !== null}
                    onClick={() => void unconfirm()}
                  >
                    取消确认
                  </button>
                  <button
                    className="primary-button"
                    disabled={busy || exportBusy !== null}
                    onClick={() => setLeavePickerOpen(true)}
                  >
                    请假调整
                  </button>
                </>
              ) : (
                <>
                  <button
                    className="ghost-button danger"
                    disabled={busy || !week.summary.has_schedule_data}
                    onClick={() => void resetSchedule()}
                  >
                    一键清除排班
                  </button>
                  <button
                    className="confirm-button"
                    disabled={week.status !== "ready" || busy}
                    onClick={() => void confirm()}
                  >
                    确认本周排班
                  </button>
                </>
              )}
            </div>
          </section>

          {week.active_adjustment && (
            <section className="confirmed-conflict adjustment-banner">
              <span>
                正在处理
                {week.employees.find((employee) => employee.id === week.active_adjustment?.employee_id)?.name ?? "该员工"}
                的请假（{week.active_adjustment.leave_dates?.map(shortDate).join("、")}）：
                整机任务按员工1→员工2→员工3在当天转派，必要时向前回排；附件任务仍由本人补做。
              </span>
              <button
                type="button"
                className="text-button danger"
                disabled={busy}
                onClick={() => void cancelLeaveAdjustment()}
              >
                取消调整并恢复原排班
              </button>
            </section>
          )}

          {exportPath && week.status === "confirmed" && (
            <section className="export-save-success">
              <strong>导出成功，文件已保存到下载文件夹</strong>
              <span>{exportPath}</span>
            </section>
          )}

          {week.summary.remaining_hours > 0 && (
            <section className="shortage-banner">
              <div className="warning-symbol">!</div>
              <div>
                <strong>
                  {week.active_adjustment
                    ? "请假调整后仍有任务无法按期完成"
                    : week.shortage.suggestion === "reinforcement"
                    ? "建议安排候补人员增援"
                    : week.shortage.suggestion === "overtime"
                      ? "建议由合格员工加班完成"
                      : "当前人员与技能无法覆盖全部任务"}
                </strong>
                <p>
                  剩余 {week.summary.remaining_hours.toFixed(2)} 标准工时。
                  {!week.active_adjustment && ` 半周判断阈值为 ${week.summary.shortage_threshold.toFixed(3)} 小时。`}
                  {week.active_adjustment && " 整机已按优先级尝试转派和提前生产，附件已尝试由本人补做；可取消本次调整后重新选择补班方式。"}
                  {week.shortage.missing_skill_parts.length > 0 &&
                    ` 未覆盖零件：${week.shortage.missing_skill_parts.map((item) => `${item.part_code} · ${item.part_name}`).join("、")}。`}
                </p>
              </div>
              {!week.active_adjustment && <button onClick={() => setShortageOpen(true)}>查看人选</button>}
            </section>
          )}

          <section className="panel schedule-panel">
            <div className="panel-header">
              <div>
                <h3>周排班明细</h3>
                <p>周一至周日均可逐人设置；默认周一至周五出勤，点击员工姓名调整本周具体日期。</p>
              </div>
              <div className="view-switch">
                <button className={view === "employee" ? "active" : ""} onClick={() => setView("employee")}>按员工</button>
                <button className={view === "part" ? "active" : ""} onClick={() => setView("part")}>按零件</button>
              </div>
            </div>

            {view === "employee" ? (
              week.employees.length === 0 ? (
                <div className="empty-state"><p>请先创建启用的固定成员。</p></div>
              ) : (
                <div className="schedule-grid-wrap">
                  <div className="schedule-grid" style={{ gridTemplateColumns: `190px repeat(${week.days.length}, minmax(155px, 1fr))` }}>
                    <div className="grid-head sticky-col">员工 / 日负荷</div>
                    {week.days.map((day) => (
                      <div className="grid-head" key={day}>
                        <b>{weekday(day)}</b><span>{shortDate(day)}</span>
                      </div>
                    ))}
                    {week.employees.map((employee) => (
                      <EmployeeScheduleRow
                        key={employee.id}
                        employee={employee}
                        week={week}
                        assignmentsByDay={assignmentsByDay}
                        editable={week.status !== "confirmed" && !week.active_adjustment}
                        onEmployee={() => openAvailability(employee)}
                        onAssignment={openAssignment}
                        onDelete={deleteAssignment}
                      />
                    ))}
                    <DailyEfficiencyRow week={week} />
                  </div>
                </div>
              )
            ) : (
              <div className="table-wrap">
                <table>
                  <thead><tr><th>零件</th><th>单件工时</th><th>需求</th><th>已排</th><th>未排</th><th>任务来源</th><th>完成率</th></tr></thead>
                  <tbody>
                    {week.demands.map((demand) => {
                      const ratio = demand.quantity ? demand.assigned_quantity / demand.quantity : 1;
                      return (
                        <tr key={demand.part_id}>
                          <td><span className="code-chip">{demand.part_code}</span> <strong>{demand.part_name}</strong></td>
                          <td>{demand.standard_hours.toFixed(2)}h</td>
                          <td>{demand.quantity} 件</td>
                          <td>{demand.assigned_quantity} 件</td>
                          <td className={demand.remaining_quantity ? "danger-text" : ""}>{demand.remaining_quantity} 件</td>
                          <td>
                            <div className="demand-source-list">
                              {(demand.sources ?? []).map((source) => (
                                <span key={source.order_item_id} className={`source-badge ${source.order_type}`}>
                                  {source.order_type === "machine" ? `整机：${source.source_code}` : `附件：${source.source_code}`}
                                </span>
                              ))}
                              {!demand.sources?.length && <span>历史周需求</span>}
                            </div>
                          </td>
                          <td><div className="mini-progress"><i style={{ width: `${Math.min(100, ratio * 100)}%` }} /><span>{Math.round(ratio * 100)}%</span></div></td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
            {week.status !== "confirmed" && !week.active_adjustment && week.employees.length > 0 && week.demands.length > 0 && (
              <div className="panel-footer">
                <button className="secondary-button" onClick={() => openAssignment()}>＋ 手工添加任务</button>
              </div>
            )}
          </section>
        </>
      )}

      {newWeekOpen && (
        <Modal title="新建周计划" onClose={() => setNewWeekOpen(false)}>
          <form className="form-grid" onSubmit={createWeek}>
            <label className="full-field">
              <span>周开始日期（周一）</span>
              <input type="date" required value={newWeekDate} onChange={(event) => setNewWeekDate(event.target.value)} />
              <small>每个周一只能创建一份周计划。</small>
            </label>
            <div className="form-actions full-field">
              <button type="button" className="ghost-button" onClick={() => setNewWeekOpen(false)}>取消</button>
              <button className="primary-button" disabled={busy} type="submit">创建并录入需求</button>
            </div>
          </form>
        </Modal>
      )}

      {demandOpen && week && (
        <Modal title="编辑本周生产需求" width="760px" onClose={() => setDemandOpen(false)}>
          <form onSubmit={saveDemands}>
            <div className="demand-editor">
              <div className="demand-head"><span>零件（仅显示技能覆盖项）</span><span>单件工时</span><span>需求数量</span></div>
              {demandParts.map((part) => (
                <label key={part.id} className={!coveredPartIds.has(part.id) ? "uncovered-demand" : ""}>
                  <span>
                    <b>{part.code}</b>{part.name}
                    {!coveredPartIds.has(part.id) && <small>当前没有启用员工掌握该零件，请归零或先配置技能</small>}
                  </span>
                  <em>{part.standard_hours.toFixed(2)}h</em>
                  <input
                    type="number"
                    min="0"
                    step="1"
                    value={demandValues[part.id] ?? 0}
                    onChange={(event) => setDemandValues({ ...demandValues, [part.id]: Math.max(0, Number(event.target.value)) })}
                  />
                </label>
              ))}
              {demandParts.length === 0 && (
                <div className="field-note">没有被启用员工技能覆盖的零件，请先在员工管理中配置可制作零件。</div>
              )}
            </div>
            <div className="form-actions">
              <button type="button" className="ghost-button" onClick={() => setDemandOpen(false)}>取消</button>
              <button className="primary-button" disabled={busy} type="submit">保存生产需求</button>
            </div>
          </form>
        </Modal>
      )}

      {shortageOpen && week && (
        <Modal title="人工处理任务缺口" width="760px" onClose={() => setShortageOpen(false)}>
          <div className="resolution-summary">
            <span>尚未安排</span><strong>{week.summary.remaining_hours.toFixed(2)}h</strong>
            <p>系统只提供合格候选人，由你决定本周实际加入或加班的人员。</p>
          </div>
          {week.shortage.missing_skill_parts.length > 0 && (
            <section className="missing-skill-summary">
              <strong>当前员工未覆盖的零件</strong>
              <div>
                {week.shortage.missing_skill_parts.map((part) => (
                  <span key={part.part_id}>
                    <b>{part.part_code}</b>
                    {part.part_name}
                    <small>未排 {part.remaining_quantity} 件</small>
                  </span>
                ))}
              </div>
              <p>可先到员工管理补充技能，或在下方选择具备对应技能的候补人员。</p>
            </section>
          )}
          <div className="mode-tabs">
            <button className={resolutionMode === "reinforcement" ? "active" : ""} onClick={() => { setResolutionMode("reinforcement"); setSelectedCandidates([]); }}>
              候补增援
              {week.shortage.suggestion === "reinforcement" && <small>系统建议</small>}
            </button>
            <button className={resolutionMode === "overtime" ? "active" : ""} onClick={() => { setResolutionMode("overtime"); setSelectedCandidates([]); }}>
              安排加班
              {week.shortage.suggestion === "overtime" && <small>系统建议</small>}
            </button>
          </div>
          <div className="candidate-list">
            {candidates.length ? candidates.map((candidate) => (
              <label key={candidate.employee_id} className={selectedCandidates.includes(candidate.employee_id) ? "selected" : ""}>
                <input
                  type="checkbox"
                  checked={selectedCandidates.includes(candidate.employee_id)}
                  onChange={() => setSelectedCandidates((items) => items.includes(candidate.employee_id) ? items.filter((id) => id !== candidate.employee_id) : [...items, candidate.employee_id])}
                />
                <div className="avatar small">{candidate.name.slice(0, 1)}</div>
                <span><b>{candidate.name}</b><small>覆盖：{candidate.coverage_parts.join("、")}</small></span>
                <em>最多 {candidate.available_capacity.toFixed(1)}h</em>
              </label>
            )) : (
              <div className="empty-candidates">没有符合当前缺口技能要求的可选人员。</div>
            )}
          </div>
          <div className="form-actions">
            <button className="ghost-button" onClick={() => setShortageOpen(false)}>稍后处理</button>
            <button className="primary-button" disabled={!selectedCandidates.length || busy} onClick={() => void resolve()}>
              {busy ? "正在重新计算…" : "使用所选人员重新排班"}
            </button>
          </div>
        </Modal>
      )}

      {assignmentOpen && week && (
        <Modal title={assignmentOpen === "new" ? "手工添加任务" : "调整任务分配"} onClose={() => setAssignmentOpen(null)}>
          <form className="form-grid" onSubmit={saveAssignment}>
            <label>
              <span>员工</span>
              <select value={assignmentForm.employee_id} onChange={(event) => setAssignmentForm({ ...assignmentForm, employee_id: Number(event.target.value) })}>
                {week.employees.map((employee) => <option key={employee.id} value={employee.id}>{employee.name}</option>)}
              </select>
            </label>
            <label>
              <span>日期</span>
              <select value={assignmentForm.work_date} onChange={(event) => {
                const workDate = event.target.value;
                setAssignmentForm({
                  ...assignmentForm,
                  work_date: workDate,
                  target_date:
                    selectedAssignmentSource?.order_type === "machine"
                      ? assignmentForm.target_date < workDate
                        ? workDate
                        : assignmentForm.target_date
                      : workDate,
                });
              }}>
                {week.days.map((day) => <option key={day} value={day}>{day} · {weekday(day)}</option>)}
              </select>
            </label>
            {assignmentSources.length > 0 ? (
              <label>
                <span>任务来源与零件</span>
                <select value={assignmentForm.order_item_id ?? ""} onChange={(event) => {
                  const source = assignmentSources.find((item) => item.order_item_id === Number(event.target.value));
                  if (source) {
                    const machineTarget =
                      assignmentForm.work_date > source.start_date
                        ? assignmentForm.work_date
                        : source.start_date;
                    setAssignmentForm({
                      ...assignmentForm,
                      order_item_id: source.order_item_id,
                      part_id: source.part_id,
                      target_date:
                        source.order_type === "machine"
                          ? machineTarget
                          : assignmentForm.work_date,
                    });
                  }
                }}>
                  {assignmentSources.map((source) => <option key={source.order_item_id} value={source.order_item_id}>{source.order_type === "machine" ? "整机" : "附件"}：{source.source_code} · {source.part_code} {source.part_name}</option>)}
                </select>
              </label>
            ) : (
              <label>
                <span>零件</span>
                <select value={assignmentForm.part_id} onChange={(event) => setAssignmentForm({ ...assignmentForm, part_id: Number(event.target.value), order_item_id: null })}>
                  {week.demands.map((demand) => <option key={demand.part_id} value={demand.part_id}>{demand.part_code} · {demand.part_name}</option>)}
                </select>
              </label>
            )}
            {selectedAssignmentSource?.order_type === "machine" && (
              <label>
                <span>目标完成日期</span>
                <input
                  type="date"
                  min={
                    assignmentForm.work_date >
                    selectedAssignmentSource.start_date
                      ? assignmentForm.work_date
                      : selectedAssignmentSource.start_date
                  }
                  max={selectedAssignmentSource.end_date}
                  value={assignmentForm.target_date}
                  onChange={(event) =>
                    setAssignmentForm({
                      ...assignmentForm,
                      target_date: event.target.value,
                    })
                  }
                />
              </label>
            )}
            <label>
              <span>数量（整数件）</span>
              <input type="number" min="1" step="1" value={assignmentForm.quantity} onChange={(event) => setAssignmentForm({ ...assignmentForm, quantity: Math.max(1, Number(event.target.value)) })} />
            </label>
            <p className="field-note full-field">保存时会校验员工技能、任务目标日期、本周需求总量以及每日正常工时与加班上限。</p>
            <div className="form-actions full-field">
              <button type="button" className="ghost-button" onClick={() => setAssignmentOpen(null)}>取消</button>
              <button className="primary-button" disabled={busy} type="submit">保存任务</button>
            </div>
          </form>
        </Modal>
      )}

      {leavePickerOpen && week && (
        <Modal
          title="选择临时请假员工"
          width="620px"
          onClose={() => setLeavePickerOpen(false)}
        >
          <div className="leave-employee-list">
            {week.employees.map((employee) => (
              <button
                type="button"
                key={employee.id}
                onClick={() => openLeaveAdjustment(employee)}
              >
                <span className="avatar small">{employee.name.slice(0, 1)}</span>
                <span>
                  <b>{employee.name}</b>
                  <small>
                    {employee.employee_type === "core" ? "固定成员" : "候补增援"} ·
                    本周 {employee.week_assigned_hours.toFixed(2)}h
                  </small>
                </span>
              </button>
            ))}
          </div>
          <p className="field-note">
            选择员工后点击有任务的请假日期。系统会保存原确认版本：整机任务优先当天转派给同零件的员工2、员工3，附件任务由请假员工本人补做。
          </p>
        </Modal>
      )}

      {leaveEmployee && week && (
        <Modal
          title={`请假调整 · ${leaveEmployee.name}`}
          width="760px"
          onClose={() => setLeaveEmployee(null)}
        >
          <form onSubmit={createLeaveAdjustment}>
            <div className="leave-date-picker">
              {leaveEmployee.days.map((day) => {
                const hasTask = day.assigned_hours > 0;
                const selected = leaveDates.includes(day.date);
                return (
                  <button
                    key={day.date}
                    type="button"
                    disabled={!hasTask}
                    className={selected ? "selected" : ""}
                    onClick={() =>
                      setLeaveDates(
                        selected
                          ? leaveDates.filter((item) => item !== day.date)
                          : [...leaveDates, day.date].sort(),
                      )
                    }
                  >
                    <b>{weekday(day.date)}</b>
                    <span>{shortDate(day.date)}</span>
                    <small>{hasTask ? `${day.assigned_hours.toFixed(2)}h任务` : "无任务"}</small>
                    <em>{selected ? "已选请假" : "点击请假"}</em>
                  </button>
                );
              })}
            </div>
            <div className="leave-recovery-options">
              <label>
                <input
                  type="checkbox"
                  checked={leaveUseOvertime}
                  onChange={(event) => setLeaveUseOvertime(event.target.checked)}
                />
                <span>
                  <b>允许本人固定加班</b>
                  <small>需要时按每次{overtimeBlockHours}小时，从本周后面的日期向前安排。</small>
                </span>
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={leaveUseWeekend}
                  onChange={(event) => setLeaveUseWeekend(event.target.checked)}
                />
                <span>
                  <b>允许本人周末补班</b>
                  <small>周六、周日按系统每日理论工时开放，只用于本人补做。</small>
                </span>
              </label>
            </div>
            <p className="field-note">
              整机任务会先在请假当天按员工优先级转派，仍不足时向前回排；附件任务先使用本人其他日期的空余时间，再使用你允许的固定加班或周末。
            </p>
            <div className="form-actions">
              <button type="button" className="ghost-button" onClick={() => setLeaveEmployee(null)}>取消</button>
              <button type="submit" className="primary-button" disabled={busy || leaveDates.length === 0}>
                确定请假并重新安排
              </button>
            </div>
          </form>
        </Modal>
      )}

      {availabilityEmployee && week && (
        <Modal title={`设置 ${availabilityEmployee.name} 的本周出勤与加班`} width="760px" onClose={() => {
          setAvailabilityEmployee(null);
        }}>
          <form onSubmit={saveAvailability}>
            <div className="availability-editor">
              <div className="availability-editor-head">
                <span>日期</span>
                <span>正常出勤</span>
                <span>固定加班班次</span>
              </div>
              {week.days.map((day) => (
                <div className="availability-editor-row" key={day}>
                  <span><b>{weekday(day)}</b><small>{day}</small></span>
                  <div className="input-with-unit">
                    <input aria-label={`${day} 正常出勤工时`} type="number" min="0" max="24" step="0.25" value={availabilityValues[day] ?? 0} onChange={(event) => setAvailabilityValues({ ...availabilityValues, [day]: Number(event.target.value) })} />
                    <b>小时</b>
                  </div>
                  <div className="daily-overtime-control">
                    <label className={overtimeManualValues[day] ? "overtime-block-toggle selected" : "overtime-block-toggle"}>
                      <input
                        type="checkbox"
                        aria-label={`${day} 加班${overtimeBlockHours}小时`}
                        checked={overtimeManualValues[day] ?? false}
                        onChange={(event) => setOvertimeManualValues({
                        ...overtimeManualValues,
                        [day]: event.target.checked,
                      })}
                      />
                      <span>
                        {overtimeManualValues[day]
                          ? `加班 ${overtimeBlockHours} 小时`
                          : "不加班"}
                      </span>
                    </label>
                  </div>
                </div>
              ))}
            </div>
            <p className="field-note">
              这里仅调整当前周：周一至周日可分别填写出勤时间；加班只能勾选完整的{overtimeBlockHours}小时班次。
            </p>
            <div className="form-actions">
              <button type="button" className="ghost-button" onClick={() => {
                setAvailabilityEmployee(null);
              }}>取消</button>
              <button className="primary-button" disabled={busy} type="submit">
                保存本周出勤与加班
              </button>
            </div>
          </form>
        </Modal>
      )}

      {message && <Toast message={message.text} kind={message.error ? "error" : "success"} onClose={() => setMessage(null)} />}
    </>
  );
}

function EmployeeScheduleRow({
  employee,
  week,
  assignmentsByDay,
  editable,
  onEmployee,
  onAssignment,
  onDelete,
}: {
  employee: WeekEmployee;
  week: WeekDetail;
  assignmentsByDay: Map<string, Assignment[]>;
  editable: boolean;
  onEmployee: () => void;
  onAssignment: (assignment: Assignment) => void;
  onDelete: (assignment: Assignment) => void;
}) {
  return (
    <>
      <button className="employee-grid-label sticky-col" onClick={editable ? onEmployee : undefined}>
        <div className="avatar small">{employee.name.slice(0, 1)}</div>
        <span><b>{employee.name}</b><small>{employee.employee_type === "core" ? "固定成员" : "候补增援"} · 周 {employee.week_assigned_hours.toFixed(1)}h</small></span>
        {editable && <em>编辑</em>}
      </button>
      {employee.days.map((day) => {
        const assignments = assignmentsByDay.get(`${employee.id}-${day.date}`) ?? [];
        const color = loadClass(
          day.utilization,
          week.settings.green_threshold,
          week.settings.yellow_threshold,
        );
        return (
          <div className={`day-cell ${day.availability_hours === 0 ? "unavailable" : ""}`} key={day.date}>
            <div className="load-caption">
              <span>{day.assigned_hours.toFixed(2)} / {day.normal_capacity.toFixed(2)}h</span>
              <b className={color}>{day.utilization >= 999 ? "超载" : `${Math.round(day.utilization * 100)}%`}</b>
            </div>
            <div
              className={`load-bar ${color}`}
              title={`标准工时 ${day.assigned_hours.toFixed(2)}h；预计实际出勤 ${day.estimated_actual_hours.toFixed(2)}h；${day.overtime_is_manual ? "人工设置加班" : "系统批准加班"} ${day.approved_overtime_hours.toFixed(2)}h`}
            >
              <i style={{ width: `${Math.min(100, day.utilization * 100)}%` }} />
            </div>
            {day.overtime_is_manual ? (
              <span className="overtime-note">人工加班 {day.approved_overtime_hours.toFixed(2)}h</span>
            ) : day.required_overtime_hours > 0 && (
              <span className="overtime-note">加班 {day.required_overtime_hours.toFixed(2)}h</span>
            )}
            <div className="task-stack">
              {assignments.map((assignment) => (
                <div className="task-chip" key={assignment.id}>
                  <button disabled={!editable} onClick={() => onAssignment(assignment)}>
                    {assignment.order_type && (
                      <em className={`source-badge ${assignment.order_type}`}>
                        {assignment.order_type === "machine" ? `整机：${assignment.source_code}` : "附件订单"}
                      </em>
                    )}
                    <b>{assignment.part_code}</b>
                    <span>{assignment.part_name} × {assignment.quantity}</span>
                    {assignment.order_type === "machine" &&
                      assignment.target_date !== assignment.work_date && (
                        <small>目标日 {assignment.target_date}</small>
                      )}
                  </button>
                  {editable && <button className="remove-task" onClick={() => void onDelete(assignment)} aria-label="移除任务">×</button>}
                </div>
              ))}
              {!assignments.length && <span className="no-task">{day.availability_hours === 0 ? "不可用" : "—"}</span>}
            </div>
          </div>
        );
      })}
    </>
  );
}

function DailyEfficiencyRow({ week }: { week: WeekDetail }) {
  const values = week.daily_efficiency ?? week.days.map((date) => {
    const employeeDays = week.employees
      .filter((employee) => employee.employee_type === "core")
      .map((employee) => employee.days.find((day) => day.date === date))
      .filter((day): day is NonNullable<typeof day> => Boolean(day));
    const assignedHours = employeeDays.reduce((sum, day) => sum + day.assigned_hours, 0);
    const availableHours = employeeDays.reduce((sum, day) => sum + day.availability_hours, 0);
    return {
      date,
      assigned_hours: assignedHours,
      available_hours: availableHours,
      efficiency: availableHours > 0 ? assignedHours / availableHours : 0,
    };
  });

  return (
    <>
      <div className="daily-efficiency-label sticky-col">
        <b>每日整体生产效率</b>
        <small>固定成员已排工时 ÷ 固定成员可用工时</small>
      </div>
      {values.map((day) => {
        const color = efficiencyClass(
          day.efficiency,
          week.settings.daily_efficiency_low_threshold,
          week.settings.daily_efficiency_target_threshold,
        );
        const hasAvailability = day.available_hours > 0;
        return (
          <div className={`daily-efficiency-cell ${hasAvailability ? color : "neutral"}`} key={day.date}>
            <div className="load-caption">
              <span>{day.assigned_hours.toFixed(2)} / {day.available_hours.toFixed(2)}h</span>
              <b className={hasAvailability ? color : ""}>
                {hasAvailability ? `${Math.round(day.efficiency * 100)}%` : "无出勤"}
              </b>
            </div>
            <div
              className={`load-bar ${hasAvailability ? color : ""}`}
              title={`当天固定成员已排标准工时 ${day.assigned_hours.toFixed(2)}h；固定成员可用工时 ${day.available_hours.toFixed(2)}h；整体生产效率 ${hasAvailability ? `${(day.efficiency * 100).toFixed(1)}%` : "无可用工时"}；候补增援不参与计算`}
            >
              <i style={{ width: `${Math.min(100, day.efficiency * 100)}%` }} />
            </div>
            <small className="efficiency-status">
              {!hasAvailability
                ? "当天没有可用员工"
                : color === "green"
                  ? "已达标"
                  : color === "yellow"
                    ? "接近目标"
                    : "低于预警线"}
            </small>
          </div>
        );
      })}
    </>
  );
}
