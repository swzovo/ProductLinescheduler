import { ChangeEvent, FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import { Modal } from "../components/Modal";
import { Toast } from "../components/Toast";
import type { Employee, Part, PartImportPreview } from "../types";

const EMPTY_FORM = {
  code: "",
  name: "",
  standard_hours: 0.5,
  usage_types: ["accessory"] as ("accessory" | "assembly")[],
  level_1_employee_id: null as number | null,
  level_2_employee_id: null as number | null,
  active: true,
};

export function PartsPage() {
  const [parts, setParts] = useState<Part[]>([]);
  const [employees, setEmployees] = useState<Employee[]>([]);
  const [editing, setEditing] = useState<Part | null | "new">(null);
  const [form, setForm] = useState(EMPTY_FORM);
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [importOpen, setImportOpen] = useState(false);
  const [importBusy, setImportBusy] = useState(false);
  const [importPreview, setImportPreview] = useState<PartImportPreview | null>(null);
  const [importError, setImportError] = useState<string | null>(null);
  const [templateBusy, setTemplateBusy] = useState(false);
  const [templatePath, setTemplatePath] = useState<string | null>(null);
  const [deleteAllBusy, setDeleteAllBusy] = useState(false);
  const [employeeSearch, setEmployeeSearch] = useState({ 1: "", 2: "" });

  const load = async () => {
    setLoading(true);
    try {
      const [partData, employeeData] = await Promise.all([
        api<Part[]>("/parts"),
        api<Employee[]>("/employees"),
      ]);
      setParts(partData);
      setEmployees(employeeData);
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const open = (part?: Part) => {
    if (part) {
      setEditing(part);
      setForm({
        code: part.code,
        name: part.name,
        standard_hours: part.standard_hours,
        usage_types: part.usage_types?.length ? part.usage_types : ["accessory"],
        level_1_employee_id: part.level_1_employee_id ?? null,
        level_2_employee_id: part.level_2_employee_id ?? null,
        active: part.active,
      });
    } else {
      setEditing("new");
      setForm(EMPTY_FORM);
    }
    setEmployeeSearch({ 1: "", 2: "" });
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    try {
      if (editing === "new") {
        await api("/parts", { method: "POST", body: JSON.stringify(form) });
      } else if (editing) {
        await api(`/parts/${editing.id}`, {
          method: "PUT",
          body: JSON.stringify(form),
        });
      }
      setEditing(null);
      setMessage({ text: "零件资料已保存" });
      await load();
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    }
  };

  const deactivate = async (part: Part) => {
    if (!window.confirm(`确定停用零件“${part.name}”吗？历史计划不会受影响。`))
      return;
    try {
      await api(`/parts/${part.id}`, { method: "DELETE" });
      setMessage({ text: "零件已停用" });
      await load();
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    }
  };

  const permanentlyDelete = async (part: Part) => {
    if (
      !window.confirm(
        `确定永久删除零件“${part.name}”吗？此操作无法恢复，并会从员工技能中移除该零件；用于周计划的零件将被系统拒绝删除。`,
      )
    )
      return;
    try {
      await api(`/parts/${part.id}/permanent`, { method: "DELETE" });
      setMessage({ text: "零件已永久删除" });
      await load();
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    }
  };

  const permanentlyDeleteAll = async () => {
    if (!parts.length) return;
    if (
      !window.confirm(
        `确定一键永久删除全部 ${parts.length} 个零件吗？\n\n此操作无法恢复；员工技能中的零件关联也会被移除。若零件已用于整机、生产任务或排班，系统将取消整批删除。`,
      )
    )
      return;
    if (!window.confirm("请再次确认：确实要永久删除所有零件吗？")) return;
    setDeleteAllBusy(true);
    try {
      const result = await api<{ deleted: number }>("/parts/all/permanent", {
        method: "DELETE",
      });
      setMessage({ text: `已永久删除全部 ${result.deleted} 个零件` });
      await load();
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setDeleteAllBusy(false);
    }
  };

  const openImport = () => {
    setImportPreview(null);
    setImportError(null);
    setTemplatePath(null);
    setImportOpen(true);
  };

  const saveTemplate = async () => {
    setTemplateBusy(true);
    try {
      const result = await api<{ filename: string; path: string }>(
        "/parts/import/template/save",
        { method: "POST" },
      );
      setTemplatePath(result.path);
      setMessage({ text: `Excel模板已保存：${result.filename}` });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setTemplateBusy(false);
    }
  };

  const previewFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    setImportBusy(true);
    setImportPreview(null);
    setImportError(null);
    try {
      const body = new FormData();
      body.append("file", file);
      setImportPreview(
        await api<PartImportPreview>("/parts/import/preview", {
          method: "POST",
          body,
        }),
      );
    } catch (error) {
      const detail = (error as Error).message;
      setImportError(detail);
      setMessage({ text: `表格格式有误：${detail}`, error: true });
    } finally {
      setImportBusy(false);
      event.target.value = "";
    }
  };

  const commitImport = async () => {
    if (!importPreview || importPreview.invalid_count > 0) return;
    setImportBusy(true);
    try {
      const result = await api<{ created: number; updated: number; total: number; employees_created: number; skills_updated: number }>(
        "/parts/import/commit",
        {
          method: "POST",
          body: JSON.stringify({
            rows: importPreview.rows.map((row) => ({
              code: row.code,
              name: row.name,
              standard_hours: row.standard_hours,
              usage_types: row.usage_types,
              active: row.active,
              employee_names: row.employee_names,
              employee_level1_names: row.employee_level1_names,
              employee_level2_names: row.employee_level2_names,
            })),
          }),
        },
      );
      setImportOpen(false);
      setImportPreview(null);
      await load();
      setMessage({
        text: `导入完成：新增 ${result.created} 项，更新 ${result.updated} 项，新建员工 ${result.employees_created} 人，同步技能 ${result.skills_updated} 项`,
      });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setImportBusy(false);
    }
  };

  const priorityEmployees = (level: 1 | 2) => {
    const query = employeeSearch[level].trim().toLocaleLowerCase("zh-CN");
    const otherId =
      level === 1 ? form.level_2_employee_id : form.level_1_employee_id;
    return employees
      .filter((employee) => employee.active && employee.id !== otherId)
      .filter(
        (employee) =>
          !query || employee.name.toLocaleLowerCase("zh-CN").includes(query),
      );
  };

  return (
    <>
      <section className="section-heading">
        <div>
          <span className="eyebrow">MASTER DATA · PARTS</span>
          <h2>零件与标准工时</h2>
          <p>标准工时按单件维护，生成周计划时会自动保存快照。</p>
        </div>
        <div className="heading-actions">
          <button
            className="danger-button"
            disabled={loading || deleteAllBusy || parts.length === 0}
            onClick={() => void permanentlyDeleteAll()}
          >
            {deleteAllBusy ? "正在删除…" : "一键删除所有零件"}
          </button>
          <button className="secondary-button" onClick={openImport}>导入表格</button>
          <button className="primary-button" onClick={() => open()}>
            <span>＋</span> 新增零件
          </button>
        </div>
      </section>

      <section className="panel">
        <div className="panel-header">
          <div>
            <h3>零件清单</h3>
            <p>共 {parts.length} 项 · {parts.filter((p) => p.active).length} 项启用</p>
          </div>
        </div>
        {loading ? (
          <div className="loading-block">正在读取零件资料…</div>
        ) : parts.length === 0 ? (
          <div className="empty-state">
            <div className="empty-icon">◇</div>
            <h3>还没有零件资料</h3>
            <p>先创建需要生产的零件及其单件标准工时。</p>
            <button className="secondary-button" onClick={() => open()}>
              创建第一个零件
            </button>
          </div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>零件编号</th>
                  <th>零件名称</th>
                  <th>单件标准工时</th>
                  <th>零件用途</th>
                  <th>员工1</th>
                  <th>员工2</th>
                  <th>状态</th>
                  <th className="align-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {parts.map((part) => (
                  <tr key={part.id} className={!part.active ? "muted-row" : ""}>
                    <td><span className="code-chip">{part.code}</span></td>
                    <td><strong>{part.name}</strong></td>
                    <td>{part.standard_hours.toFixed(2)} 小时</td>
                    <td>
                      <div className="usage-tags">
                        {(part.usage_types ?? ["accessory"]).map((usage) => (
                          <span key={usage}>{usage === "accessory" ? "附件" : "整机装配"}</span>
                        ))}
                      </div>
                    </td>
                    <td>{part.level_1_employee?.employee_name ?? "—"}</td>
                    <td>{part.level_2_employee?.employee_name ?? "—"}</td>
                    <td>
                      <span className={`status-pill ${part.active ? "ready" : "inactive"}`}>
                        {part.active ? "启用" : "已停用"}
                      </span>
                    </td>
                    <td className="align-right action-cell">
                      <button className="text-button" onClick={() => open(part)}>
                        编辑
                      </button>
                      {part.active && (
                        <button className="text-button danger" onClick={() => deactivate(part)}>
                          停用
                        </button>
                      )}
                      <button className="text-button danger" onClick={() => permanentlyDelete(part)}>
                        删除
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {editing && (
        <Modal
          title={editing === "new" ? "新增零件" : "编辑零件"}
          onClose={() => setEditing(null)}
        >
          <form className="form-grid" onSubmit={save}>
            <label>
              <span>零件编号</span>
              <input
                required
                maxLength={40}
                value={form.code}
                placeholder="例如：P-001"
                onChange={(event) => setForm({ ...form, code: event.target.value })}
              />
            </label>
            <label>
              <span>零件名称</span>
              <input
                required
                maxLength={100}
                value={form.name}
                placeholder="例如：定位销"
                onChange={(event) => setForm({ ...form, name: event.target.value })}
              />
            </label>
            <label className="full-field">
              <span>单件标准工时（小时）</span>
              <input
                required
                min="0.01"
                max="1000"
                step="0.01"
                type="number"
                value={form.standard_hours}
                onChange={(event) =>
                  setForm({ ...form, standard_hours: Number(event.target.value) })
                }
              />
              <small>用于计算任务工作量；90%效率会在员工产能侧换算。</small>
            </label>
            <fieldset className="full-field usage-picker">
              <legend>零件用途（至少选择一种）</legend>
              {(["accessory", "assembly"] as const).map((usage) => (
                <label key={usage}>
                  <input
                    type="checkbox"
                    checked={form.usage_types.includes(usage)}
                    onChange={(event) => {
                      const next = event.target.checked
                        ? [...form.usage_types, usage]
                        : form.usage_types.filter((item) => item !== usage);
                      if (next.length) setForm({ ...form, usage_types: next });
                    }}
                  />
                  <span>
                    <b>{usage === "accessory" ? "附件零件" : "整机装配件"}</b>
                    <small>{usage === "accessory" ? "可作为单发订单录入" : "可加入整机BOM"}</small>
                  </span>
                </label>
              ))}
            </fieldset>
            <fieldset className="full-field priority-picker">
              <legend>整机任务优先员工</legend>
              <p className="field-note">
                每级最多一人。整机每天先使用员工1，再使用员工2；双用途附件只能由员工2完成。
              </p>
              <div className="priority-picker-grid">
                {([1, 2] as const).map((level) => {
                  const selectedId =
                    level === 1
                      ? form.level_1_employee_id
                      : form.level_2_employee_id;
                  return (
                    <div className="priority-picker-column" key={level}>
                      <strong>员工{level}</strong>
                      <input
                        type="search"
                        aria-label={`搜索员工${level}`}
                        placeholder="搜索员工姓名"
                        value={employeeSearch[level]}
                        onChange={(event) =>
                          setEmployeeSearch({
                            ...employeeSearch,
                            [level]: event.target.value,
                          })
                        }
                      />
                      <div className="priority-employee-options">
                        <button
                          type="button"
                          className={!selectedId ? "selected" : ""}
                          onClick={() =>
                            setForm({
                              ...form,
                              [level === 1
                                ? "level_1_employee_id"
                                : "level_2_employee_id"]: null,
                            })
                          }
                        >
                          暂不配置
                        </button>
                        {priorityEmployees(level).map((employee) => (
                          <button
                            type="button"
                            key={employee.id}
                            className={selectedId === employee.id ? "selected" : ""}
                            onClick={() =>
                              setForm({
                                ...form,
                                [level === 1
                                  ? "level_1_employee_id"
                                  : "level_2_employee_id"]: employee.id,
                              })
                            }
                          >
                            <span>{employee.name}</span>
                            <small>
                              {employee.employee_type === "core"
                                ? "固定成员"
                                : "候补人员"}
                            </small>
                          </button>
                        ))}
                      </div>
                    </div>
                  );
                })}
              </div>
            </fieldset>
            <label className="check-field full-field">
              <input
                type="checkbox"
                checked={form.active}
                onChange={(event) => setForm({ ...form, active: event.target.checked })}
              />
              <span>启用该零件</span>
            </label>
            <div className="form-actions full-field">
              <button type="button" className="ghost-button" onClick={() => setEditing(null)}>
                取消
              </button>
              <button className="primary-button" type="submit">保存零件</button>
            </div>
          </form>
        </Modal>
      )}
      {importOpen && (
        <Modal
          title="批量导入零件"
          width="920px"
          onClose={() => !importBusy && !templateBusy && setImportOpen(false)}
        >
          <div className="import-guide">
            <div>
              <strong>使用 Excel 或 CSV 批量维护零件</strong>
              <p>同编号零件会标记为更新；“员工1、员工2”每格各填一名员工，留空表示不配置。</p>
            </div>
            <button
              type="button"
              className="secondary-button"
              disabled={templateBusy}
              onClick={() => void saveTemplate()}
            >
              {templateBusy ? "正在保存…" : "下载Excel模板"}
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
              onChange={previewFile}
            />
            <span>{importBusy ? "正在读取并校验…" : "选择 .xlsx 或 .csv 文件"}</span>
            <small>文件不超过5MB，最多5000条零件数据</small>
          </label>
          {importError && (
            <div className="import-format-error" role="alert">
              <strong>表格格式有误，暂时无法导入</strong>
              <p>{importError}</p>
              <small>
                请使用模板，并保留“零件编号、零件名称、单件标准工时（小时）”三列；不要使用公式，修改后可重新选择文件校验。
              </small>
            </div>
          )}
          {importPreview && (
            <>
              <div className="import-summary">
                <span>共 <b>{importPreview.total_rows}</b> 行</span>
                <span className="success-text">新增 <b>{importPreview.create_count}</b></span>
                <span>更新 <b>{importPreview.update_count}</b></span>
                <span>将新建员工 <b>{importPreview.new_employee_count}</b></span>
                <span className={importPreview.invalid_count ? "danger-text" : ""}>
                  错误 <b>{importPreview.invalid_count}</b>
                </span>
              </div>
              {importPreview.invalid_count > 0 && (
                <div className="import-format-error" role="alert">
                  <strong>发现 {importPreview.invalid_count} 行格式错误，整批尚未导入</strong>
                  <p>
                    {importPreview.rows
                      .filter((row) => row.errors.length > 0)
                      .slice(0, 3)
                      .map((row) => `第${row.row_number}行：${row.errors.join("、")}`)
                      .join("；")}
                    {importPreview.invalid_count > 3 ? "；更多错误请查看下表红色行" : ""}
                  </p>
                  <small>请在原表格中修正后重新选择文件；系统不会只导入其中一部分。</small>
                </div>
              )}
              <div className="table-wrap import-preview-table">
                <table>
                  <thead>
                    <tr>
                      <th>行号</th>
                      <th>处理</th>
                      <th>零件编号</th>
                      <th>零件名称</th>
                      <th>标准工时</th>
                      <th>零件用途</th>
                      <th>员工1</th>
                      <th>员工2</th>
                      <th>状态 / 校验结果</th>
                    </tr>
                  </thead>
                  <tbody>
                    {importPreview.rows.map((row) => (
                      <tr key={row.row_number} className={row.errors.length ? "error-row" : ""}>
                        <td>{row.row_number}</td>
                        <td>
                          <span className={`status-pill ${row.action === "create" ? "ready" : "confirmed"}`}>
                            {row.action === "create" ? "新增" : "更新"}
                          </span>
                        </td>
                        <td><span className="code-chip">{row.code || "—"}</span></td>
                        <td>{row.name || "—"}</td>
                        <td>{row.standard_hours > 0 ? `${row.standard_hours.toFixed(2)} 小时` : "—"}</td>
                        <td>{(row.usage_types ?? ["accessory"]).map((usage) => usage === "accessory" ? "附件" : "整机装配").join(" + ")}</td>
                        <td>{row.employee_level1_names?.[0] ?? row.employee_names?.[0] ?? "—"}</td>
                        <td>{row.employee_level2_names?.[0] ?? row.employee_names?.[1] ?? "—"}</td>
                        <td>
                          {row.errors.length ? (
                            <span className="danger-text">{row.errors.join("；")}</span>
                          ) : (
                            <span>{row.active ? "启用" : "停用"} · 校验通过</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="form-actions">
                <button
                  type="button"
                  className="ghost-button"
                  disabled={importBusy}
                  onClick={() => setImportOpen(false)}
                >
                  取消
                </button>
                <button
                  type="button"
                  className="primary-button"
                  disabled={importBusy || importPreview.invalid_count > 0}
                  onClick={() => void commitImport()}
                >
                  {importPreview.invalid_count > 0
                    ? "请先修正表格错误"
                    : `确认导入 ${importPreview.valid_count} 项`}
                </button>
              </div>
            </>
          )}
        </Modal>
      )}
      {message && (
        <Toast
          message={message.text}
          kind={message.error ? "error" : "success"}
          onClose={() => setMessage(null)}
        />
      )}
    </>
  );
}
