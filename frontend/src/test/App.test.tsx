import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";

const emptyResponses: Record<string, unknown> = {
  "/api/weeks": [],
  "/api/parts": [],
  "/api/employees": [],
  "/api/machines": [],
  "/api/production-orders": [],
  "/api/settings": {
    daily_hours: 7.5,
    efficiency: 0.9,
    overtime_limit: 4,
    overtime_block_hours: 4,
    shortage_threshold: 16.875,
    green_threshold: 0.8,
    yellow_threshold: 1,
    daily_efficiency_low_threshold: 0.8,
    daily_efficiency_target_threshold: 0.9,
  },
  "/api/parts/import/template/save": {
    filename: "零件导入模板.xlsx",
    path: "/Users/test/Downloads/零件导入模板.xlsx",
  },
  "/api/maintenance/clear-cache": { status: "cleared", cleared_paths: [] },
  "/api/maintenance/schedule-history": { status: "cleared", weeks: 2, orders: 3, assignments: 8 },
  "/api/parts/all/permanent": { status: "deleted", deleted: 1 },
  "/api/employees/all/permanent": { status: "deleted", deleted: 1 },
};

describe("应用导航与容量展示", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = String(input);
        return Promise.resolve(
          new Response(JSON.stringify(emptyResponses[path]), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("展示空周计划引导，并可进入设置页查看公式", async () => {
    render(<App />);
    expect(await screen.findByText("从第一个周计划开始")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /系统设置/ }));
    expect(await screen.findByText("标准日有效产能")).toBeInTheDocument();
    expect(screen.getByText("6.75")).toBeInTheDocument();
    expect(screen.getByText("16.875 标准工时")).toBeInTheDocument();
    expect(screen.getByText("红色预警线（低于）")).toBeInTheDocument();
    expect(screen.getByText("绿色达标线（达到）")).toBeInTheDocument();
    await waitFor(() => expect(fetch).toHaveBeenCalled());
  });

  it("系统设置提供清除缓存和删除历史排班入口", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: /系统设置/ }));

    fireEvent.click(await screen.findByRole("button", { name: "清除缓存" }));
    expect(await screen.findByText(/缓存已清除/)).toBeInTheDocument();
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      "/api/maintenance/clear-cache",
      expect.objectContaining({ method: "POST" }),
    ));

    fireEvent.click(screen.getByRole("button", { name: "删除全部历史排班" }));
    expect(await screen.findByText(/2 个周计划、3 个生产任务、8 条排班明细/)).toBeInTheDocument();
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      "/api/maintenance/schedule-history",
      expect.objectContaining({ method: "DELETE" }),
    ));
  });

  it("员工清单提供永久删除入口并调用安全删除接口", async () => {
    const requests: { path: string; method: string }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        requests.push({ path, method: init?.method ?? "GET" });
        if (path === "/api/employees") {
          return Promise.resolve(
            new Response(
              JSON.stringify([
                {
                  id: 9,
                  name: "测试员工",
                  employee_type: "backup",
                  overtime_limit: null,
                  weekly_work_days: 5,
                  unavailable_weekdays: [5, 6],
                  active: true,
                  skill_part_ids: [],
                },
              ]),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        if (path === "/api/employees/9/permanent") {
          return Promise.resolve(new Response(null, { status: 204 }));
        }
        return Promise.resolve(
          new Response(JSON.stringify(emptyResponses[path] ?? []), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: /员工管理/ }));
    fireEvent.click(await screen.findByRole("button", { name: "删除" }));

    await waitFor(() =>
      expect(requests).toContainEqual({
        path: "/api/employees/9/permanent",
        method: "DELETE",
      }),
    );
  });

  it("员工清单可二次确认后一键删除所有员工", async () => {
    const requests: { path: string; method: string }[] = [];
    let employees = [{
      id: 19,
      name: "批量员工",
      employee_type: "backup",
      overtime_limit: null,
      weekly_work_days: 5,
      unavailable_weekdays: [5, 6],
      active: true,
      skill_part_ids: [],
    }];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        requests.push({ path, method: init?.method ?? "GET" });
        if (path === "/api/employees/all/permanent" && init?.method === "DELETE") {
          employees = [];
          return Promise.resolve(new Response(
            JSON.stringify({ status: "deleted", deleted: 1 }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ));
        }
        const payload = path === "/api/employees" ? employees : emptyResponses[path] ?? [];
        return Promise.resolve(new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }));
      }),
    );
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: /员工管理/ }));
    fireEvent.click(await screen.findByRole("button", { name: "一键删除所有员工" }));

    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(requests).toContainEqual({
      path: "/api/employees/all/permanent",
      method: "DELETE",
    }));
    expect(await screen.findByText("已永久删除全部 1 名员工")).toBeInTheDocument();
  });

  it("已停用员工可从卡片右上角直接启用", async () => {
    const requests: { path: string; method: string; body?: string }[] = [];
    let active = false;
    const employee = {
      id: 10,
      name: "停用员工",
      employee_type: "core",
      overtime_limit: null,
      weekly_work_days: 5,
      unavailable_weekdays: [5, 6],
      active,
      skill_part_ids: [],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        requests.push({ path, method: init?.method ?? "GET", body: init?.body as string | undefined });
        if (path === "/api/employees/10" && init?.method === "PUT") {
          active = true;
          return Promise.resolve(
            new Response(JSON.stringify({ ...employee, active: true }), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }),
          );
        }
        const payload = path === "/api/employees"
          ? [{ ...employee, active }]
          : emptyResponses[path] ?? [];
        return Promise.resolve(
          new Response(JSON.stringify(payload), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: /员工管理/ }));
    fireEvent.click(await screen.findByRole("button", { name: "启用" }));

    await waitFor(() => expect(requests).toContainEqual(expect.objectContaining({
      path: "/api/employees/10",
      method: "PUT",
    })));
    expect(await screen.findByText("员工已启用，将重新参与后续排班")).toBeInTheDocument();
  });

  it("零件管理提供表格导入和模板下载", async () => {
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: /零件管理/ }));
    fireEvent.click(await screen.findByRole("button", { name: "导入表格" }));

    expect(await screen.findByText("批量导入零件")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: /Excel模板/ }),
    );
    expect(await screen.findByText("模板保存成功")).toBeInTheDocument();
    expect(
      screen.getByText("/Users/test/Downloads/零件导入模板.xlsx"),
    ).toBeInTheDocument();
    expect(screen.getByText("选择 .xlsx 或 .csv 文件")).toBeInTheDocument();
  });

  it("员工管理移除长期班次并提示在周排班逐周设置", async () => {
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: /员工管理/ }));
    fireEvent.click(await screen.findByRole("button", { name: /新增员工/ }));

    expect(screen.queryByText("每周工作天数")).not.toBeInTheDocument();
    expect(screen.queryByText("固定不能上班的星期")).not.toBeInTheDocument();
    expect(screen.getByText("出勤安排")).toBeInTheDocument();
    expect(screen.getByText(/默认周一至周五.*周排班.*逐周调整/)).toBeInTheDocument();
    expect(screen.getByText("统一加班规则")).toBeInTheDocument();
    expect(screen.getByText(/不加班.*完整加班4小时/)).toBeInTheDocument();
    expect(screen.queryByText("个人每日加班时间来源")).not.toBeInTheDocument();
  });

  it("员工技能可按零件编号搜索并以两位小数显示工时", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = String(input);
        const payload = path === "/api/parts"
          ? [
              {
                id: 1,
                code: "140502377",
                name: "带微调定位功能的样品夹固定器",
                standard_hours: 0.1667,
                active: true,
              },
              {
                id: 2,
                code: "P-OTHER",
                name: "其它零件",
                standard_hours: 0.5,
                active: true,
              },
            ]
          : emptyResponses[path] ?? [];
        return Promise.resolve(
          new Response(JSON.stringify(payload), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: /员工管理/ }));
    fireEvent.click(await screen.findByRole("button", { name: /新增员工/ }));

    expect(screen.getByText("0.17h / 件")).toBeInTheDocument();
    expect(screen.getByText("其它零件")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("searchbox", { name: "搜索零件编号或名称" }), {
      target: { value: "140502377" },
    });
    expect(screen.getByText("带微调定位功能的样品夹固定器")).toBeInTheDocument();
    expect(screen.queryByText("其它零件")).not.toBeInTheDocument();
  });

  it("整机管理只允许选择整机装配用途零件并填写每台用量", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = String(input);
        let payload: unknown = emptyResponses[path] ?? [];
        if (path === "/api/parts") {
          payload = [
            { id: 1, code: "ASM-001", name: "装配零件", standard_hours: 0.5, usage_types: ["assembly"], active: true },
            { id: 2, code: "ACC-002", name: "附件零件", standard_hours: 0.25, usage_types: ["accessory"], active: true },
          ];
        }
        return Promise.resolve(new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } }));
      }),
    );

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: /整机与BOM/ }));
    fireEvent.click(await screen.findByRole("button", { name: /新增整机/ }));

    expect(screen.getByText("ASM-001")).toBeInTheDocument();
    expect(screen.queryByText("ACC-002")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: /ASM-001/ }));
    expect(screen.getByRole("spinbutton", { name: "ASM-001 每台用量" })).toBeEnabled();
  });

  it("整机管理提供BOM矩阵导入入口和模板说明", async () => {
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: /整机与BOM/ }));
    fireEvent.click(await screen.findByRole("button", { name: "导入BOM矩阵" }));

    expect(await screen.findByText("导入整机BOM矩阵")).toBeInTheDocument();
    expect(screen.getByText(/第一行为整机编号，第二行为整机名称/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下载Excel模板" })).toBeInTheDocument();
  });

  it("生产需求页面提供整机计划、附件订单和跨周一键生成", async () => {
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: /订单与跨周计划/ }));

    expect(await screen.findByText("生产需求与跨周任务")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /整机计划/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /附件订单/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /一键生成所有相关周排班/ })).toBeDisabled();
  });

  it("新增任务日期默认同一天并提供整机周计划矩阵导入", async () => {
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: /订单与跨周计划/ }));

    fireEvent.click(await screen.findByRole("button", { name: /整机计划/ }));
    const startInput = screen.getByText("开始日期").parentElement?.querySelector("input");
    const endInput = screen.getByText("截止日期").parentElement?.querySelector("input");
    expect(startInput).not.toBeNull();
    expect(endInput).not.toBeNull();
    expect((startInput as HTMLInputElement).value).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect((endInput as HTMLInputElement).value).toBe(
      (startInput as HTMLInputElement).value,
    );
    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    fireEvent.click(screen.getByRole("button", { name: "导入整机周计划" }));
    expect(
      await screen.findByRole("heading", { name: "导入整机周计划" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/星期一至星期日/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下载Excel模板" })).toBeInTheDocument();
  });

  it("任务清单提供安全永久删除入口", async () => {
    const requests: { path: string; method: string }[] = [];
    let removed = false;
    const order = {
      id: 21,
      order_type: "accessory",
      source_id: 8,
      source_code: "ACC-DELETE",
      source_name: "待删除附件",
      quantity: 5,
      start_date: "2026-07-20",
      end_date: "2026-07-24",
      status: "active",
      needs_generation: false,
      schedule_status: "unscheduled",
      required_hours: 2.5,
      scheduled_hours: 0,
      remaining_hours: 2.5,
      remaining_quantity: 5,
      confirmed_conflicts: [],
      items: [],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        requests.push({ path, method: init?.method ?? "GET" });
        if (path === "/api/production-orders/21/permanent") {
          removed = true;
          return Promise.resolve(new Response(null, { status: 204 }));
        }
        const payload = path === "/api/production-orders"
          ? (removed ? [] : [order])
          : emptyResponses[path] ?? [];
        return Promise.resolve(new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }));
      }),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: /生产需求/ }));
    fireEvent.click(await screen.findByRole("button", { name: "删除" }));

    await waitFor(() => expect(requests).toContainEqual({
      path: "/api/production-orders/21/permanent",
      method: "DELETE",
    }));
    expect(await screen.findByText("生产任务已永久删除，相关未确认周排班已重新计算")).toBeInTheDocument();
  });

  it("附件订单使用卡片单选全部附件零件并支持按编号搜索", async () => {
    const days = ["2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23", "2026-07-24"];
    const employee = {
      id: 11,
      name: "装配员工",
      employee_type: "core",
      overtime_limit: null,
      weekly_work_days: 5,
      unavailable_weekdays: [5, 6],
      active: true,
      skill_part_ids: [1],
    };
    const draftWeek = {
      id: 3,
      week_start: "2026-07-20",
      include_weekend: false,
      status: "draft",
      confirmed_at: null,
      settings: emptyResponses["/api/settings"],
      days,
      demands: [],
      employees: [{
        ...employee,
        week_assigned_hours: 0,
        days: days.map((date) => ({
          date,
          availability_hours: 7.5,
          normal_capacity: 6.75,
          assigned_hours: 0,
          estimated_actual_hours: 0,
          utilization: 0,
          approved_overtime_hours: 0,
          required_overtime_hours: 0,
        })),
      }],
      assignments: [],
      summary: {
        total_required_hours: 0,
        scheduled_hours: 0,
        remaining_hours: 0,
        shortage_threshold: 16.875,
        unapproved_overload: false,
        has_schedule_data: false,
      },
      shortage: {
        suggestion: null,
        missing_skill_parts: [],
        reinforcement_candidates: [],
        overtime_candidates: [],
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = String(input);
        let payload: unknown = emptyResponses[path] ?? [];
        if (path === "/api/weeks") {
          payload = [{ id: 3, week_start: "2026-07-20", include_weekend: false, status: "draft", required_hours: 0 }];
        } else if (path === "/api/weeks/3") {
          payload = draftWeek;
        } else if (path === "/api/employees") {
          payload = [employee];
        } else if (path === "/api/parts") {
          payload = [
            { id: 1, code: "COVERED-001", name: "已覆盖零件", standard_hours: 0.25, usage_types: ["accessory"], active: true },
            { id: 2, code: "HIDDEN-002", name: "未覆盖零件", standard_hours: 0.5, usage_types: ["accessory"], active: true },
          ];
        }
        return Promise.resolve(
          new Response(JSON.stringify(payload), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: /生产需求/ }));
    fireEvent.click(await screen.findByRole("button", { name: /附件订单/ }));

    const coveredPart = screen.getByRole("checkbox", { name: "COVERED-001 已覆盖零件" });
    const hiddenPart = screen.getByRole("checkbox", { name: "HIDDEN-002 未覆盖零件" });
    expect(coveredPart).not.toBeChecked();
    expect(hiddenPart).not.toBeChecked();

    fireEvent.click(coveredPart);
    expect(coveredPart).toBeChecked();
    fireEvent.click(hiddenPart);
    expect(coveredPart).not.toBeChecked();
    expect(hiddenPart).toBeChecked();

    fireEvent.change(screen.getByRole("searchbox", { name: "搜索附件零件编号或名称" }), {
      target: { value: "HIDDEN-002" },
    });
    expect(screen.queryByRole("checkbox", { name: "COVERED-001 已覆盖零件" })).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "HIDDEN-002 未覆盖零件" })).toBeChecked();
    expect(screen.getByText(/已选择/)).toHaveTextContent("已选择 1 项 · 当前显示 1 项");
  });

  it("导入表格格式错误时在弹窗中持续显示修正提示", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/api/parts/import/preview") {
          return Promise.resolve(
            new Response(JSON.stringify({ detail: "表头缺少：单件标准工时（小时）" }), {
              status: 422,
              headers: { "Content-Type": "application/json" },
            }),
          );
        }
        return Promise.resolve(
          new Response(JSON.stringify(emptyResponses[path] ?? []), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: /零件管理/ }));
    fireEvent.click(await screen.findByRole("button", { name: "导入表格" }));
    const input = document.querySelector<HTMLInputElement>('input[type="file"]');
    expect(input).not.toBeNull();
    fireEvent.change(input!, {
      target: { files: [new File(["编号,名称\nP-001,测试"], "错误表头.csv", { type: "text/csv" })] },
    });

    expect(await screen.findByText("表格格式有误，暂时无法导入")).toBeInTheDocument();
    expect(screen.getByText("表头缺少：单件标准工时（小时）")).toBeInTheDocument();
    expect(screen.getByText(/请使用模板/)).toBeInTheDocument();
  });

  it("已确认周排班可导出PDF并显示保存路径", async () => {
    const requests: { path: string; method: string }[] = [];
    const confirmedWeek = {
      id: 1,
      week_start: "2026-07-20",
      include_weekend: false,
      status: "confirmed",
      confirmed_at: "2026-07-20T10:00:00",
      settings: emptyResponses["/api/settings"],
      days: ["2026-07-20"],
      demands: [{
        id: 1, part_id: 1, part_code: "P-001", part_name: "测试零件",
        standard_hours: 0.5, quantity: 2, assigned_quantity: 2, remaining_quantity: 0,
      }],
      employees: [{
        id: 1, name: "测试员工", employee_type: "core", overtime_limit: null,
        weekly_work_days: 5, unavailable_weekdays: [5, 6],
        active: true, skill_part_ids: [1], week_assigned_hours: 1,
        days: [{
          date: "2026-07-20", availability_hours: 7.5, normal_capacity: 6.75,
          assigned_hours: 1, estimated_actual_hours: 1.111, utilization: 0.148,
          approved_overtime_hours: 0, required_overtime_hours: 0,
        }],
      }],
      daily_efficiency: [{
        date: "2026-07-20", assigned_hours: 1, available_hours: 7.5, efficiency: 0.1333,
      }],
      assignments: [{
        id: 1, employee_id: 1, employee_name: "测试员工", part_id: 1,
        part_code: "P-001", part_name: "测试零件", work_date: "2026-07-20",
        quantity: 2, standard_hours: 0.5, source: "generated",
      }],
      summary: {
        total_required_hours: 1, scheduled_hours: 1, remaining_hours: 0,
        shortage_threshold: 16.875, unapproved_overload: false, has_schedule_data: true,
      },
      shortage: {
        suggestion: null, missing_skill_parts: [], reinforcement_candidates: [], overtime_candidates: [],
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        requests.push({ path, method: init?.method ?? "GET" });
        let payload: unknown = emptyResponses[path] ?? [];
        if (path === "/api/weeks") {
          payload = [{ id: 1, week_start: "2026-07-20", include_weekend: false, status: "confirmed", required_hours: 1 }];
        } else if (path === "/api/weeks/1") {
          payload = confirmedWeek;
        } else if (path === "/api/weeks/1/export") {
          payload = {
            filename: "周排班明细_2026-07-20.pdf",
            path: "/Users/test/Downloads/周排班明细_2026-07-20.pdf",
            format: "pdf",
            page_count: 3,
          };
        }
        return Promise.resolve(
          new Response(JSON.stringify(payload), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );

    render(<App />);
    expect(await screen.findByText("每日整体生产效率")).toBeInTheDocument();
    expect(screen.getByText("低于预警线")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "导出 PDF" }));

    expect(await screen.findByText("导出成功，文件已保存到下载文件夹")).toBeInTheDocument();
    expect(screen.getByText("/Users/test/Downloads/周排班明细_2026-07-20.pdf")).toBeInTheDocument();
    expect(requests).toContainEqual({ path: "/api/weeks/1/export", method: "POST" });
  });

  it("技能无法覆盖缺口时显示零件编号、名称和未排数量", async () => {
    let availabilityBody: Record<string, unknown> | null = null;
    let resolutionBody: Record<string, unknown> | null = null;
    const shortageWeek = {
      id: 6,
      week_start: "2026-07-20",
      include_weekend: false,
      status: "shortage",
      confirmed_at: null,
      settings: emptyResponses["/api/settings"],
      days: ["2026-07-20"],
      demands: [{
        id: 1, part_id: 8, part_code: "MISS-008", part_name: "无人覆盖零件",
        standard_hours: 1, quantity: 3, assigned_quantity: 0, remaining_quantity: 3,
      }],
      employees: [{
        id: 1, name: "现有员工", employee_type: "core", overtime_limit: null,
        weekly_work_days: 5, unavailable_weekdays: [5, 6],
        active: true, skill_part_ids: [], week_assigned_hours: 0,
        days: [{
          date: "2026-07-20", availability_hours: 7.5, normal_capacity: 6.75,
          assigned_hours: 0, estimated_actual_hours: 0, utilization: 0,
          approved_overtime_hours: 0, overtime_is_manual: false, required_overtime_hours: 0,
        }],
      }],
      daily_efficiency: [{
        date: "2026-07-20", assigned_hours: 0, available_hours: 7.5, efficiency: 0,
      }],
      assignments: [],
      summary: {
        total_required_hours: 3, scheduled_hours: 0, remaining_hours: 3,
        shortage_threshold: 16.875, unapproved_overload: false, has_schedule_data: true,
      },
      shortage: {
        suggestion: "no_skill",
        missing_skill_parts: [{
          part_id: 8, part_code: "MISS-008", part_name: "无人覆盖零件", remaining_quantity: 3,
        }],
        reinforcement_candidates: [],
        overtime_candidates: [],
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if (path === "/api/weeks/6/resolve" && init?.method === "POST") {
          resolutionBody = JSON.parse(String(init.body)) as Record<string, unknown>;
          return Promise.resolve(
            new Response(JSON.stringify(shortageWeek), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }),
          );
        }
        if (path === "/api/weeks/6/availability" && init?.method === "PUT") {
          availabilityBody = JSON.parse(String(init.body)) as Record<string, unknown>;
          return Promise.resolve(
            new Response(JSON.stringify(shortageWeek), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }),
          );
        }
        const payload = path === "/api/weeks"
          ? [{ id: 6, week_start: "2026-07-20", include_weekend: false, status: "shortage", required_hours: 3 }]
          : path === "/api/weeks/6"
            ? shortageWeek
            : emptyResponses[path] ?? [];
        return Promise.resolve(
          new Response(JSON.stringify(payload), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );

    render(<App />);
    expect(await screen.findByText(/MISS-008 · 无人覆盖零件/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /现有员工/ }));
    expect(screen.getByText("正常出勤")).toBeInTheDocument();
    expect(screen.getByText("固定加班班次")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "2026-07-20 加班4小时" }));
    fireEvent.click(screen.getByRole("button", { name: "保存本周出勤与加班" }));
    await waitFor(() => expect(availabilityBody).toEqual(expect.objectContaining({
      overtime_entries: [{
        employee_id: 1,
        work_date: "2026-07-20",
        hours: 4,
        manual: true,
      }],
    })));
    fireEvent.click(screen.getByRole("button", { name: "选择处理方式" }));
    expect(screen.getByText("当前员工未覆盖的零件")).toBeInTheDocument();
    expect(screen.getByText("未排 3 件")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /使用员工2\/3/ }));
    expect(screen.getByText(/整机任务仍只安排在目标日/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "授权员工2/3并重排" }));
    await waitFor(() => expect(resolutionBody).toEqual({
      mode: "alternate",
      employee_ids: [],
    }));
  });

  it("零件清单提供永久删除入口", async () => {
    const requests: { path: string; method: string }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        requests.push({ path, method: init?.method ?? "GET" });
        if (path === "/api/parts") {
          return Promise.resolve(
            new Response(
              JSON.stringify([
                {
                  id: 7,
                  code: "P-007",
                  name: "测试零件",
                  standard_hours: 0.5,
                  active: true,
                },
              ]),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        if (path === "/api/parts/7/permanent") {
          return Promise.resolve(new Response(null, { status: 204 }));
        }
        return Promise.resolve(
          new Response(JSON.stringify(emptyResponses[path] ?? []), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: /零件管理/ }));
    fireEvent.click(await screen.findByRole("button", { name: "删除" }));

    await waitFor(() =>
      expect(requests).toContainEqual({
        path: "/api/parts/7/permanent",
        method: "DELETE",
      }),
    );
  });

  it("零件清单可二次确认后一键删除所有零件", async () => {
    const requests: { path: string; method: string }[] = [];
    let parts = [{
      id: 17,
      code: "BULK-017",
      name: "批量零件",
      standard_hours: 0.5,
      usage_types: ["accessory"],
      active: true,
    }];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        requests.push({ path, method: init?.method ?? "GET" });
        if (path === "/api/parts/all/permanent" && init?.method === "DELETE") {
          parts = [];
          return Promise.resolve(new Response(
            JSON.stringify({ status: "deleted", deleted: 1 }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ));
        }
        const payload = path === "/api/parts" ? parts : emptyResponses[path] ?? [];
        return Promise.resolve(new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }));
      }),
    );
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: /零件管理/ }));
    fireEvent.click(await screen.findByRole("button", { name: "一键删除所有零件" }));

    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(requests).toContainEqual({
      path: "/api/parts/all/permanent",
      method: "DELETE",
    }));
    expect(await screen.findByText("已永久删除全部 1 个零件")).toBeInTheDocument();
  });
});
