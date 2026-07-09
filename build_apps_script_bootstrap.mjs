import fs from "node:fs/promises";
import path from "node:path";

const root = "/Users/wangyongqiang/Desktop/codex/profit-cloud-deploy";
const outputs = path.join(root, "outputs");
const sourceCodePath = path.join(root, "Code.gs");
const xlsxPath = path.join(outputs, "每日利润播报云端模板.xlsx");
const pptxPath = path.join(outputs, "每日利润播报云端画布.pptx");
const outCodePath = path.join(root, "Code.bootstrap.gs");
const outManifestPath = path.join(root, "appsscript.bootstrap.json");

const original = await fs.readFile(sourceCodePath, "utf8");
const logicStart = original.indexOf("function normalizeProfitPayload(payload)");
if (logicStart === -1) {
  throw new Error("Cannot locate normalizeProfitPayload in Code.gs");
}
const sharedLogic = original.slice(logicStart);

function splitBase64(buffer, size = 7000) {
  const b64 = buffer.toString("base64");
  const parts = [];
  for (let i = 0; i < b64.length; i += size) {
    parts.push(b64.slice(i, i + size));
  }
  return parts;
}

const xlsxParts = splitBase64(await fs.readFile(xlsxPath));
const pptxParts = splitBase64(await fs.readFile(pptxPath));

