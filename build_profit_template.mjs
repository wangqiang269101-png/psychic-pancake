import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const outputDir = "/Users/wangyongqiang/Desktop/codex/profit-cloud-deploy/outputs";
await fs.mkdir(outputDir, { recursive: true });

const wb = Workbook.create();
const ws = wb.worksheets.add("日报图片");
ws.showGridLines = false;
const inbox = wb.worksheets.add("数据接收");
const guide = wb.worksheets.add("部署说明");

const yellow = "#FFC800";
const black = "#1E1E1E";
const white = "#FFFFFF";
const cream = "#FFF8DE";
const paleYellow = "#FFF3C4";
const paleRed = "#FCE9E3";
const green = "#34A853";
const red = "#E4474F";
const gray = "#6A6A6A";
const line = "#E6D89B";

ws.getRange("A1:O48").format.fill = yellow;
ws.getRange("A1:O48").format.font = { name: "Microsoft YaHei", color: black, size: 11 };
ws.getRange("A1:O48").format.verticalAlignment = "center";

for (const col of "ABCDEFGHIJKLMNO") {
  ws.getRange(`${col}:${col}`).format.columnWidthPx = 64;
}
for (let r = 1; r <= 48; r += 1) {
  ws.getRange(`${r}:${r}`).format.rowHeightPx = r <= 5 ? 34 : 27;
}

function merge(range, value, format = {}) {
  const r = ws.getRange(range);
  r.merge();
  r.values = [[value]];
  r.format = {
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    ...format,
  };
  return r;
}

function box(range, fill, border = line) {
  const r = ws.getRange(range);
  r.format.fill = fill;
  r.format.borders = { preset: "outside", style: "medium", color: border };
  return r;
}

// Top title band.
box("A1:O5", yellow, yellow);
merge("A2:I4", "每日利润播报\n公司经营数据快报", {
  fill: black,
  font: { name: "Microsoft YaHei", color: white, size: 22, bold: false },
  horizontalAlignment: "left",
  indentLevel: 1,
});
merge("L2:O3", "2026-06-21", {
  fill: white,
  font: { name: "Microsoft YaHei", color: black, size: 15 },
  borders: { preset: "outside", style: "medium", color: white },
});

// Monthly summary card.
box("A6:O16", white);
merge("A7:F8", "公司维度｜本月累计", {
  fill: black,
  font: { color: white, size: 14 },
  horizontalAlignment: "left",
  indentLevel: 1,
});
merge("A9:G14", "444.40 万元\n累计权责总利润", {
  fill: white,
  font: { color: black, size: 25 },
  horizontalAlignment: "left",
  indentLevel: 1,
});
merge("I8:O10", "累计外卖利润\n480.24 万元", {
  fill: paleYellow,
  font: { color: green, size: 15 },
});
merge("I12:O14", "累计团购利润\n-35.84 万元", {
  fill: paleRed,
  font: { color: red, size: 15 },
});

// Daily KPI cards.
box("A17:E24", white);
box("F17:J24", white);
box("K17:O24", white);
merge("A18:E19", "公司当日总利润", { font: { size: 13 } });
merge("F18:J19", "公司当日外卖", { font: { size: 13 } });
merge("K18:O19", "公司当日团购", { font: { size: 13 } });
merge("A20:E23", "53.49\n万元", { font: { color: black, size: 24 } });
merge("F20:J23", "56.38\n万元", { font: { color: green, size: 24 } });
merge("K20:O23", "-2.89\n万元", { font: { color: red, size: 24 } });

// Region table.
box("A25:O44", white);
merge("A25:O26", "区域维度｜当日实际利润明细                                      单位：万元", {
  fill: black,
  font: { color: white, size: 13 },
  horizontalAlignment: "left",
  indentLevel: 1,
});

const headers = [["排名", "区域", "", "", "", "", "总利润", "", "", "外卖", "", "", "团购", "", ""]];
ws.getRange("A27:O27").values = headers;
ws.getRange("A27:O27").format = {
  fill: cream,
  font: { bold: true, size: 11 },
  horizontalAlignment: "center",
  verticalAlignment: "center",
  borders: { preset: "inside", style: "thin", color: line },
};
merge("B27:F27", "区域", { fill: cream, font: { bold: true } });
merge("G27:I27", "总利润", { fill: cream, font: { bold: true } });
merge("J27:L27", "外卖", { fill: cream, font: { bold: true } });
merge("M27:O27", "团购", { fill: cream, font: { bold: true } });

const regionRows = [
  [1, "川藏一区", 116.35, 124.79, -8.43],
  [2, "川藏二区", 104.65, 110.31, -5.65],
  [3, "江西二区", 98.60, 101.98, -3.38],
  [4, "川藏三区", 36.97, 36.15, 0.83],
  [5, "江西一区", 32.58, 43.32, -10.74],
  [6, "湖北区域", 30.93, 31.22, -0.30],
  [7, "粤海区域", 11.40, 10.72, 0.67],
  [8, "河南区域", 9.47, 13.91, -4.44],
  [9, "福建&黔渝区域", 3.44, 7.85, -4.41],
];

