import { FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import { Toast } from "../components/Toast";
import type { Settings } from "../types";

const DEFAULTS: Settings = {
  daily_hours: 7.5,
  efficiency: 0.9,
  overtime_limit: 4,
  overtime_block_hours: 4,
  shortage_threshold: 16.875,
  green_threshold: 0.8,
  yellow_threshold: 1,
  daily_efficiency_low_threshold: 0.8,
  daily_efficiency_target_threshold: 0.9,
};

export function SettingsPage() {
  const [settings, setSettings] = useState<Settings>(DEFAULTS);
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(null);
  const [loading, setLoading] = useState(true);
  const [maintenanceBusy, setMaintenanceBusy] = useState<"cache" | "history" | null>(null);

  useEffect(() => {
    api<Settings>("/settings")
      .then(setSettings)
      .catch((error) => setMessage({ text: error.message, error: true }))
      .finally(() => setLoading(false));
  }, []);

  const save = async (event: FormEvent) => {
    event.preventDefault();
    try {
      const result = await api<Settings>("/settings", {
        method: "PUT",
        body: JSON.stringify(settings),
      });
      setSettings(result);
      setMessage({ text: "设置已保存并同步到未确认周；请重新生成排班。已确认历史不会改变" });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    }
  };

  const effectiveCapacity = settings.daily_hours * settings.efficiency;
  const calculatedHalfWeek = (effectiveCapacity * 5) / 2;

  const clearCache = async () => {
    setMaintenanceBusy("cache");
    try {
      await api("/maintenance/clear-cache", { method: "POST" });
      localStorage.clear();
      sessionStorage.clear();
      if ("caches" in window) {
        const keys = await window.caches.keys();
        await Promise.all(keys.map((key) => window.caches.delete(key)));
      }
      setMessage({ text: "缓存已清除，零件、员工和排班资料没有改变" });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setMaintenanceBusy(null);
    }
  };

  const deleteHistory = async () => {
    const accepted = window.confirm(
      "确定删除全部生产需求和历史周排班吗？\n\n将删除草稿、已确认周、任务来源、排班明细、出勤调整和加班审批。零件、整机BOM、员工技能及系统设置会保留。此操作无法撤销。",
    );
    if (!accepted) return;
    setMaintenanceBusy("history");
    try {
      const result = await api<{ weeks: number; orders: number; assignments: number }>(
        "/maintenance/schedule-history",
        { method: "DELETE" },
      );
      setMessage({ text: `历史记录已删除：${result.weeks} 个周计划、${result.orders} 个生产任务、${result.assignments} 条排班明细` });
    } catch (error) {
      setMessage({ text: (error as Error).message, error: true });
    } finally {
      setMaintenanceBusy(null);
    }
  };

  return (
    <>
      <section className="section-heading">
        <div>
          <span className="eyebrow">SYSTEM · CAPACITY</span>
          <h2>产能与告警参数</h2>
          <p>设置保存后同步到新建和未确认周；已确认历史继续使用原参数快照。</p>
        </div>
      </section>
      <div className="settings-layout">
        <section className="panel">
          <div className="panel-header">
            <div><h3>全局设置</h3><p>统一应用于所有固定成员与候补人员</p></div>
          </div>
          {loading ? (
            <div className="loading-block">正在读取系统设置…</div>
          ) : (
            <form className="settings-form" onSubmit={save}>
              <label>
                <span>每日理论出勤工时</span>
                <div className="input-with-unit">
                  <input
                    type="number"
                    min="0.1"
                    max="24"
                    step="0.1"
                    value={settings.daily_hours}
                    onChange={(event) => setSettings({ ...settings, daily_hours: Number(event.target.value) })}
                  />
                  <b>小时</b>
                </div>
              </label>
              <label>
                <span>统一生产效率</span>
                <div className="input-with-unit">
                  <input
                    type="number"
                    min="1"
                    max="100"
                    step="1"
                    value={Math.round(settings.efficiency * 100)}
                    onChange={(event) => setSettings({ ...settings, efficiency: Number(event.target.value) / 100 })}
                  />
                  <b>%</b>
                </div>
              </label>
              <label>
                <span>固定加班班次</span>
                <div className="input-with-unit">
                  <input
                    type="number"
                    min="0.25"
                    max="12"
                    step="0.25"
                    value={settings.overtime_block_hours}
                    onChange={(event) => {
                      const hours = Number(event.target.value);
                      setSettings({
                        ...settings,
                        overtime_limit: hours,
                        overtime_block_hours: hours,
                      });
                    }}
                  />
                  <b>小时</b>
                </div>
                <small>所有员工每天只能选择不加班或完整加班一次；缺口处理中会按该班次从任务截止日期向前安排。</small>
              </label>
              <label>
                <span>建议增援的缺口阈值</span>
                <div className="input-with-unit">
                  <input
                    type="number"
                    min="0"
                    max="10000"
                    step="0.125"
                    value={settings.shortage_threshold}
                    onChange={(event) => setSettings({ ...settings, shortage_threshold: Number(event.target.value) })}
                  />
                  <b>标准工时</b>
                </div>
                <small>
                  缺口大于此值时建议增加成员；等于或小于时建议加班。当前参数计算的半周产能为 {calculatedHalfWeek.toFixed(3)} 小时。
                </small>
              </label>
              <div className="settings-subheading">
                <strong>员工每日负荷颜色</strong>
                <small>用于每名员工的“已排标准工时 ÷ 有效产能”进度条。</small>
              </div>
              <div className="threshold-row">
                <label>
                  <span>绿色上限</span>
                  <div className="input-with-unit">
                    <input
                      type="number"
                      min="1"
                      max="100"
                      value={Math.round(settings.green_threshold * 100)}
                      onChange={(event) => setSettings({ ...settings, green_threshold: Number(event.target.value) / 100 })}
                    />
                    <b>%</b>
                  </div>
                </label>
                <label>
                  <span>黄色上限</span>
                  <div className="input-with-unit">
                    <input
                      type="number"
                      min="1"
                      max="200"
                      value={Math.round(settings.yellow_threshold * 100)}
                      onChange={(event) => setSettings({ ...settings, yellow_threshold: Number(event.target.value) / 100 })}
                    />
                    <b>%</b>
                  </div>
                </label>
              </div>
              <div className="settings-subheading">
                <strong>每日整体生产效率颜色</strong>
                <small>整体生产效率＝固定成员当天已排标准工时 ÷ 固定成员当天可用出勤工时；候补增援不参与计算。</small>
              </div>
              <div className="threshold-row">
                <label>
                  <span>红色预警线（低于）</span>
                  <div className="input-with-unit">
                    <input
                      type="number"
                      min="0"
                      max="200"
                      value={Math.round(settings.daily_efficiency_low_threshold * 100)}
                      onChange={(event) => setSettings({ ...settings, daily_efficiency_low_threshold: Number(event.target.value) / 100 })}
                    />
                    <b>%</b>
                  </div>
                </label>
                <label>
                  <span>绿色达标线（达到）</span>
                  <div className="input-with-unit">
                    <input
                      type="number"
                      min="1"
                      max="200"
                      value={Math.round(settings.daily_efficiency_target_threshold * 100)}
                      onChange={(event) => setSettings({ ...settings, daily_efficiency_target_threshold: Number(event.target.value) / 100 })}
                    />
                    <b>%</b>
                  </div>
                </label>
              </div>
              <div className="form-actions">
                <button className="primary-button" type="submit">保存设置</button>
              </div>
            </form>
          )}
        </section>
        <aside className="capacity-preview">
          <span className="eyebrow">实时换算</span>
          <h3>标准日有效产能</h3>
          <strong>{effectiveCapacity.toFixed(2)}<small>小时</small></strong>
          <div className="formula">
            <span>{settings.daily_hours}h 出勤</span><i>×</i>
            <span>{Math.round(settings.efficiency * 100)}% 效率</span>
          </div>
          <hr />
          <p>半周缺口判断阈值</p>
          <b>{settings.shortage_threshold.toFixed(3)} 标准工时</b>
          <small className="capacity-note">可人工调整 · 理论半周产能 {calculatedHalfWeek.toFixed(3)} 小时</small>
          <div className="legend">
            <b>员工每日负荷</b>
            <span><i className="green" />≤ {Math.round(settings.green_threshold * 100)}%</span>
            <span><i className="yellow" />≤ {Math.round(settings.yellow_threshold * 100)}%</span>
            <span><i className="red" />超出正常产能</span>
          </div>
          <div className="legend efficiency-legend">
            <b>每日整体生产效率</b>
            <span><i className="red" />低于 {Math.round(settings.daily_efficiency_low_threshold * 100)}%</span>
            <span>
              <i className="yellow" />
              {Math.round(settings.daily_efficiency_low_threshold * 100)}%–{Math.round(settings.daily_efficiency_target_threshold * 100)}%
            </span>
            <span><i className="green" />达到 {Math.round(settings.daily_efficiency_target_threshold * 100)}%</span>
          </div>
        </aside>
      </div>
      <section className="panel maintenance-panel">
        <div className="panel-header">
          <div><h3>数据维护</h3><p>清理运行缓存，或将排班数据恢复为空白状态</p></div>
        </div>
        <div className="maintenance-actions">
          <div>
            <strong>清除缓存</strong>
            <p>清除数据库临时日志、浏览器缓存和页面临时状态，不删除任何业务资料。</p>
            <button className="secondary-button" disabled={maintenanceBusy !== null} onClick={() => void clearCache()}>{maintenanceBusy === "cache" ? "正在清理…" : "清除缓存"}</button>
          </div>
          <div className="danger-zone">
            <strong>删除历史排班记录</strong>
            <p>删除全部生产需求、周计划、排班明细、出勤调整及加班审批；保留零件、整机、员工和系统设置。</p>
            <button className="danger-button" disabled={maintenanceBusy !== null} onClick={() => void deleteHistory()}>{maintenanceBusy === "history" ? "正在删除…" : "删除全部历史排班"}</button>
          </div>
        </div>
      </section>
      {message && (
        <Toast message={message.text} kind={message.error ? "error" : "success"} onClose={() => setMessage(null)} />
      )}
    </>
  );
}