const header = `const CONFIG = {
  inboxSheet: '数据接收',
  statusPending: '',
  statusSuccess: 'SUCCESS',
  statusFailed: 'FAILED',
  statusSkipped: 'SKIPPED',
  timezone: 'Asia/Shanghai',
  maxRowsPerRun: 10,
};

const EMBEDDED_WORKBOOK = {
  title: '每日利润播报云端数据',
  sourceMime: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  targetMime: 'application/vnd.google-apps.spreadsheet',
  base64Parts: ${JSON.stringify(xlsxParts, null, 2)},
};

const EMBEDDED_PRESENTATION = {
  title: '每日利润播报云端画布',
  sourceMime: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  targetMime: 'application/vnd.google-apps.presentation',
  base64Parts: ${JSON.stringify(pptxParts, null, 2)},
};

/**
 * 首次部署时运行一次。
 * 它会自动在 Google Drive 中生成原生 Google Sheet 和 Google Slides 模板，
 * 保存企业微信 webhook，并创建每 5 分钟扫描一次的触发器。
 */
function bootstrapProfitCloud(webhookUrl) {
  validateWebhook_(webhookUrl);

  const props = PropertiesService.getScriptProperties();
  let spreadsheetId = props.getProperty('SPREADSHEET_ID');
  let templatePresentationId = props.getProperty('TEMPLATE_PRESENTATION_ID');

  if (!spreadsheetId) {
    const createdSheet = uploadAndConvertEmbeddedFile_(EMBEDDED_WORKBOOK);
    spreadsheetId = createdSheet.id;
  }

  if (!templatePresentationId) {
    const createdPresentation = uploadAndConvertEmbeddedFile_(EMBEDDED_PRESENTATION);
    templatePresentationId = createdPresentation.id;
  }

  props.setProperties({
    WECOM_WEBHOOK: webhookUrl,
    SPREADSHEET_ID: spreadsheetId,
    TEMPLATE_PRESENTATION_ID: templatePresentationId,
  }, false);

  resetProfitTrigger_();
  return getDeploymentInfo();
}

/**
 * 如果你已经手工创建好了 Sheet/Slides，也可以用这个函数直接绑定。
 */
function setupProfitPush(webhookUrl, templatePresentationId, spreadsheetId) {
  validateWebhook_(webhookUrl);
  if (!templatePresentationId) throw new Error('缺少 Google Slides 模板 ID。');
  if (!spreadsheetId) throw new Error('缺少 Google Sheet ID。');

  PropertiesService.getScriptProperties().setProperties({
    WECOM_WEBHOOK: webhookUrl,
    SPREADSHEET_ID: spreadsheetId,
    TEMPLATE_PRESENTATION_ID: templatePresentationId,
  }, false);

  resetProfitTrigger_();
  return getDeploymentInfo();
}

function getDeploymentInfo() {
  const props = PropertiesService.getScriptProperties();
  const spreadsheetId = props.getProperty('SPREADSHEET_ID');
  const templatePresentationId = props.getProperty('TEMPLATE_PRESENTATION_ID');
  return {
    configured: Boolean(props.getProperty('WECOM_WEBHOOK') && spreadsheetId && templatePresentationId),
    spreadsheetId,
    spreadsheetUrl: spreadsheetId ? 'https://docs.google.com/spreadsheets/d/' + spreadsheetId + '/edit' : '',
    templatePresentationId,
    templatePresentationUrl: templatePresentationId ? 'https://docs.google.com/presentation/d/' + templatePresentationId + '/edit' : '',
    trigger: '每 5 分钟检查一次，仅在“数据接收”页出现新数据时发送',
  };
}

function resetProfitTrigger_() {
  ScriptApp.getProjectTriggers()
    .filter((trigger) => trigger.getHandlerFunction() === 'processPendingProfitRows')
    .forEach((trigger) => ScriptApp.deleteTrigger(trigger));

  ScriptApp.newTrigger('processPendingProfitRows')
    .timeBased()
    .everyMinutes(5)
    .create();
}

function validateWebhook_(webhookUrl) {
  if (!webhookUrl || !/^https:\\/\\/qyapi\\.weixin\\.qq\\.com\\/cgi-bin\\/webhook\\/send\\?key=/.test(webhookUrl)) {
    throw new Error('企业微信 Webhook 格式不正确。');
  }
}

function uploadAndConvertEmbeddedFile_(embedded) {
  const bytes = Utilities.base64Decode(embedded.base64Parts.join(''));
  const boundary = 'codex_profit_' + Date.now();
  const metadata = {
    name: embedded.title,
    mimeType: embedded.targetMime,
  };

  const payload = concatBytes_([
    stringBytes_('--' + boundary + '\\r\\n'),
    stringBytes_('Content-Type: application/json; charset=UTF-8\\r\\n\\r\\n'),
    stringBytes_(JSON.stringify(metadata) + '\\r\\n'),
    stringBytes_('--' + boundary + '\\r\\n'),
    stringBytes_('Content-Type: ' + embedded.sourceMime + '\\r\\n\\r\\n'),
    bytes,
    stringBytes_('\\r\\n--' + boundary + '--'),
  ]);

  const response = UrlFetchApp.fetch(
    'https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,mimeType,webViewLink',
    {
      method: 'post',
      contentType: 'multipart/related; boundary=' + boundary,
      headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
      payload,
      muteHttpExceptions: true,
    },
  );

  const text = response.getContentText();
  if (response.getResponseCode() < 200 || response.getResponseCode() >= 300) {
    throw new Error('Google Drive 转换上传失败：HTTP ' + response.getResponseCode() + ' ' + text.slice(0, 500));
  }

  const file = JSON.parse(text);
  if (file.mimeType !== embedded.targetMime) {
    throw new Error('Google Drive 未完成原生格式转换：' + JSON.stringify(file));
  }
  return file;
}

function concatBytes_(segments) {
  const out = [];
  segments.forEach((segment) => {
    for (let i = 0; i < segment.length; i += 1) out.push(segment[i]);
  });
  return out;
}

function stringBytes_(text) {
  return Utilities.newBlob(text).getBytes();
}

function getDataSpreadsheet_() {
  const id = PropertiesService.getScriptProperties().getProperty('SPREADSHEET_ID');
  if (!id) throw new Error('尚未配置 Google Sheet ID，请先运行 bootstrapProfitCloud(webhookUrl)。');
  return SpreadsheetApp.openById(id);
}

/**
 * 由时间触发器执行。处理“数据接收”页中尚未处理的新记录。
 */
function processPendingProfitRows() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;

  try {
    const sheet = getDataSpreadsheet_().getSheetByName(CONFIG.inboxSheet);
    if (!sheet) throw new Error(\`未找到工作表：\${CONFIG.inboxSheet}\`);

    const lastRow = sheet.getLastRow();
    if (lastRow < 2) return;

    const startRow = Math.max(2, lastRow - CONFIG.maxRowsPerRun + 1);
    const values = sheet.getRange(startRow, 1, lastRow - startRow + 1, 6).getValues();

    for (let offset = values.length - 1; offset >= 0; offset -= 1) {
      const row = values[offset];
      const rowNumber = startRow + offset;
      const payload = row[2];
      const status = String(row[3] || '').trim();

      if (!payload || status) continue;

      try {
        const report = normalizeProfitPayload(payload);
        validateProfitReport(report);

        const props = PropertiesService.getScriptProperties();
        const lastSentDate = props.getProperty('LAST_SENT_DATA_DATE');
        if (lastSentDate === report.dateText) {
          markRow(sheet, rowNumber, CONFIG.statusSkipped, \`数据日期 \${report.dateText} 已发送，跳过重复记录。\`);
          continue;
        }

        const messageText = buildMessageText(report);
        const imageBlob = renderProfitImage(report);

        sendWeComMarkdown(messageText);
        sendWeComImage(imageBlob);

        props.setProperty('LAST_SENT_DATA_DATE', report.dateText);
        markRow(sheet, rowNumber, CONFIG.statusSuccess, '');
      } catch (error) {
        markRow(sheet, rowNumber, CONFIG.statusFailed, error && error.message ? error.message : String(error));
      }
    }
  } finally {
    lock.releaseLock();
  }
}

/**
 * 部署后用于端到端测试。仅处理最新一行，并允许重复发送同一数据日期。
 */
function forceProcessLatestProfitRow() {
  const sheet = getDataSpreadsheet_().getSheetByName(CONFIG.inboxSheet);
  if (!sheet || sheet.getLastRow() < 2) throw new Error('数据接收页暂无数据。');

  const rowNumber = sheet.getLastRow();
  const payload = sheet.getRange(rowNumber, 3).getValue();
  const report = normalizeProfitPayload(payload);
  validateProfitReport(report);
  sendWeComMarkdown(buildMessageText(report));
  sendWeComImage(renderProfitImage(report));
  markRow(sheet, rowNumber, CONFIG.statusSuccess, '人工端到端测试发送成功。');
}

`;

const manifest = {
  timeZone: "Asia/Shanghai",
  dependencies: {},
  exceptionLogging: "STACKDRIVER",
  runtimeVersion: "V8",
  oauthScopes: [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/script.external_request",
    "https://www.googleapis.com/auth/script.scriptapp",
  ],
};

await fs.writeFile(outCodePath, header + sharedLogic, "utf8");
await fs.writeFile(outManifestPath, JSON.stringify(manifest, null, 2) + "\n", "utf8");

console.log(JSON.stringify({
  outCodePath,
  outManifestPath,
  codeBytes: Buffer.byteLength(header + sharedLogic),
  xlsxChunks: xlsxParts.length,
  pptxChunks: pptxParts.length,
}, null, 2));