for (let i = 0; i < regionRows.length; i += 1) {
  const row = 28 + i * 2;
  const [rank, region, total, delivery, group] = regionRows[i];
  const fill = i % 2 === 0 ? white : cream;
  merge(`A${row}:A${row + 1}`, rank, {
    fill: rank >= 8 ? red : yellow,
    font: { color: rank >= 8 ? white : black, bold: true },
  });
  merge(`B${row}:F${row + 1}`, region, {
    fill,
    horizontalAlignment: "left",
    indentLevel: 1,
    font: { size: 12 },
  });
  merge(`G${row}:I${row + 1}`, total, {
    fill,
    font: { color: total < 0 ? red : black, size: 12 },
    numberFormat: "0.00",
  });
  merge(`J${row}:L${row + 1}`, delivery, {
    fill,
    font: { color: delivery < 0 ? red : green, size: 12 },
    numberFormat: "0.00",
  });
  merge(`M${row}:O${row + 1}`, group, {
    fill,
    font: { color: group < 0 ? red : green, size: 12 },
    numberFormat: "0.00",
  });
}

// Footer.
box("A45:O48", yellow, white);
merge("A46:G47", "Power BI｜T−1 财务利润", {
  fill: yellow,
  font: { color: black, size: 11 },
  horizontalAlignment: "left",
  indentLevel: 1,
});
merge("I46:O47", "公司整体 · 区域 · 外卖 · 团购", {
  fill: yellow,
  font: { color: black, size: 11 },
  horizontalAlignment: "right",
  indentLevel: 1,
});

// Power Automate writes one JSON payload per run to this inbox.
inbox.showGridLines = false;
inbox.getRange("A1:F1").values = [[
  "接收时间",
  "数据日期",
  "PayloadJSON",
  "处理状态",
  "处理时间",
  "错误信息",
]];
inbox.getRange("A1:F1").format = {
  fill: black,
  font: { color: white, bold: true, size: 11 },
  horizontalAlignment: "center",
  verticalAlignment: "center",
  borders: { preset: "outside", style: "medium", color: black },
};
inbox.getRange("A2:F1000").format = {
  fill: white,
  font: { color: black, size: 10 },
  verticalAlignment: "top",
  borders: { preset: "inside", style: "thin", color: "#E8E8E8" },
};
inbox.getRange("A2:A1000").format.numberFormat = "yyyy-mm-dd hh:mm:ss";
inbox.getRange("B2:B1000").format.numberFormat = "yyyy-mm-dd";
inbox.getRange("A:A").format.columnWidthPx = 150;
inbox.getRange("B:B").format.columnWidthPx = 110;
inbox.getRange("C:C").format.columnWidthPx = 520;
inbox.getRange("D:D").format.columnWidthPx = 110;
inbox.getRange("E:E").format.columnWidthPx = 150;
inbox.getRange("F:F").format.columnWidthPx = 300;
inbox.getRange("1:1").format.rowHeightPx = 30;
inbox.freezePanes.freezeRows(1);

guide.showGridLines = false;
guide.getRange("A1:F20").format.fill = white;
guide.getRange("A1:F3").merge();
guide.getRange("A1").values = [["每日利润云端推送｜部署说明"]];
guide.getRange("A1:F3").format = {
  fill: yellow,
  font: { color: black, size: 22, bold: true },
  horizontalAlignment: "left",
  verticalAlignment: "center",
};
guide.getRange("A5:B10").values = [
  ["项目", "配置"],
  ["数据入口", "Power Automate 在“数据接收”页新增一行"],
  ["图片模板", "固定黄色模板；图片中不展示预计利润"],
  ["文字消息", "当日、本月累计、本月预计"],
  ["处理方式", "Apps Script 每 5 分钟检查一次，仅处理新数据"],
  ["安全", "企业微信 Webhook 仅保存在脚本属性中"],
];
guide.getRange("A5:B5").format = {
  fill: black,
  font: { color: white, bold: true },
};
guide.getRange("A6:B10").format = {
  borders: { preset: "inside", style: "thin", color: "#E8E8E8" },
  verticalAlignment: "top",
  wrapText: true,
};
guide.getRange("A:A").format.columnWidthPx = 150;
guide.getRange("B:B").format.columnWidthPx = 560;
guide.getRange("5:10").format.rowHeightPx = 34;

const preview = await wb.render({
  sheetName: "日报图片",
  range: "A1:O48",
  scale: 1.35,
  format: "png",
});
await fs.writeFile(`${outputDir}/每日利润播报模板.png`, new Uint8Array(await preview.arrayBuffer()));

for (const [sheetName, range, filename] of [
  ["数据接收", "A1:F12", "数据接收预览.png"],
  ["部署说明", "A1:F10", "部署说明预览.png"],
]) {
  const sheetPreview = await wb.render({
    sheetName,
    range,
    scale: 1.2,
    format: "png",
  });
  await fs.writeFile(
    `${outputDir}/${filename}`,
    new Uint8Array(await sheetPreview.arrayBuffer()),
  );
}

const check = await wb.inspect({
  kind: "table",
  range: "日报图片!A1:O48",
  include: "values,formulas",
  tableMaxRows: 48,
  tableMaxCols: 15,
  maxChars: 8000,
});
await fs.writeFile(`${outputDir}/template_check.ndjson`, check.ndjson, "utf8");

const errors = await wb.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 50 },
  summary: "final formula error scan",
});
await fs.writeFile(`${outputDir}/formula_errors.ndjson`, errors.ndjson, "utf8");

const xlsx = await SpreadsheetFile.exportXlsx(wb);
await xlsx.save(`${outputDir}/每日利润播报云端模板.xlsx`);
