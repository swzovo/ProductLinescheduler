import { useState } from "react";
import { useCloudSync } from "./cloud/CloudSyncProvider";
import { EmployeesPage } from "./pages/EmployeesPage";
import { PartsPage } from "./pages/PartsPage";
import { MachinesPage } from "./pages/MachinesPage";
import { ProductionOrdersPage } from "./pages/ProductionOrdersPage";
import { SettingsPage } from "./pages/SettingsPage";
import { WeekPlanner } from "./pages/WeekPlanner";

type Page = "schedule" | "orders" | "parts" | "machines" | "employees" | "settings";

const NAV: { id: Page; label: string; icon: string; note: string }[] = [
  { id: "schedule", label: "周排班", icon: "▦", note: "计划与负荷" },
  { id: "orders", label: "生产需求", icon: "◫", note: "订单与跨周计划" },
  { id: "parts", label: "零件管理", icon: "◇", note: "工时资料" },
  { id: "machines", label: "整机管理", icon: "▣", note: "整机与BOM" },
  { id: "employees", label: "员工管理", icon: "♙", note: "人员与技能" },
  { id: "settings", label: "系统设置", icon: "⚙", note: "产能与阈值" },
];

export default function App() {
  const cloud = useCloudSync();
  const [page, setPage] = useState<Page>("schedule");
  const [productionWeekStart, setProductionWeekStart] = useState<string | undefined>();
  const [sidebarOpen, setSidebarOpen] = useState(false);

  return (
    <div className="app-shell">
      <aside className={`sidebar ${sidebarOpen ? "open" : ""}`}>
        <div className="brand">
          <div className="brand-mark">PL</div>
          <div>
            <strong>产线排班</strong>
            <span>PRODUCTION FLOW</span>
          </div>
        </div>
        <nav>
          {NAV.map((item) => (
            <button
              key={item.id}
              className={page === item.id ? "active" : ""}
              onClick={() => {
                setPage(item.id);
                setSidebarOpen(false);
              }}
            >
              <span className="nav-icon">{item.icon}</span>
              <span>
                <b>{item.label}</b>
                <small>{item.note}</small>
              </span>
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <button className="sidebar-sync" onClick={cloud.openCenter}>
            <span className={`live-dot ${cloud.status}`} />
            <span>
              <b>{cloud.user ? cloud.user.displayName : "本机数据"}</b>
              <small>{cloud.statusText}</small>
            </span>
          </button>
        </div>
      </aside>
      <main>
        <header className="topbar">
          <button
            className="menu-button"
            onClick={() => setSidebarOpen((value) => !value)}
            aria-label="打开菜单"
          >
            ☰
          </button>
          <div>
            <p>{NAV.find((item) => item.id === page)?.note}</p>
            <h1>{NAV.find((item) => item.id === page)?.label}</h1>
          </div>
          <div className="topbar-actions">
            <button className={`sync-chip ${cloud.status}`} onClick={cloud.openCenter}>
              <span className={`live-dot ${cloud.status}`} />
              {cloud.statusText}
            </button>
            <div className="today-chip">
              <span>今日</span>
              {new Intl.DateTimeFormat("zh-CN", {
                month: "long",
                day: "numeric",
                weekday: "short",
              }).format(new Date())}
            </div>
          </div>
        </header>
        <div className="page-content">
          {page === "schedule" && <WeekPlanner onOpenProduction={(weekStart) => { setProductionWeekStart(weekStart); setPage("orders"); }} />}
          {page === "orders" && <ProductionOrdersPage defaultWeekStart={productionWeekStart} onOpenSchedule={() => setPage("schedule")} />}
          {page === "parts" && <PartsPage />}
          {page === "machines" && <MachinesPage />}
          {page === "employees" && <EmployeesPage />}
          {page === "settings" && <SettingsPage />}
        </div>
      </main>
    </div>
  );
}
