const CONFIG = {
  inboxSheet: '数据接收',
  statusPending: '',
  statusSuccess: 'SUCCESS',
  statusFailed: 'FAILED',
  statusSkipped: 'SKIPPED',
  timezone: 'Asia/Shanghai',
  maxRowsPerRun: 10,
};

/**
 * 首次部署时运行一次。
 * webhookUrl 和 templatePresentationId 会被保存到脚本属性，不写入表格。
 */
function setupProfitPush(webhookUrl, templatePresentationId) {
  if (!webhookUrl || !/^https:\/\/qyapi\.weixin\.qq\.com\/cgi-bin\/webhook\/send\?key=/.test(webhookUrl)) {
    throw new Error('企业微信 Webhook 格式不正确。');
  }
  if (!templatePresentationId) {
    throw new Error('缺少 Google Slides 模板 ID。');
  }

  const props = PropertiesService.getScriptProperties();
  props.setProperties({
    WECOM_WEBHOOK: webhookUrl,
    TEMPLATE_PRESENTATION_ID: templatePresentationId,
  }, false);

  ScriptApp.getProjectTriggers()
    .filter((trigger) => trigger.getHandlerFunction() === 'processPendingProfitRows')
    .forEach((trigger) => ScriptApp.deleteTrigger(trigger));

  ScriptApp.newTrigger('processPendingProfitRows')
    .timeBased()
    .everyMinutes(5)
    .create();

  return {
    configured: true,
    trigger: '每 5 分钟检查一次，仅在出现新数据时发送',
  };
}

/**
 * 由时间触发器执行。处理“数据接收”页中尚未处理的新记录。
 */
function processPendingProfitRows() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;

  try {
    const sheet = SpreadsheetApp.getActive().getSheetByName(CONFIG.inboxSheet);
    if (!sheet) throw new Error(`未找到工作表：${CONFIG.inboxSheet}`);

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
          markRow(sheet, rowNumber, CONFIG.statusSkipped, `数据日期 ${report.dateText} 已发送，跳过重复记录。`);
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
  const sheet = SpreadsheetApp.getActive().getSheetByName(CONFIG.inboxSheet);
  if (!sheet || sheet.getLastRow() < 2) throw new Error('数据接收页暂无数据。');

  const rowNumber = sheet.getLastRow();
  const payload = sheet.getRange(rowNumber, 3).getValue();
  const report = normalizeProfitPayload(payload);
  validateProfitReport(report);
  sendWeComMarkdown(buildMessageText(report));
  sendWeComImage(renderProfitImage(report));
  markRow(sheet, rowNumber, CONFIG.statusSuccess, '人工端到端测试发送成功。');
}

