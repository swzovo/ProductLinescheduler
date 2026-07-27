import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const root = "/Users/shengwenzhe/Documents/生产线排班程序";
const outputDir = `${root}/outputs/production-scheduler-20260726`;
const assetDir = `${root}/backend/assets`;
const previewDir = `${root}/tmp/spreadsheets`;

const colors = {
  dark: "#0C4A3E",
  green: "#DDEFE8",
  line: "#D7E3DE",
  muted: "#5E716B",
  peach: "#F7D0B5",
  white: "#FFFFFF",
};

function styleInputSheet(sheet, headerRange, bodyRange, widths) {
  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(1);
  headerRange.format = {
    fill: colors.dark,
    font: { bold: true, color: colors.white, size: 11 },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    borders: { preset: "outside", style: "thin", color: colors.dark },
  };
  headerRange.format.rowHeight = 30;
  bodyRange.format = {
    fill: colors.white,
    font: { color: "#173B34", size: 10 },
    verticalAlignment: "center",
    borders: {
      insideHorizontal: { style: "thin", color: colors.line },
      bottom: { style: "thin", color: colors.line },
    },
  };
  bodyRange.format.rowHeight = 24;
  widths.forEach((width, index) => {
    sheet.getRangeByIndexes(0, index, 201, 1).format.columnWidth = width;
  });
}

function addInstructions(sheet, title, rows) {
  sheet.showGridLines = false;
  sheet.getRange("A1:B1").merge();
  sheet.getRange("A1").values = [[title]];
  sheet.getRange("A1:B1").format = {
    fill: colors.dark,
    font: { bold: true, color: colors.white, size: 16 },
    verticalAlignment: "center",
  };
  sheet.getRange("A1:B1").format.rowHeight = 34;
  sheet.getRange("A3:B3").values = [["字段", "填写要求"]];
  sheet.getRange("A3:B3").format = {
    fill: colors.green,
    font: { bold: true, color: colors.dark },
    borders: { preset: "outside", style: "thin", color: colors.line },
  };
  sheet.getRange(`A4:B${rows.length + 3}`).values = rows;
  sheet.getRange(`A4:B${rows.length + 3}`).format = {
    verticalAlignment: "top",
    wrapText: true,
    borders: {
      insideHorizontal: { style: "thin", color: colors.line },
      bottom: { style: "thin", color: colors.line },
    },
  };
  sheet.getRange(`A4:A${rows.length + 3}`).format.font = {
    bold: true,
    color: colors.dark,
  };
  sheet.getRange("A:A").format.columnWidth = 24;
  sheet.getRange("B:B").format.columnWidth = 72;
  sheet.getRange(`A1:B${rows.length + 3}`).format.autofitRows();
}

async function saveAndVerify(workbook, filename, sheetName, previewRange) {
  const inspect = await workbook.inspect({
    kind: "table",
    range: `${sheetName}!${previewRange}`,
    include: "values,formulas",
    tableMaxRows: 15,
    tableMaxCols: 10,
  });
  process.stdout.write(`${inspect.ndjson}\n`);
  const errors = await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 100 },
    summary: `${filename} formula error scan`,
  });
  process.stdout.write(`${errors.ndjson}\n`);
  const preview = await workbook.render({
    sheetName,
    range: previewRange,
    scale: 1.5,
    format: "png",
  });
  await fs.writeFile(
    `${previewDir}/${filename.replace(".xlsx", "")}.png`,
    new Uint8Array(await preview.arrayBuffer()),
  );
  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(`${outputDir}/${filename}`);
  await output.save(`${assetDir}/${filename}`);
}

await fs.mkdir(outputDir, { recursive: true });

