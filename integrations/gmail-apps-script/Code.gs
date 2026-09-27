/**
 * Finance autoimport from Gmail (Google Apps Script, runs in your own account).
 *
 * Every 30 minutes:
 *  - statements: mail from TNG / Maybank with PDF or CSV attachments is uploaded
 *    to POST /api/imports (parsed, deduplicated, reported to the Telegram bot);
 *  - receipts: purchase emails (Gmail's "Purchases" category, or subjects like
 *    receipt / order / invoice) go as text to POST /api/receipts; a daily Claude
 *    routine matches them to transactions and sets the category.
 * Processed threads get the labels `finance-imported` / `finance-receipt`.
 * No Gmail password leaves Google.
 *
 * Setup:
 *   1. script.google.com → New project → paste this file.
 *   2. Project Settings → Script properties:
 *        FINANCE_API_URL = https://<service>.up.railway.app
 *        API_TOKEN       = <the same value as in Railway>
 *      optional RECEIPT_QUERY to override the Gmail search for receipts.
 *   3. Run `install` once and allow Gmail access. It creates the 30-minute trigger
 *      and processes what is already in the mailbox.
 */

const SENDERS = ['tngdigital.com.my', 'touchngo.com.my', 'maybank2u.com.my', 'maybank.com'];
const LABEL = 'finance-imported';
const LOOKBACK = '60d';

const RECEIPT_LABEL = 'finance-receipt';
const RECEIPT_QUERY =
  '(category:purchases OR subject:(receipt OR resit OR invoice OR "order confirmation" OR "your order" OR "payment successful" OR "payment receipt"))';
const RECEIPT_LOOKBACK = '30d';
const MAX_BODY = 20000;

function install() {
  ScriptApp.getProjectTriggers()
    .filter((t) => ['run', 'importStatements'].includes(t.getHandlerFunction()))
    .forEach((t) => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('run').timeBased().everyMinutes(30).create();
  run();
}

function run() {
  importStatements();
  sendReceipts();
}

function config_() {
  const props = PropertiesService.getScriptProperties();
  const api = (props.getProperty('FINANCE_API_URL') || '').replace(/\/$/, '');
  const token = props.getProperty('API_TOKEN');
  if (!api || !token) throw new Error('Set FINANCE_API_URL and API_TOKEN in Script properties');
  return { api, token, receiptQuery: props.getProperty('RECEIPT_QUERY') || RECEIPT_QUERY };
}

function importStatements() {
  const { api, token } = config_();
  const label = GmailApp.getUserLabelByName(LABEL) || GmailApp.createLabel(LABEL);
  const query = `from:(${SENDERS.join(' OR ')}) has:attachment newer_than:${LOOKBACK} -label:${LABEL}`;

  for (const thread of GmailApp.search(query, 0, 50)) {
    let retryLater = false;
    for (const message of thread.getMessages()) {
      for (const attachment of message.getAttachments()) {
        const name = attachment.getName().toLowerCase();
        if (!name.endsWith('.pdf') && !name.endsWith('.csv')) continue;

        const response = UrlFetchApp.fetch(`${api}/api/imports`, {
          method: 'post',
          headers: { Authorization: `Bearer ${token}` },
          payload: { file: attachment.copyBlob(), origin: 'email' },
          muteHttpExceptions: true,
        });
        const code = response.getResponseCode();
        if (code === 200) {
          console.log(`${attachment.getName()}: ${response.getContentText()}`);
        } else if (code === 400) {
          // Not a statement or wrong PDF password: the bot already got the reason; do not resend.
          console.warn(`${attachment.getName()}: ${response.getContentText()}`);
        } else {
          // API down or deploying: leave the thread unlabeled, the next run retries.
          console.error(`${attachment.getName()}: HTTP ${code} ${response.getContentText()}`);
          retryLater = true;
        }
      }
    }
    if (!retryLater) thread.addLabel(label);
  }
}

function sendReceipts() {
  const { api, token, receiptQuery } = config_();
  const label = GmailApp.getUserLabelByName(RECEIPT_LABEL) || GmailApp.createLabel(RECEIPT_LABEL);
  const senders = SENDERS.map((s) => `-from:${s}`).join(' ');
  const query = `${receiptQuery} ${senders} newer_than:${RECEIPT_LOOKBACK} -label:${RECEIPT_LABEL} -label:${LABEL}`;

  for (const thread of GmailApp.search(query, 0, 50)) {
    let retryLater = false;
    for (const message of thread.getMessages()) {
      const response = UrlFetchApp.fetch(`${api}/api/receipts`, {
        method: 'post',
        contentType: 'application/json',
        headers: { Authorization: `Bearer ${token}` },
        payload: JSON.stringify({
          message_id: message.getId(),
          sender: message.getFrom(),
          subject: message.getSubject(),
          received_at: message.getDate().toISOString(),
          body: message.getPlainBody().slice(0, MAX_BODY),
        }),
        muteHttpExceptions: true,
      });
      if (response.getResponseCode() !== 200) {
        console.error(`${message.getSubject()}: HTTP ${response.getResponseCode()} ${response.getContentText()}`);
        retryLater = true;
      }
    }
    if (!retryLater) thread.addLabel(label);
  }
}