function normalizeProfitPayload(payload) {
  let raw = payload;
  if (typeof raw === 'string') raw = JSON.parse(raw);

  let rows = raw;
  if (!Array.isArray(rows)) {
    rows =
      (((raw || {}).results || [])[0] || {}).tables?.[0]?.rows ||
      (raw || {}).firstTableRows ||
      (raw || {}).rows ||
      (raw || {}).value ||
      [];
  }
  if (!Array.isArray(rows)) throw new Error('Power BI 数据不是可识别的行数组。');

  const normalizedRows = rows.map((row) => {
    const out = {};
    Object.keys(row || {}).forEach((key) => {
      const cleanKey = String(key)
        .replace(/^.*\[/, '')
        .replace(/\]$/, '')
        .replace(/^['"]|['"]$/g, '');
      out[cleanKey] = row[key];
    });
    return out;
  });

  const companyRow = normalizedRows.find((row) => String(row.类型 || '').trim() === '公司');
  const regionRows = normalizedRows
    .filter((row) => String(row.类型 || '').trim() === '区域' && row.区域)
    .map((row) => ({
      name: String(row.区域),
      total: toNumber(row.当日总利润),
      delivery: toNumber(row.当日外卖利润),
      group: toNumber(row.当日团购利润),
    }))
    .sort((a, b) => b.total - a.total)
    .slice(0, 9);

  if (!companyRow) throw new Error('未找到“类型=公司”的汇总行。');

  const date = parseDate(companyRow.日期);
  const daysInMonth = new Date(date.getFullYear(), date.getMonth() + 1, 0).getDate();
  const accountedDays = date.getDate();
  const monthTotal = toNumber(companyRow.月累计总利润);
  const monthDelivery = toNumber(companyRow.月累计外卖利润);
  const monthGroup = toNumber(companyRow.月累计团购利润);

  return {
    date,
    dateText: Utilities.formatDate(date, CONFIG.timezone, 'yyyy-MM-dd'),
    company: {
      dailyTotal: toNumber(companyRow.当日总利润),
      dailyDelivery: toNumber(companyRow.当日外卖利润),
      dailyGroup: toNumber(companyRow.当日团购利润),
      monthTotal,
      monthDelivery,
      monthGroup,
      forecastTotal: numberOrFallback(
        companyRow.本月预计总利润,
        monthTotal / accountedDays * daysInMonth,
      ),
      forecastDelivery: numberOrFallback(
        companyRow.本月预计外卖利润,
        monthDelivery / accountedDays * daysInMonth,
      ),
      forecastGroup: numberOrFallback(
        companyRow.本月预计团购利润,
        monthGroup / accountedDays * daysInMonth,
      ),
    },
    regions: regionRows,
  };
}

function validateProfitReport(report) {
  if (!report || !report.date || !report.dateText) throw new Error('缺少明确的数据日期。');
  const values = [
    report.company.dailyTotal,
    report.company.dailyDelivery,
    report.company.dailyGroup,
    report.company.monthTotal,
    report.company.monthDelivery,
    report.company.monthGroup,
  ];
  if (values.some((value) => !Number.isFinite(value))) {
    throw new Error('公司利润字段存在空值或非数字，已停止发送。');
  }
  if (report.regions.length === 0) throw new Error('未读取到区域利润数据。');
}

function buildMessageText(report) {
  const c = report.company;
  return [
    `📊 **每日利润播报｜${report.dateText}**`,
    '',
    '🔥 **当日利润**',
    `> 权责总利润：**${fmt(c.dailyTotal)} 万元**`,
    `> 外卖利润：${fmt(c.dailyDelivery)} 万元`,
    `> 团购利润：${fmt(c.dailyGroup)} 万元`,
    '',
    '📅 **本月累计**',
    `> 累计权责总利润：**${fmt(c.monthTotal)} 万元**`,
    `> 累计外卖利润：${fmt(c.monthDelivery)} 万元`,
    `> 累计团购利润：${fmt(c.monthGroup)} 万元`,
    '',
    '🎯 **本月预计**',
    `> 本月预计累计权责总利润：**${fmt(c.forecastTotal)} 万元**`,
    `> 累计预计权责外卖利润：${fmt(c.forecastDelivery)} 万元`,
    `> 累计预计团购利润：${fmt(c.forecastGroup)} 万元`,
  ].join('\n');
}

function renderProfitImage(report) {
  const props = PropertiesService.getScriptProperties();
  const templateId = props.getProperty('TEMPLATE_PRESENTATION_ID');
  if (!templateId) throw new Error('尚未配置 Google Slides 模板 ID。');

  const copy = DriveApp.getFileById(templateId).makeCopy(`每日利润播报-${report.dateText}`);
  try {
    const presentation = SlidesApp.openById(copy.getId());
    const slide = presentation.getSlides()[0];
    const c = report.company;

    replaceMarker(slide, '{{DATE}}', report.dateText, '#202020');
    replaceMarker(slide, '{{MONTH_TOTAL}} 万元', `${fmt(c.monthTotal)} 万元`, '#202020');
    replaceMarker(slide, '{{MONTH_DELIVERY}} 万元', `${fmt(c.monthDelivery)} 万元`, signedColor(c.monthDelivery));
    replaceMarker(slide, '{{MONTH_GROUP}} 万元', `${fmt(c.monthGroup)} 万元`, signedColor(c.monthGroup));
    replaceMarker(slide, '{{DAILY_TOTAL}}', fmt(c.dailyTotal), signedColor(c.dailyTotal, '#202020'));
    replaceMarker(slide, '{{DAILY_DELIVERY}}', fmt(c.dailyDelivery), signedColor(c.dailyDelivery));
    replaceMarker(slide, '{{DAILY_GROUP}}', fmt(c.dailyGroup), signedColor(c.dailyGroup));

    for (let i = 0; i < 9; i += 1) {
      const rank = i + 1;
      const row = report.regions[i] || { name: '-', total: 0, delivery: 0, group: 0 };
      replaceMarker(slide, `{{R${rank}_NAME}}`, row.name, '#202020');
      replaceMarker(slide, `{{R${rank}_TOTAL}}`, fmt(row.total), signedColor(row.total, '#202020'));
      replaceMarker(slide, `{{R${rank}_DELIVERY}}`, fmt(row.delivery), signedColor(row.delivery));
      replaceMarker(slide, `{{R${rank}_GROUP}}`, fmt(row.group), signedColor(row.group));
    }

    presentation.saveAndClose();
    Utilities.sleep(1500);

    const slideId = SlidesApp.openById(copy.getId()).getSlides()[0].getObjectId();
    const exportUrl =
      `https://docs.google.com/presentation/d/${copy.getId()}/export/png?pageid=${encodeURIComponent(slideId)}`;
    const response = UrlFetchApp.fetch(exportUrl, {
      headers: { Authorization: `Bearer ${ScriptApp.getOAuthToken()}` },
      muteHttpExceptions: true,
    });

    if (response.getResponseCode() < 200 || response.getResponseCode() >= 300) {
      throw new Error(`图片导出失败：HTTP ${response.getResponseCode()}`);
    }
    return response.getBlob().setName(`每日利润播报-${report.dateText}.png`);
  } finally {
    copy.setTrashed(true);
  }
}

function replaceMarker(slide, marker, value, color) {
  let replaced = false;
  slide.getPageElements().forEach((element) => {
    if (element.getPageElementType() !== SlidesApp.PageElementType.SHAPE) return;
    const shape = element.asShape();
    const text = shape.getText();
    const current = text.asString();
    if (current.indexOf(marker) === -1) return;
    text.replaceAllText(marker, String(value));
    text.getTextStyle().setForegroundColor(color || '#202020');
    replaced = true;
  });
  if (!replaced) throw new Error(`模板中缺少占位符：${marker}`);
}

function sendWeComMarkdown(content) {
  sendWeCom({
    msgtype: 'markdown',
    markdown: { content },
  });
}

function sendWeComImage(blob) {
  const bytes = blob.getBytes();
  sendWeCom({
    msgtype: 'image',
    image: {
      base64: Utilities.base64Encode(bytes),
      md5: Utilities.computeDigest(Utilities.DigestAlgorithm.MD5, bytes)
        .map((byte) => (byte + 256).toString(16).slice(-2))
        .join(''),
    },
  });
}

function sendWeCom(payload) {
  const webhook = PropertiesService.getScriptProperties().getProperty('WECOM_WEBHOOK');
  if (!webhook) throw new Error('尚未配置企业微信 Webhook。');

  const response = UrlFetchApp.fetch(webhook, {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });
  const result = JSON.parse(response.getContentText() || '{}');
  if (response.getResponseCode() !== 200 || Number(result.errcode) !== 0) {
    throw new Error(`企业微信发送失败：${result.errmsg || response.getResponseCode()}`);
  }
}

function markRow(sheet, rowNumber, status, errorMessage) {
  sheet.getRange(rowNumber, 4, 1, 3).setValues([[
    status,
    new Date(),
    String(errorMessage || '').slice(0, 500),
  ]]);
}

function parseDate(value) {
  if (value instanceof Date && !Number.isNaN(value.getTime())) return value;
  const text = String(value || '').trim().replace(/\//g, '-');
  const date = new Date(text);
  if (Number.isNaN(date.getTime())) throw new Error(`无法解析数据日期：${value}`);
  return date;
}

function toNumber(value) {
  if (typeof value === 'number') return value;
  const text = String(value ?? '').replace(/,/g, '').trim();
  if (!text) return NaN;
  const number = Number(text);
  return number;
}

function numberOrFallback(value, fallback) {
  const number = toNumber(value);
  return Number.isFinite(number) ? number : fallback;
}

function fmt(value) {
  return Number(value).toFixed(2);
}

function signedColor(value, positiveColor) {
  if (Number(value) < 0) return '#D94B58';
  return positiveColor || '#34A853';
}
