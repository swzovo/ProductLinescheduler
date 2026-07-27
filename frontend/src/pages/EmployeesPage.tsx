import { FormEvent, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { Modal } from "../components/Modal";
import { Toast } from "../components/Toast";
import type { Employee, EmployeeType, Part, Settings } from "../types";

type EmployeeForm = {
  name: string;
  employee_type: EmployeeType;
  active: boolean;
  skill_part_ids: number[];
};

const EMPTY_FORM: EmployeeForm = {
  name: "",
  employee_type: "core",
  active: true,
  skill_part_ids: [],
};

export function EmployeesPage() {
  const [employees, setEmployees] = useState<Employee[]>([]);
  const [parts, setParts] = useState<Part[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [editing, setEditing] = useState<Employee | null | "new">(null);
  const [form, setForm] = useState<EmployeeForm>(EMPTY_FORM);
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [skillSearch, setSkillSearch] = useState("");
  const [deleteAllBusy, setDeleteAllBusy] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const [employeeData, partData, settingsData] = await Promise.all([
        api<Employee[]>("/employees"),
        api<Part[]>("/parts"),
        api<Settings>("/settings"),
      ]);
      setEmployees(employeeData);
      setParts(partData);
      setSettings(settingsData);
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const open = (employee?: Employee) => {
    setSkillSearch("");
    if (employee) {
      setEditing(employee);
      setForm({
        name: employee.name,
        employee_type: employee.employee_type,
        active: employee.active,
        skill_part_ids: employee.skill_part_ids,
      });
    } else {
      setEditing("new");
      setForm(EMPTY_FORM);
    }
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    try {
      if (editing === "new") {
        await api("/employees", { method: "POST", body: JSON.stringify(form) });
      } else if (editing) {
        await api(`/employees/${editing.id}`, {
          method: "PUT",
          body: JSON.stringify(form),
        });
      }
      setEditing(null);
      setMessage({ text: "员工资料与技能已保存" });
      await load();
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    }
  };

  const deactivate = async (employee: Employee) => {
    if (!window.confirm(`确定停用员工“${employee.name}”吗？`)) return;
    try {
      await api(`/employees/${employee.id}`, { method: "DELETE" });
      setMessage({ text: "员工已停用" });
      await load();
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    }
  };

  const activate = async (employee: Employee) => {
    try {
      await api(`/employees/${employee.id}`, {
        method: "PUT",
        body: JSON.stringify({
          name: employee.name,
          employee_type: employee.employee_type,
          active: true,
          skill_part_ids: employee.skill_part_ids,
        }),
      });
      setMessage({ text: "员工已启用，将重新参与后续排班" });
      await load();
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    }
  };

  const permanentlyDelete = async (employee: Employee) => {
    if (
      !window.confirm(
        `确定永久删除员工“${employee.name}”吗？此操作无法恢复；参与过周计划的员工将被系统拒绝删除。`,
      )
    )
      return;
    try {
      await api(`/employees/${employee.id}/permanent`, { method: "DELETE" });
      setMessage({ text: "员工已永久删除" });
      await load();
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    }
  };

  const permanentlyDeleteAll = async () => {
    if (!employees.length) return;
    if (
      !window.confirm(
        `确定一键永久删除全部 ${employees.length} 名员工吗？\n\n此操作无法恢复，员工技能配置也会一并删除。若员工已参与周计划或历史排班，系统将取消整批删除。`,
      )
    )
      return;
    if (!window.confirm("请再次确认：确实要永久删除所有员工吗？")) return;
    setDeleteAllBusy(true);
    try {
      const result = await api<{ deleted: number }>("/employees/all/permanent", {
        method: "DELETE",
      });
      setMessage({ text: `已永久删除全部 ${result.deleted} 名员工` });
      await load();
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setDeleteAllBusy(false);
    }
  };

  const toggleSkill = (partId: number) => {
    const selected = form.skill_part_ids.includes(partId);
    setForm({
      ...form,
      skill_part_ids: selected
        ? form.skill_part_ids.filter((id) => id !== partId)
        : [...form.skill_part_ids, partId],
    });
  };

  const activeParts = useMemo(
    () => parts.filter((part) => part.active),
    [parts],
  );
  const filteredSkillParts = useMemo(() => {
    const keyword = skillSearch.trim().toLocaleLowerCase("zh-CN");
    if (!keyword) return activeParts;
    return activeParts.filter(
      (part) =>
        part.code.toLocaleLowerCase("zh-CN").includes(keyword) ||
        part.name.toLocaleLowerCase("zh-CN").includes(keyword),
    );
  }, [activeParts, skillSearch]);

  return (
    <>
      <section className="section-heading">
        <div>
          <span className="eyebrow">MASTER DATA · PEOPLE</span>
          <h2>员工与技能矩阵</h2>
          <p>固定成员自动参与排班，候补人员只在人工选择增援后加入。</p>
        </div>
        <div className="heading-actions">
          <button
            className="danger-button"
            disabled={loading || deleteAllBusy || employees.length === 0}
            onClick={() => void permanentlyDeleteAll()}
          >
            {deleteAllBusy ? "正在删除…" : "一键删除所有员工"}
          </button>
          <button className="primary-button" onClick={() => open()}>
            <span>＋</span> 新增员工
          </button>
        </div>
      </section>

      <div className="summary-strip compact">
        <div><span>固定成员</span><strong>{employees.filter((e) => e.active && e.employee_type === "core").length}</strong></div>
        <div><span>候补人员</span><strong>{employees.filter((e) => e.active && e.employee_type === "backup").length}</strong></div>
        <div><span>技能覆盖</span><strong>{new Set(employees.flatMap((e) => e.skill_part_ids)).size}</strong></div>
      </div>

      <section className="panel">
        <div className="panel-header">
          <div>
            <h3>人员清单</h3>
            <p>技能标签展示该员工可以生产的零件</p>
          </div>
        </div>
        {loading ? (
          <div className="loading-block">正在读取员工资料…</div>
        ) : employees.length === 0 ? (
          <div className="empty-state">
            <div className="empty-icon">♙</div>
            <h3>还没有员工资料</h3>
            <p>添加固定成员和候补人员，并勾选他们会制作的零件。</p>
            <button className="secondary-button" onClick={() => open()}>创建第一个员工</button>
          </div>
        ) : (
          <div className="employee-cards">
            {employees.map((employee) => (
              <article className={`employee-card ${!employee.active ? "disabled" : ""}`} key={employee.id}>
                <div className="avatar">{employee.name.slice(0, 1)}</div>
                <div className="employee-card-main">
                  <header>
                    <div>
                      <h3>{employee.name}</h3>
                      <span className={`type-badge ${employee.employee_type}`}>
                        {employee.employee_type === "core" ? "固定成员" : "候补人员"}
                      </span>
                      {!employee.active && <span className="status-pill inactive">已停用</span>}
                    </div>
                    <div className="action-cell">
                      <button className="text-button" onClick={() => open(employee)}>编辑</button>
                      {employee.active ? (
                        <button className="text-button danger" onClick={() => deactivate(employee)}>停用</button>
                      ) : (
                        <button className="text-button activate" onClick={() => void activate(employee)}>启用</button>
                      )}
                      <button className="text-button danger" onClick={() => permanentlyDelete(employee)}>
                        删除
                      </button>
                    </div>
                  </header>
                  <div className="skill-list">
                    {employee.skill_part_ids.length ? (
                      employee.skill_part_ids.map((id) => {
                        const part = parts.find((item) => item.id === id);
                        return part ? <span key={id}>{part.code} · {part.name}</span> : null;
                      })
                    ) : (
                      <em>尚未配置技能</em>
                    )}
                  </div>
                  <footer className="employee-pattern">
                    <span>
                      基础出勤：<strong>默认周一至周五</strong>
                      <small>具体上班日期请在周排班中逐周调整</small>
                    </span>
                    <span>
                      加班规则：
                      <strong>
                        {`不加班或固定 ${settings?.overtime_block_hours ?? "—"} 小时`}
                      </strong>
                    </span>
                  </footer>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      {editing && (
        <Modal
          title={editing === "new" ? "新增员工" : "编辑员工"}
          width="760px"
          onClose={() => setEditing(null)}
        >
          <form className="form-grid" onSubmit={save}>
            <label>
              <span>员工姓名</span>
              <input
                required
                value={form.name}
                placeholder="请输入姓名"
                onChange={(event) => setForm({ ...form, name: event.target.value })}
              />
            </label>
            <label>
              <span>人员类型</span>
              <select
                value={form.employee_type}
                onChange={(event) =>
                  setForm({ ...form, employee_type: event.target.value as EmployeeType })
                }
              >
                <option value="core">固定成员</option>
                <option value="backup">候补人员</option>
              </select>
            </label>
            <div className="form-note-card">
              <strong>出勤安排</strong>
              <span>
                新员工默认周一至周五上班；具体上班天数和星期请在周排班的员工编辑中逐周调整。
              </span>
            </div>
            <div className="form-note-card">
              <strong>统一加班规则</strong>
              <span>
                员工每天只能选择“不加班”或完整加班
                {settings?.overtime_block_hours ?? "—"}小时，时长统一在系统设置中调整。
              </span>
            </div>
            <fieldset className="full-field skill-picker">
              <legend>可制作零件</legend>
              {activeParts.length ? (
                <>
                  <div className="skill-picker-toolbar">
                    <label className="skill-search">
                      <span>搜索零件编号或名称</span>
                      <input
                        type="search"
                        value={skillSearch}
                        placeholder="输入编号，例如 140502377"
                        onChange={(event) => setSkillSearch(event.target.value)}
                      />
                    </label>
                    <span className="skill-selection-count">
                      已选择 <b>{form.skill_part_ids.length}</b> 项 · 当前显示 {filteredSkillParts.length} 项
                    </span>
                  </div>
                  <div className="skill-options">
                  {filteredSkillParts.map((part) => (
                    <label
                      key={part.id}
                      className={form.skill_part_ids.includes(part.id) ? "selected" : ""}
                      title={`${part.code} · ${part.name}`}
                    >
                      <input
                        type="checkbox"
                        checked={form.skill_part_ids.includes(part.id)}
                        onChange={() => toggleSkill(part.id)}
                      />
                      <span className="skill-option-main">
                        <b>{part.code}</b>
                        <em>{part.name}</em>
                      </span>
                      <small>{part.standard_hours.toFixed(2)}h / 件</small>
                    </label>
                  ))}
                  {filteredSkillParts.length === 0 && (
                    <div className="skill-search-empty">没有找到匹配的零件，请检查编号或名称。</div>
                  )}
                  </div>
                </>
              ) : (
                <p className="field-note">请先在“零件管理”中创建启用的零件。</p>
              )}
            </fieldset>
            <label className="check-field full-field">
              <input
                type="checkbox"
                checked={form.active}
                onChange={(event) => setForm({ ...form, active: event.target.checked })}
              />
              <span>启用该员工</span>
            </label>
            <div className="form-actions full-field">
              <button type="button" className="ghost-button" onClick={() => setEditing(null)}>取消</button>
              <button className="primary-button" type="submit">保存员工</button>
            </div>
          </form>
        </Modal>
      )}
      {message && (
        <Toast message={message.text} kind={message.error ? "error" : "success"} onClose={() => setMessage(null)} />
      )}
    </>
  );
}