const partWorkbook = Workbook.create();
const partSheet = partWorkbook.worksheets.add("零件导入");
partSheet.getRange("A1:G1").values = [[
  "零件编号",
  "零件名称",
  "单件标准工时（小时）",
  "启用状态",
  "零件用途",
  "员工1",
  "员工2",
]];
styleInputSheet(
  partSheet,
  partSheet.getRange("A1:G1"),
  partSheet.getRange("A2:G201"),
  [20, 34, 22, 16, 20, 22, 22],
);
partSheet.getRange("A2:A201").setNumberFormat("@");
partSheet.getRange("C2:C201").setNumberFormat("0.00");
partSheet.getRange("D2:D201").dataValidation = {
  rule: { type: "list", values: ["启用", "停用"] },
};
partSheet.getRange("E2:E201").dataValidation = {
  rule: { type: "list", values: ["附件", "整机装配", "双用途"] },
};
const partInstructions = partWorkbook.worksheets.add("填写说明");
addInstructions(partInstructions, "零件导入模板 · 填写说明", [
  ["零件编号", "必填；建议使用文本格式，最长40个字符。同编号会更新现有零件。"],
  ["零件名称", "必填；最长100个字符。"],
  ["单件标准工时（小时）", "必填；填写大于0且不超过1000的数字，例如0.25。"],
  ["启用状态", "选填；填写启用或停用。新增零件留空时默认启用。"],
  ["零件用途", "选填；填写附件、整机装配或双用途。新增零件留空时默认附件。"],
  ["员工1", "选填；只能填写一名员工。整机任务优先交给员工1。不存在的员工会自动创建为固定成员。"],
  ["员工2", "选填；只能填写一名员工，且不能与员工1相同。员工1容量不足时承接整机任务；双用途附件只能由员工2完成。"],
  ["旧模板兼容", "旧版“员工”列仍可导入，多名员工用顿号分隔；前两名初始化为员工1和员工2，其余技能保留。"],
  ["导入限制", "只读取第一张工作表；不支持公式；最多5000条；文件不超过5MB。"],
  ["操作顺序", "填写“零件导入”工作表 → 保存 → 软件中选择“导入表格” → 检查预览 → 确认导入。"],
]);
await saveAndVerify(partWorkbook, "零件导入模板.xlsx", "零件导入", "A1:G12");

const orderWorkbook = Workbook.create();
const orderSheet = orderWorkbook.worksheets.add("附件订单导入");
orderSheet.getRange("A1:D1").values = [[
  "零件编号",
  "数量",
  "开始日期",
  "截止日期",
]];
styleInputSheet(
  orderSheet,
  orderSheet.getRange("A1:D1"),
  orderSheet.getRange("A2:D201"),
  [22, 16, 20, 20],
);
orderSheet.getRange("A2:A201").setNumberFormat("@");
orderSheet.getRange("B2:B201").setNumberFormat("#,##0");
orderSheet.getRange("C2:D201").setNumberFormat("yyyy-mm-dd");
orderSheet.getRange("B2:B201").dataValidation = {
  rule: { type: "whole", operator: "between", formula1: 1, formula2: 10000000 },
};
const orderInstructions = orderWorkbook.worksheets.add("填写说明");
addInstructions(orderInstructions, "附件订单导入模板 · 填写说明", [
  ["零件编号", "必填；必须是零件管理中已启用且具备“附件”用途的零件编号。"],
  ["数量", "必填；填写大于0且不超过10000000的整数。"],
  ["开始日期", "必填；使用Excel日期或YYYY-MM-DD格式。附件任务从该日期起向后填充。"],
  ["截止日期", "必填；不得早于开始日期，任务不会安排到该日期之后。"],
  ["一行一单", "每一行会创建一条独立附件订单；相同零件和日期可以分多行录入。"],
  ["导入限制", "只读取第一张工作表；不支持公式；最多5000条；文件不超过5MB。"],
  ["操作顺序", "填写“附件订单导入”工作表 → 保存 → 生产需求中选择“导入附件订单” → 检查预览 → 确认导入。"],
]);
await saveAndVerify(
  orderWorkbook,
  "附件订单导入模板.xlsx",
  "附件订单导入",
  "A1:D12",
);
