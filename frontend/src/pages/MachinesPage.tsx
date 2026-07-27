import { FormEvent, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { Modal } from "../components/Modal";
import { Toast } from "../components/Toast";
import type { Machine, Part } from "../types";

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

  return (
    <>
      <section className="section-heading">
        <div>
          <span className="eyebrow">MASTER DATA · MACHINES</span>
          <h2>整机与零件清单</h2>
          <p>维护整机编号和每台所需零件；生产任务会保存创建时的BOM快照。</p>
        </div>
        <button className="primary-button" onClick={() => open()}><span>＋</span> 新增整机</button>
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
          </form>
        </Modal>
      )}
      {message && <Toast message={message.text} kind={message.error ? "error" : "success"} onClose={() => setMessage(null)} />}
    </>
  );
}
