import { ChangeEvent, FormEvent, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { Modal } from "../components/Modal";
import { Toast } from "../components/Toast";
import type { Machine, MachineBomMatrixPreview, Part } from "../types";

const EMPTY_FORM = {
  code: "",
  name: "",
  active: true,
  bom: {} as Record<number, number>,
};

export function MachinesPage() {
  const [machines, setMachines] = useState<Machine[]>([]);
  const [parts, setParts] = useState<Part[]>([]);
  const [editing, setEditing] = useState<Machine | "new" | null>(null);
  const [form, setForm] = useState(EMPTY_FORM);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [importBusy, setImportBusy] = useState(false);
  const [importPreview, setImportPreview] =
    useState<MachineBomMatrixPreview | null>(null);
  const [importError, setImportError] = useState<string | null>(null);
  const [templatePath, setTemplatePath] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const [machineData, partData] = await Promise.all([
        api<Machine[]>("/machines"),
        api<Part[]>("/parts"),
      ]);
      setMachines(machineData);
      setParts(partData);
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);

  const availableParts = useMemo(() => {
    const keyword = search.trim().toLocaleLowerCase("zh-CN");
    return parts.filter((part) => {
      const assembly = part.active && (part.usage_types ?? []).includes("assembly");
      return assembly && (!keyword || `${part.code} ${part.name}`.toLocaleLowerCase("zh-CN").includes(keyword));
    });
  }, [parts, search]);

  const open = (machine?: Machine) => {
    setSearch("");
    if (machine) {
      setEditing(machine);
      setForm({
        code: machine.code,
        name: machine.name,
        active: machine.active,
        bom: Object.fromEntries(machine.bom_items.map((item) => [item.part_id, item.quantity_per_machine])),
      });
    } else {
      setEditing("new");
      setForm(EMPTY_FORM);
    }
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    const bom_items = Object.entries(form.bom)
      .filter(([, quantity]) => quantity > 0)
      .map(([partId, quantity]) => ({ part_id: Number(partId), quantity_per_machine: quantity }));
    if (!bom_items.length) {
      setMessage({ text: "请至少选择一个整机装配零件", error: true });
      return;
    }
    try {
      await api(editing === "new" ? "/machines" : `/machines/${editing!.id}`, {
        method: editing === "new" ? "POST" : "PUT",
        body: JSON.stringify({ code: form.code, name: form.name, active: form.active, bom_items }),
      });
      setEditing(null);
      await load();
      setMessage({ text: "整机和BOM已保存" });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    }
  };

  const remove = async (machine: Machine, permanent = false) => {
    const action = permanent ? "永久删除" : "停用";
    if (!window.confirm(`确定${action}整机“${machine.name}”吗？`)) return;
    try {
      await api(`/machines/${machine.id}${permanent ? "/permanent" : ""}`, { method: "DELETE" });
      await load();
      setMessage({ text: `整机已${action}` });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    }
  };

  const openImport = () => {
    setImportPreview(null);
    setImportError(null);
    setTemplatePath(null);
    setImportOpen(true);
  };

  const saveTemplate = async () => {
    setImportBusy(true);
    try {
      const result = await api<{ filename: string; path: string }>(
        "/machines/import/template/save",
        { method: "POST" },
      );
      setTemplatePath(result.path);
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setImportBusy(false);
    }
  };

  const previewImport = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    setImportBusy(true);
    setImportPreview(null);
    setImportError(null);
    try {
      const body = new FormData();
      body.append("file", file);
      setImportPreview(
        await api<MachineBomMatrixPreview>("/machines/import/preview", {
          method: "POST",
          body,
        }),
      );
    } catch (error) {
      setImportError((error as Error).message);
    } finally {
      setImportBusy(false);
      event.target.value = "";
    }
  };

  const commitImport = async () => {
    if (!importPreview || importPreview.invalid_count > 0) return;
    setImportBusy(true);
    try {
      const result = await api<{ created: number; updated: number }>(
        "/machines/import/commit",
        {
          method: "POST",
          body: JSON.stringify({
            machines: importPreview.machines.map((machine) => ({
              code: machine.code,
              name: machine.name,
              bom_items: machine.bom_items.map((item) => ({
                part_id: item.part_id,
                quantity_per_machine: item.quantity_per_machine,
              })),
            })),
          }),
        },
      );
      setImportOpen(false);
      setImportPreview(null);
      await load();
      setMessage({
        text: `整机BOM导入完成：新增 ${result.created} 项，更新 ${result.updated} 项`,
      });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setImportBusy(false);
    }
  };

  return (
    <>
      <section className="section-heading">
        <div>
          <span className="eyebrow">MASTER DATA · MACHINES</span>
          <h2>整机与零件清单</h2>
          <p>维护整机编号和每台所需零件；生产任务会保存创建时的BOM快照。</p>
        </div>
        <div className="heading-actions">
          <button className="secondary-button" onClick={openImport}>导入BOM矩阵</button>
          <button className="primary-button" onClick={() => open()}><span>＋</span> 新增整机</button>
        </div>
      </section>

      <section className="panel">
        <div className="panel-header"><div><h3>整机清单</h3><p>共 {machines.length} 项</p></div></div>
        {loading ? <div className="loading-block">正在读取整机资料…</div> : machines.length === 0 ? (
          <div className="empty-state"><div className="empty-icon">▣</div><h3>还没有整机</h3><p>先建立整机，再选择它包含的装配零件。</p></div>
        ) : (
          <div className="machine-card-grid">
            {machines.map((machine) => (
              <article key={machine.id} className={`machine-card ${machine.active ? "" : "inactive"}`}>
                <header>
                  <div><span className="code-chip">{machine.code}</span><h3>{machine.name}</h3></div>
                  <span className={`status-pill ${machine.active ? "ready" : "inactive"}`}>{machine.active ? "启用" : "停用"}</span>
                </header>
                <div className="machine-bom-summary">
                  {machine.bom_items.slice(0, 6).map((item) => (
                    <span key={item.part_id}><b>{item.part_code}</b> × {item.quantity_per_machine}</span>
                  ))}
                  {machine.bom_items.length > 6 && <span>另有 {machine.bom_items.length - 6} 项</span>}
                </div>
                <footer>
                  <span>每台共 {machine.bom_items.reduce((sum, item) => sum + item.quantity_per_machine, 0)} 件</span>
                  <div><button className="text-button" onClick={() => open(machine)}>编辑</button>{machine.active && <button className="text-button danger" onClick={() => void remove(machine)}>停用</button>}<button className="text-button danger" onClick={() => void remove(machine, true)}>删除</button></div>
                </footer>
              </article>
            ))}
          </div>
        )}
      </section>

      {editing && (
        <Modal title={editing === "new" ? "新增整机" : "编辑整机与BOM"} width="900px" onClose={() => setEditing(null)}>
          <form onSubmit={save}>
            <div className="form-grid">
              <label><span>整机编号</span><input required maxLength={40} value={form.code} onChange={(event) => setForm({ ...form, code: event.target.value })} /></label>
              <label><span>整机名称</span><input required maxLength={100} value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></label>
            </div>
            <div className="bom-editor">
              <div className="bom-editor-head">
                <div><h4>每台用量</h4><p>只显示具备“整机装配”用途的启用零件</p></div>
                <input type="search" aria-label="搜索BOM零件编号或名称" placeholder="搜索零件编号或名称" value={search} onChange={(event) => setSearch(event.target.value)} />
              </div>
              <div className="bom-part-list">
                {availableParts.map((part) => {
                  const selected = Boolean(form.bom[part.id]);
                  return (
                    <label key={part.id} className={selected ? "selected" : ""}>
                      <input type="checkbox" checked={selected} onChange={(event) => {
                        const next = { ...form.bom };
                        if (event.target.checked) next[part.id] = 1; else delete next[part.id];
                        setForm({ ...form, bom: next });
                      }} />
                      <span><b>{part.code}</b><em>{part.name}</em></span>
                      <small>{part.standard_hours.toFixed(2)}h</small>
                      <input aria-label={`${part.code} 每台用量`} type="number" min="1" step="1" disabled={!selected} value={form.bom[part.id] ?? 1} onChange={(event) => setForm({ ...form, bom: { ...form.bom, [part.id]: Math.max(1, Number(event.target.value)) } })} />
                    </label>
                  );
                })}
                {availableParts.length === 0 && <div className="skill-search-empty">没有匹配的整机装配零件。</div>}
              </div>
            </div>
            <label className="check-field"><input type="checkbox" checked={form.active} onChange={(event) => setForm({ ...form, active: event.target.checked })} /><span>启用该整机</span></label>
            <div className="form-actions"><button type="button" className="ghost-button" onClick={() => setEditing(null)}>取消</button><button className="primary-button" type="submit">保存整机</button></div>
            {editing === "new" && (
              <button type="button" className="text-button" onClick={() => {
                setEditing(null);
                openImport();
              }}>需要一次新增多个机型？改用BOM矩阵导入</button>
            )}
          </form>
        </Modal>
      )}
      {importOpen && (
        <Modal title="导入整机BOM矩阵" width="980px" onClose={() => !importBusy && setImportOpen(false)}>
          <div className="import-guide">
            <div>
              <strong>按“零件行 × 整机列”完整更新BOM</strong>
              <p>第一行为整机编号，第二行为整机名称；Y表示每台1件，也可填写正整数用量。已有整机将完整替换BOM。</p>
            </div>
            <button type="button" className="secondary-button" disabled={importBusy} onClick={() => void saveTemplate()}>
              下载Excel模板
            </button>
          </div>
          {templatePath && <div className="template-save-success"><strong>模板保存成功</strong><span>{templatePath}</span></div>}
          <label className="file-drop">
            <input type="file" accept=".xlsx" disabled={importBusy} onChange={previewImport} />
            <span>{importBusy ? "正在读取并校验…" : "选择 .xlsx 文件"}</span>
            <small>零件必须已启用并具备“整机装配”用途</small>
          </label>
          {importError && <div className="import-format-error" role="alert"><strong>表格格式有误</strong><p>{importError}</p></div>}
          {importPreview && (
            <>
              <div className="import-summary">
                <span>整机 <b>{importPreview.total_machines}</b> 项</span>
                <span className="success-text">可导入 <b>{importPreview.valid_count}</b></span>
                <span className={importPreview.invalid_count ? "danger-text" : ""}>错误 <b>{importPreview.invalid_count}</b></span>
              </div>
              <div className="table-wrap import-preview-table">
                <table>
                  <thead><tr><th>列</th><th>处理</th><th>整机编号</th><th>整机名称</th><th>BOM零件数</th><th>校验结果</th></tr></thead>
                  <tbody>
                    {importPreview.machines.map((machine) => (
                      <tr key={`${machine.column}-${machine.code}`} className={machine.errors.length ? "error-row" : ""}>
                        <td>{machine.column}</td>
                        <td>{machine.action === "create" ? "新增" : "完整更新"}</td>
                        <td><span className="code-chip">{machine.code || "—"}</span></td>
                        <td>{machine.name || "—"}</td>
                        <td>{machine.bom_items.length}</td>
                        <td>{machine.errors.length ? <span className="danger-text">{machine.errors.join("；")}</span> : <span className="success-text">校验通过</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="form-actions">
                <button type="button" className="ghost-button" onClick={() => setImportOpen(false)}>取消</button>
                <button type="button" className="primary-button" disabled={importBusy || importPreview.invalid_count > 0} onClick={() => void commitImport()}>
                  {importPreview.invalid_count ? "请先修正表格错误" : `确认导入 ${importPreview.valid_count} 个整机`}
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
