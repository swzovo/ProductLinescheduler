import { ChangeEvent, FormEvent, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { Modal } from "../components/Modal";
import { Toast } from "../components/Toast";
import type {
  AccessoryOrderImportPreview,
  Machine,
  MachinePlanMatrixPreview,
  Part,
  ProductionOrder,
} from "../types";

function isoLocal(value: Date) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function currentWeek(preferredStart?: string) {
  const today = new Date();
  const monday = preferredStart ? new Date(`${preferredStart}T00:00:00`) : new Date(today);
  if (!preferredStart) monday.setDate(today.getDate() - ((today.getDay() + 6) % 7));
  const friday = new Date(monday);
  friday.setDate(monday.getDate() + 4);
  return { start: isoLocal(monday), end: isoLocal(friday), today: isoLocal(today) };
}

type OrderForm = {
  order_type: "machine" | "accessory";
  source_id: number;
  quantity: number;
  start_date: string;
  end_date: string;
};

export function ProductionOrdersPage({ defaultWeekStart, onOpenSchedule }: { defaultWeekStart?: string; onOpenSchedule: () => void }) {
  const defaults = currentWeek(defaultWeekStart);
  const [orders, setOrders] = useState<ProductionOrder[]>([]);
  const [machines, setMachines] = useState<Machine[]>([]);
  const [parts, setParts] = useState<Part[]>([]);
  const [editing, setEditing] = useState<ProductionOrder | "new" | null>(null);
  const [form, setForm] = useState<OrderForm>({ order_type: "machine", source_id: 0, quantity: 1, start_date: defaults.today, end_date: defaults.today });
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<"all" | "machine" | "accessory">("all");
  const [partSearch, setPartSearch] = useState("");
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [importBusy, setImportBusy] = useState(false);
  const [importPreview, setImportPreview] =
    useState<AccessoryOrderImportPreview | null>(null);
  const [importError, setImportError] = useState<string | null>(null);
  const [templatePath, setTemplatePath] = useState<string | null>(null);
  const [machinePlanImportOpen, setMachinePlanImportOpen] = useState(false);
  const [machinePlanBusy, setMachinePlanBusy] = useState(false);
  const [machinePlanWeek, setMachinePlanWeek] = useState(defaults.start);
  const [machinePlanPreview, setMachinePlanPreview] =
    useState<MachinePlanMatrixPreview | null>(null);
  const [machinePlanError, setMachinePlanError] = useState<string | null>(null);
  const [machinePlanTemplatePath, setMachinePlanTemplatePath] =
    useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const [orderData, machineData, partData] = await Promise.all([
        api<ProductionOrder[]>("/production-orders"),
        api<Machine[]>("/machines"),
        api<Part[]>("/parts"),
      ]);
      setOrders(orderData);
      setMachines(machineData);
      setParts(partData);
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { void load(); }, []);

  const activeMachines = machines.filter((item) => item.active);
  const accessoryParts = parts.filter((item) => item.active && (item.usage_types ?? ["accessory"]).includes("accessory"));
  const normalizedSearch = partSearch.trim().toLocaleLowerCase("zh-CN");
  const filteredAccessoryParts = accessoryParts.filter((item) => !normalizedSearch || `${item.code} ${item.name}`.toLocaleLowerCase("zh-CN").includes(normalizedSearch));
  const sourceOptions = form.order_type === "machine" ? activeMachines : filteredAccessoryParts;
  const selectedMachine = form.order_type === "machine" ? machines.find((item) => item.id === form.source_id) : undefined;
  const shownOrders = useMemo(() => orders.filter((order) => filter === "all" || order.order_type === filter), [orders, filter]);

  const openNew = (type: "machine" | "accessory" = "machine") => {
    setPartSearch("");
    setEditing("new");
    setForm({
      order_type: type,
      source_id: type === "machine" ? activeMachines[0]?.id ?? 0 : 0,
      quantity: 1,
      start_date: defaults.today,
      end_date: defaults.today,
    });
  };

  const openEdit = (order: ProductionOrder) => {
    setPartSearch("");
    setEditing(order);
    setForm({ order_type: order.order_type, source_id: order.source_id, quantity: order.quantity, start_date: order.start_date, end_date: order.end_date });
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    try {
      if (editing === "new") {
        await api("/production-orders", { method: "POST", body: JSON.stringify(form) });
      } else if (editing) {
        await api(`/production-orders/${editing.id}`, {
          method: "PUT",
          body: JSON.stringify({ quantity: form.quantity, start_date: form.start_date, end_date: form.end_date }),
        });
      }
      setEditing(null);
      await load();
      setMessage({ text: "生产任务已保存，请一键生成相关周排班" });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally { setBusy(false); }
  };

  const generate = async () => {
    setBusy(true);
    try {
      const result = await api<{ affected_week_ids: number[] }>("/production-orders/generate", { method: "POST" });
      await load();
      setMessage({ text: `排班已更新，共影响 ${result.affected_week_ids.length} 个周计划` });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally { setBusy(false); }
  };

  const cancel = async (order: ProductionOrder) => {
    if (!window.confirm(`确定取消任务“${order.source_code} · ${order.source_name}”吗？已确认周不会改变。`)) return;
    try {
      await api(`/production-orders/${order.id}`, { method: "DELETE" });
      await load();
      setMessage({ text: "任务已取消；请重新生成排班以释放未确认周产能" });
    } catch (error) { setMessage({ text: (error as Error).message, error: true }); }
  };

  const permanentlyDelete = async (order: ProductionOrder) => {
    if (!window.confirm(`确定永久删除任务“${order.source_code} · ${order.source_name}”吗？未确认周中的自动排班会一并清除，此操作无法恢复。`)) return;
    try {
      await api(`/production-orders/${order.id}/permanent`, { method: "DELETE" });
      await load();
      setMessage({ text: "生产任务已永久删除，相关未确认周排班已重新计算" });
    } catch (error) { setMessage({ text: (error as Error).message, error: true }); }
  };

  const openImport = () => {
    setImportPreview(null);
    setImportError(null);
    setTemplatePath(null);
    setImportOpen(true);
  };

  const saveImportTemplate = async () => {
    setImportBusy(true);
    try {
      const result = await api<{ filename: string; path: string }>(
        "/production-orders/import/template/save",
        { method: "POST" },
      );
      setTemplatePath(result.path);
      setMessage({ text: `附件订单模板已保存：${result.filename}` });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setImportBusy(false);
    }
  };

  const previewImportFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    setImportBusy(true);
    setImportPreview(null);
    setImportError(null);
    try {
      const body = new FormData();
      body.append("file", file);
      setImportPreview(
        await api<AccessoryOrderImportPreview>(
          "/production-orders/import/preview",
          { method: "POST", body },
        ),
      );
    } catch (error) {
      setImportError((error as Error).message);
    } finally {
      setImportBusy(false);
      event.target.value = "";
    }
  };

  const commitImport = async () => {
    if (!importPreview || importPreview.invalid_count) return;
    setImportBusy(true);
    try {
      const result = await api<{ created: number }>(
        "/production-orders/import/commit",
        {
          method: "POST",
          body: JSON.stringify({
            rows: importPreview.rows.map((row) => ({
              part_code: row.part_code,
              quantity: row.quantity,
              start_date: row.start_date,
              end_date: row.end_date,
            })),
          }),
        },
      );
      setImportOpen(false);
      setImportPreview(null);
      await load();
      setMessage({ text: `已导入 ${result.created} 条附件订单，请一键生成相关周排班` });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setImportBusy(false);
    }
  };

  const openMachinePlanImport = () => {
    setMachinePlanWeek(defaults.start);
    setMachinePlanPreview(null);
    setMachinePlanError(null);
    setMachinePlanTemplatePath(null);
    setMachinePlanImportOpen(true);
  };

  const saveMachinePlanTemplate = async () => {
    setMachinePlanBusy(true);
    try {
      const result = await api<{ filename: string; path: string }>(
        "/production-orders/machine-plan-import/template/save",
        { method: "POST" },
      );
      setMachinePlanTemplatePath(result.path);
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setMachinePlanBusy(false);
    }
  };

  const previewMachinePlan = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    setMachinePlanBusy(true);
    setMachinePlanPreview(null);
    setMachinePlanError(null);
    try {
      const body = new FormData();
      body.append("week_start", machinePlanWeek);
      body.append("file", file);
      setMachinePlanPreview(
        await api<MachinePlanMatrixPreview>(
          "/production-orders/machine-plan-import/preview",
          { method: "POST", body },
        ),
      );
    } catch (error) {
      setMachinePlanError((error as Error).message);
    } finally {
      setMachinePlanBusy(false);
      event.target.value = "";
    }
  };

  const commitMachinePlan = async () => {
    if (!machinePlanPreview || machinePlanPreview.invalid_count > 0) return;
    setMachinePlanBusy(true);
    try {
      const result = await api<{ created: number; replaced: number }>(
        "/production-orders/machine-plan-import/commit",
        {
          method: "POST",
          body: JSON.stringify({
            week_start: machinePlanPreview.week_start,
            entries: machinePlanPreview.entries
              .filter((entry) => entry.quantity > 0)
              .map((entry) => ({
                machine_code: entry.machine_code,
                target_date: entry.target_date,
                quantity: entry.quantity,
              })),
          }),
        },
      );
      setMachinePlanImportOpen(false);
      setMachinePlanPreview(null);
      await load();
      setMessage({
        text: `整机周计划已导入：创建 ${result.created} 条，替换旧计划 ${result.replaced} 条；请一键生成排班`,
      });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setMachinePlanBusy(false);
    }
  };

  const totalHours = orders.filter((item) => item.status === "active").reduce((sum, item) => sum + item.required_hours, 0);
  const remainingHours = orders.filter((item) => item.status === "active").reduce((sum, item) => sum + item.remaining_hours, 0);

  return (
    <>
      <section className="section-heading">
        <div><span className="eyebrow">PRODUCTION REQUIREMENTS</span><h2>生产需求与跨周任务</h2><p>整机优先在目标日完成，附件订单从最早空闲日期开始填充。</p></div>
        <div className="heading-actions"><button className="secondary-button" onClick={openMachinePlanImport}>导入整机周计划</button><button className="secondary-button" onClick={openImport}>导入附件表格</button><button className="secondary-button" onClick={() => openNew("accessory")}>＋ 附件订单</button><button className="primary-button" onClick={() => openNew("machine")}>＋ 整机计划</button></div>
      </section>

      <div className="summary-strip">
        <div><span>进行中任务</span><strong>{orders.filter((item) => item.status === "active").length}</strong></div>
        <div><span>计划标准工时</span><strong>{totalHours.toFixed(2)}<small>h</small></strong></div>
        <div><span>尚未排入</span><strong className={remainingHours ? "danger-text" : ""}>{remainingHours.toFixed(2)}<small>h</small></strong></div>
      </div>

      <section className="panel">
        <div className="panel-header production-order-toolbar">
          <div><h3>任务清单</h3><p>日期范围可跨越多个周计划</p></div>
          <div className="heading-actions">
            <select aria-label="筛选任务类型" value={filter} onChange={(event) => setFilter(event.target.value as typeof filter)}><option value="all">全部类型</option><option value="machine">整机计划</option><option value="accessory">附件订单</option></select>
            <button className="primary-button" disabled={busy || !orders.some((item) => item.status === "active")} onClick={() => void generate()}>{busy ? "正在计算…" : "一键生成所有相关周排班"}</button>
            <button className="secondary-button" onClick={onOpenSchedule}>查看周排班</button>
          </div>
        </div>
        {loading ? <div className="loading-block">正在读取生产任务…</div> : shownOrders.length === 0 ? (
          <div className="empty-state"><div className="empty-icon">◫</div><h3>还没有生产任务</h3><p>新增整机计划或附件订单后，系统会自动拆分到相关周。</p></div>
        ) : (
          <div className="order-list">
            {shownOrders.map((order) => (
              <article key={order.id} className={`order-card ${order.status}`}>
                <header>
                  <div><span className={`source-badge ${order.order_type}`}>{order.order_type === "machine" ? "整机" : "附件"}</span><span className="code-chip">{order.source_code}</span><h3>{order.source_name}</h3></div>
                  <span className={`status-pill ${order.schedule_status === "completed" ? "ready" : order.schedule_status === "partial" ? "shortage" : "draft"}`}>{order.status === "cancelled" ? "已取消" : order.schedule_status === "completed" ? "已排完" : order.schedule_status === "partial" ? "部分已排" : "待排班"}</span>
                </header>
                <div className="order-metrics"><span>计划 <b>{order.quantity}</b> {order.order_type === "machine" ? "台" : "件"}</span><span>{order.start_date} → {order.end_date}</span><span>工时 <b>{order.scheduled_hours.toFixed(2)}</b> / {order.required_hours.toFixed(2)}h</span><span className={order.remaining_hours ? "danger-text" : ""}>剩余 {order.remaining_hours.toFixed(2)}h</span></div>
                {order.order_type === "machine" && <div className="order-bom-preview">{order.items.map((item) => <span key={item.id}><b>{item.part_code}</b> × {item.required_quantity}<small>已排 {item.assigned_quantity}</small></span>)}</div>}
                {order.confirmed_conflicts.length > 0 && <div className="confirmed-conflict"><span>日期范围涉及已确认周：{order.confirmed_conflicts.map((item) => item.week_start).join("、")}。已确认排班保持不变，如需调整请先取消确认。</span><button type="button" className="text-button" onClick={onOpenSchedule}>前往周排班取消确认</button></div>}
                {order.needs_generation && <div className="confirmed-conflict">任务尚未同步到周排班，请点击“一键生成所有相关周排班”。</div>}
                <footer>{order.status === "active" && <><button className="text-button" onClick={() => openEdit(order)}>修改数量/日期</button><button className="text-button danger" onClick={() => void cancel(order)}>取消任务</button></>}<button className="text-button danger" onClick={() => void permanentlyDelete(order)}>删除</button></footer>
              </article>
            ))}
          </div>
        )}
      </section>

      {editing && (
        <Modal title={editing === "new" ? "新增生产任务" : "修改生产任务"} width="760px" onClose={() => setEditing(null)}>
          <form className="form-grid" onSubmit={save}>
            {editing === "new" && <label><span>任务类型</span><select value={form.order_type} onChange={(event) => {
              const type = event.target.value as "machine" | "accessory";
              setPartSearch("");
              setForm({ ...form, order_type: type, source_id: type === "machine" ? activeMachines[0]?.id ?? 0 : 0, start_date: defaults.today, end_date: defaults.today });
            }}><option value="machine">整机计划（优先目标日完成）</option><option value="accessory">附件订单（从前往后填充）</option></select></label>}
            {editing === "new" && form.order_type === "accessory" && (
              <fieldset className="full-field skill-picker accessory-part-picker">
                <legend>选择附件零件</legend>
                {accessoryParts.length ? (
                  <>
                    <div className="skill-picker-toolbar">
                      <label className="skill-search">
                        <span>搜索零件编号或名称</span>
                        <input
                          type="search"
                          aria-label="搜索附件零件编号或名称"
                          placeholder="输入编号，例如 140502377"
                          value={partSearch}
                          onChange={(event) => setPartSearch(event.target.value)}
                        />
                      </label>
                      <span className="skill-selection-count">
                        已选择 <b>{form.source_id ? 1 : 0}</b> 项 · 当前显示 {filteredAccessoryParts.length} 项
                      </span>
                    </div>
                    <div className="skill-options">
                      {filteredAccessoryParts.map((part) => {
                        const selected = form.source_id === part.id;
                        return (
                          <label
                            key={part.id}
                            className={selected ? "selected" : ""}
                            title={`${part.code} · ${part.name}`}
                          >
                            <input
                              type="checkbox"
                              aria-label={`${part.code} ${part.name}`}
                              checked={selected}
                              onChange={() => setForm({ ...form, source_id: selected ? 0 : part.id })}
                            />
                            <span className="skill-option-main">
                              <b>{part.code}</b>
                              <em>{part.name}</em>
                            </span>
                            <small>{part.standard_hours.toFixed(2)}h / 件</small>
                          </label>
                        );
                      })}
                      {filteredAccessoryParts.length === 0 && (
                        <div className="skill-search-empty">没有找到匹配的零件，请检查编号或名称。</div>
                      )}
                    </div>
                    <small className="field-note accessory-picker-note">
                      显示所有启用且具备“附件”用途的零件，不要求提前配置员工技能。
                    </small>
                  </>
                ) : (
                  <p className="field-note">暂无启用的附件零件，请先在“零件管理”中添加或修改零件用途。</p>
                )}
              </fieldset>
            )}
            {!(editing === "new" && form.order_type === "accessory") && (
              <label>
                <span>{form.order_type === "machine" ? "选择整机" : "选择附件零件"}</span>
                <select required disabled={editing !== "new"} value={form.source_id} onChange={(event) => setForm({ ...form, source_id: Number(event.target.value) })}>
                  <option value={0}>请选择</option>
                  {sourceOptions.map((item) => <option key={item.id} value={item.id}>{item.code} · {item.name}</option>)}
                </select>
              </label>
            )}
            <label><span>{form.order_type === "machine" ? "整机数量（台）" : "订单数量（件）"}</span><input required type="number" min="1" step="1" value={form.quantity} onChange={(event) => setForm({ ...form, quantity: Math.max(1, Number(event.target.value)) })} /></label>
            <label><span>开始日期</span><input required type="date" value={form.start_date} onChange={(event) => setForm({ ...form, start_date: event.target.value })} /></label>
            <label><span>截止日期</span><input required type="date" min={form.start_date} value={form.end_date} onChange={(event) => setForm({ ...form, end_date: event.target.value })} /></label>
            {selectedMachine && <div className="full-field order-bom-detail"><strong>按当前BOM展开（保存后形成快照）</strong>{selectedMachine.bom_items.map((item) => <span key={item.part_id}><b>{item.part_code}</b> {item.part_name}<em>每台 × {item.quantity_per_machine} · 本任务 {item.quantity_per_machine * form.quantity} 件</em></span>)}</div>}
            <div className="form-actions full-field"><button type="button" className="ghost-button" onClick={() => setEditing(null)}>取消</button><button className="primary-button" disabled={busy || form.source_id === 0} type="submit">保存生产任务</button></div>
          </form>
        </Modal>
      )}
      {importOpen && (
        <Modal
          title="导入附件订单"
          width="920px"
          onClose={() => !importBusy && setImportOpen(false)}
        >
          <div className="import-guide">
            <div>
              <strong>按零件编号批量创建附件订单</strong>
              <p>模板包含零件编号、数量、开始日期和截止日期；每一行创建一条独立订单。</p>
            </div>
            <button
              type="button"
              className="secondary-button"
              disabled={importBusy}
              onClick={() => void saveImportTemplate()}
            >
              {importBusy ? "正在处理…" : "下载Excel模板"}
            </button>
          </div>
          {templatePath && (
            <div className="template-save-success">
              <strong>模板保存成功</strong>
              <span>{templatePath}</span>
            </div>
          )}
          <label className="file-drop">
            <input
              type="file"
              accept=".xlsx,.csv"
              disabled={importBusy}
              onChange={previewImportFile}
            />
            <span>{importBusy ? "正在读取并校验…" : "选择 .xlsx 或 .csv 文件"}</span>
            <small>文件不超过5MB，最多5000条附件订单</small>
          </label>
          {importError && (
            <div className="import-format-error" role="alert">
              <strong>表格格式有误，暂时无法导入</strong>
              <p>{importError}</p>
            </div>
          )}
          {importPreview && (
            <>
              <div className="import-summary">
                <span>共 <b>{importPreview.total_rows}</b> 行</span>
                <span className="success-text">可导入 <b>{importPreview.valid_count}</b></span>
                <span className={importPreview.invalid_count ? "danger-text" : ""}>
                  错误 <b>{importPreview.invalid_count}</b>
                </span>
              </div>
              {importPreview.invalid_count > 0 && (
                <div className="import-format-error" role="alert">
                  <strong>存在格式错误，整批尚未导入</strong>
                  <p>
                    {importPreview.rows
                      .filter((row) => row.errors.length)
                      .slice(0, 4)
                      .map((row) => `第${row.row_number}行：${row.errors.join("、")}`)
                      .join("；")}
                  </p>
                </div>
              )}
              <div className="table-wrap import-preview-table">
                <table>
                  <thead>
                    <tr>
                      <th>行号</th>
                      <th>零件编号</th>
                      <th>零件名称</th>
                      <th>数量</th>
                      <th>开始日期</th>
                      <th>截止日期</th>
                      <th>校验结果</th>
                    </tr>
                  </thead>
                  <tbody>
                    {importPreview.rows.map((row) => (
                      <tr key={row.row_number} className={row.errors.length ? "error-row" : ""}>
                        <td>{row.row_number}</td>
                        <td><span className="code-chip">{row.part_code || "—"}</span></td>
                        <td>{row.part_name || "—"}</td>
                        <td>{row.quantity || "—"}</td>
                        <td>{row.start_date || "—"}</td>
                        <td>{row.end_date || "—"}</td>
                        <td>
                          {row.errors.length ? (
                            <span className="danger-text">{row.errors.join("；")}</span>
                          ) : (
                            <span className="success-text">校验通过</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="form-actions">
                <button type="button" className="ghost-button" onClick={() => setImportOpen(false)}>取消</button>
                <button
                  type="button"
                  className="primary-button"
                  disabled={importBusy || importPreview.invalid_count > 0}
                  onClick={() => void commitImport()}
                >
                  {importPreview.invalid_count
                    ? "请先修正表格错误"
                    : `确认导入 ${importPreview.valid_count} 条`}
                </button>
              </div>
            </>
          )}
        </Modal>
      )}
      {machinePlanImportOpen && (
        <Modal
          title="导入整机周计划"
          width="980px"
          onClose={() => !machinePlanBusy && setMachinePlanImportOpen(false)}
        >
          <div className="import-guide">
            <div>
              <strong>按星期和机型批量建立每日整机任务</strong>
              <p>空白或0表示当天没有该机型；再次导入同一周会完整替换此前通过周计划表导入的任务。</p>
            </div>
            <button type="button" className="secondary-button" disabled={machinePlanBusy} onClick={() => void saveMachinePlanTemplate()}>
              下载Excel模板
            </button>
          </div>
          {machinePlanTemplatePath && (
            <div className="template-save-success">
              <strong>模板保存成功</strong>
              <span>{machinePlanTemplatePath}</span>
            </div>
          )}
          <label>
            <span>目标周周一</span>
            <input
              type="date"
              value={machinePlanWeek}
              disabled={machinePlanBusy}
              onChange={(event) => {
                setMachinePlanWeek(event.target.value);
                setMachinePlanPreview(null);
                setMachinePlanError(null);
              }}
            />
          </label>
          <label className="file-drop">
            <input type="file" accept=".xlsx" disabled={machinePlanBusy || !machinePlanWeek} onChange={previewMachinePlan} />
            <span>{machinePlanBusy ? "正在读取并校验…" : "选择 .xlsx 周计划文件"}</span>
            <small>星期一至星期日映射到所选周；今天以前的非零计划会报错</small>
          </label>
          {machinePlanError && (
            <div className="import-format-error" role="alert">
              <strong>表格格式有误，暂时无法导入</strong>
              <p>{machinePlanError}</p>
            </div>
          )}
          {machinePlanPreview && (
            <>
              <div className="import-summary">
                <span>目标周 <b>{machinePlanPreview.week_start}</b></span>
                <span className="success-text">非零任务 <b>{machinePlanPreview.nonzero_count}</b></span>
                <span className={machinePlanPreview.invalid_count ? "danger-text" : ""}>错误单元格 <b>{machinePlanPreview.invalid_count}</b></span>
              </div>
              <div className="table-wrap import-preview-table">
                <table>
                  <thead><tr><th>日期</th><th>整机编号</th><th>整机名称</th><th>数量</th><th>校验结果</th></tr></thead>
                  <tbody>
                    {machinePlanPreview.entries
                      .filter((entry) => entry.quantity > 0 || entry.errors.length > 0)
                      .map((entry) => (
                        <tr key={`${entry.target_date}-${entry.machine_code}`} className={entry.errors.length ? "error-row" : ""}>
                          <td>{entry.target_date}</td>
                          <td><span className="code-chip">{entry.machine_code}</span></td>
                          <td>{entry.machine_name || "—"}</td>
                          <td>{entry.quantity}</td>
                          <td>{entry.errors.length ? <span className="danger-text">{entry.errors.join("；")}</span> : <span className="success-text">校验通过</span>}</td>
                        </tr>
                      ))}
                    {machinePlanPreview.nonzero_count === 0 && machinePlanPreview.invalid_count === 0 && (
                      <tr><td colSpan={5}>本表没有非零任务；提交后会清空该周此前导入的整机计划。</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
              <div className="form-actions">
                <button type="button" className="ghost-button" onClick={() => setMachinePlanImportOpen(false)}>取消</button>
                <button type="button" className="primary-button" disabled={machinePlanBusy || machinePlanPreview.invalid_count > 0} onClick={() => void commitMachinePlan()}>
                  {machinePlanPreview.invalid_count ? "请先修正表格错误" : "确认替换该周导入计划"}
                </button>
              </div>
            </>
          )}
        </Modal>
      )}
      {message && <Toast message={message.text} kind={message.error ? "error" : "success"} onClose={() => setMessage(null)} />}
    </>
  );
}
