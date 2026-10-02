"use strict";

// ---------- helpers ----------------------------------------------------------

const $ = (sel) => document.querySelector(sel);
const SVG_NS = "http://www.w3.org/2000/svg";
const RU_MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
const RU_MONTHS_NOM = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"];
const RU_WEEKDAYS = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"];
const SERIES = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s7"];
const REVIEW_LABELS = {
  p2p: "перевод человеку — что это?",
  p2p_in: "входящий перевод — что это?",
  uncategorized: "нет категории",
  awaiting_pair: "нет пары в другом счёте",
};

const state = { meta: null, start: null, end: null, preset: "month", txOffset: 0, monthly: null };

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined) node.append(c);
  return node;
}

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

const num = (v) => Number(v || 0);
function fmtRM(v) {
  return "RM " + num(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtRUB(v) {
  return "₽ " + Math.round(num(v)).toLocaleString("ru-RU");
}
function fmtCoin(v, cur) {
  return `${cur} ` + num(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 8 });
}
function fmtMoney(v, cur, signed = false) {
  const n = num(v);
  const body = cur === "RUB" ? fmtRUB(Math.abs(n)) : cur && cur !== "MYR" ? fmtCoin(Math.abs(n), cur) : fmtRM(Math.abs(n));
  if (n < 0) return "−" + body;
  return (signed && n > 0 ? "+" : "") + body;
}
function fmtAxis(v) {
  if (v >= 1000) return (v / 1000).toLocaleString("en-US", { maximumFractionDigits: v >= 10000 ? 0 : 1 }) + "K";
  return String(Math.round(v));
}
function isoDate(d) {
  return d.toISOString().slice(0, 10);
}
function parseISO(s) {
  const [y, m, d] = s.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}
function fmtDay(s) {
  const d = parseISO(s);
  return `${d.getUTCDate()} ${RU_MONTHS[d.getUTCMonth()]}`;
}
function addDays(s, n) {
  const d = parseISO(s);
  d.setUTCDate(d.getUTCDate() + n);
  return isoDate(d);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    credentials: "same-origin",
  });
  if (res.status === 401) {
    location.reload();
    throw new Error("unauthorized");
  }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

// ---------- tooltip ----------------------------------------------------------

const tooltip = $("#tooltip");

function showTooltip(title, rows, x, y) {
  tooltip.textContent = "";
  tooltip.append(el("div", { class: "t-title", text: title }));
  for (const r of rows) {
    const key = el("span", { class: "key-line" });
    key.style.background = r.color ? `var(${r.color})` : "transparent";
    tooltip.append(el("div", { class: "t-row" }, key, el("b", { text: r.value }), el("span", { text: r.name })));
  }
  tooltip.hidden = false;
  const w = tooltip.offsetWidth;
  const left = Math.min(x + 14, window.scrollX + document.documentElement.clientWidth - w - 8);
  tooltip.style.left = `${Math.max(8, left)}px`;
  const h = tooltip.offsetHeight;
  const below = y + 14 + h <= window.scrollY + window.innerHeight - 8;
  tooltip.style.top = `${below ? y + 14 : Math.max(window.scrollY + 8, y - h - 14)}px`;
}
function hideTooltip() {
  tooltip.hidden = true;
}

// ---------- charts -----------------------------------------------------------

function niceScale(maxV) {
  const raw = maxV / 4;
  const pow = 10 ** Math.floor(Math.log10(raw || 1));
  const step = [1, 2, 2.5, 5, 10].map((s) => s * pow).find((s) => s >= raw) || pow * 10;
  const max = Math.ceil(maxV / step) * step || step;
  const ticks = [];
  for (let t = 0; t <= max + 1e-9; t += step) ticks.push(t);
  return { max, ticks };
}

function topRoundedRect(x, y, w, h, r) {
  r = Math.min(r, h, w / 2);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

// Charts are drawn in pixels of their container, so redraw when it changes width.
const chartObserver = new ResizeObserver((entries) => {
  for (const entry of entries) {
    const root = entry.target;
    const width = Math.round(entry.contentRect.width);
    if (root._cfg && width > 0 && width !== root._width) columnChart(root, root._cfg);
  }
});

/** Stacked columns (+ optional line) on one MYR axis, with a per-column hover readout. */
function columnChart(root, cfg) {
  root._cfg = cfg;
  root._width = Math.round(root.clientWidth);
  chartObserver.observe(root);
  root.textContent = "";
  const W = Math.max(root.clientWidth, 280);
  const H = cfg.height || 240;
  const m = { l: 44, r: 8, t: 10, b: 24 };
  const pw = W - m.l - m.r;
  const ph = H - m.t - m.b;
  const n = cfg.labels.length;
  const totals = cfg.labels.map((_, i) => cfg.series.reduce((s, se) => s + Math.max(0, num(se.values[i])), 0));
  const maxV = Math.max(1, ...totals, ...(cfg.line ? cfg.line.values.map(num) : []));
  const { max, ticks } = niceScale(maxV);
  const y = (v) => m.t + ph - (v / max) * ph;
  const band = pw / n;
  const bw = Math.max(2, Math.min(24, band * 0.62));

  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": cfg.ariaLabel || "" });
  for (const t of ticks) {
    svg.append(svgEl("line", { x1: m.l, x2: W - m.r, y1: y(t), y2: y(t), stroke: t === 0 ? "var(--axis)" : "var(--grid)", "stroke-width": 1 }));
    const label = svgEl("text", { x: m.l - 8, y: y(t) + 4, "text-anchor": "end", class: "axis-label" });
    label.textContent = fmtAxis(t);
    svg.append(label);
  }

  const bands = [];
  for (let i = 0; i < n; i++) {
    const rect = svgEl("rect", { x: m.l + i * band, y: m.t, width: band, height: ph, fill: "transparent", class: "band", tabindex: 0 });
    rect.setAttribute("aria-label", `${cfg.fullLabels[i]}: ${cfg.fmtVal(totals[i])}`);
    const show = (evt) => {
      bands.forEach((b) => b.classList.remove("hover"));
      rect.classList.add("hover");
      const rows = [];
      if (cfg.line && cfg.line.values[i] !== null) rows.push({ color: cfg.line.color, value: cfg.fmtVal(cfg.line.values[i]), name: cfg.line.name });
      for (const se of [...cfg.series].reverse()) {
        if (num(se.values[i]) > 0) rows.push({ color: se.color, value: cfg.fmtVal(se.values[i]), name: se.name });
      }
      if (cfg.series.length > 1) rows.push({ color: null, value: cfg.fmtVal(totals[i]), name: "всего расходов" });
      const box = rect.getBoundingClientRect();
      const px = evt && evt.pageX !== undefined ? evt.pageX : box.left + window.scrollX + box.width / 2;
      const py = evt && evt.pageY !== undefined ? evt.pageY : box.top + window.scrollY + 20;
      showTooltip(cfg.fullLabels[i], rows, px, py);
    };
    const hide = () => {
      rect.classList.remove("hover");
      hideTooltip();
    };
    rect.addEventListener("pointermove", show);
    rect.addEventListener("pointerleave", hide);
    rect.addEventListener("focus", () => show(null));
    rect.addEventListener("blur", hide);
    bands.push(rect);
    svg.append(rect);
  }

  for (let i = 0; i < n; i++) {
    const x = m.l + i * band + (band - bw) / 2;
    const segs = cfg.series.filter((se) => num(se.values[i]) > 0);
    let yCur = y(0);
    segs.forEach((se, k) => {
      const h = (num(se.values[i]) / max) * ph;
      const top = yCur - h;
      const bottom = k > 0 ? yCur - 2 : yCur; // 2px surface gap between stacked segments
      const height = bottom - top;
      yCur = top;
      if (height < 0.5) return;
      const attrs = { fill: `var(${se.color})`, "pointer-events": "none" };
      if (k === segs.length - 1) svg.append(svgEl("path", { ...attrs, d: topRoundedRect(x, top, bw, height, 4) }));
      else svg.append(svgEl("rect", { ...attrs, x, y: top, width: bw, height }));
    });
  }

  if (cfg.line) {
    const pts = cfg.line.values.flatMap((v, i) => (v === null ? [] : [[m.l + i * band + band / 2, y(num(v))]]));
    svg.append(svgEl("polyline", {
      points: pts.map((p) => p.join(",")).join(" "), fill: "none", stroke: `var(${cfg.line.color})`,
      "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round", "pointer-events": "none",
    }));
    for (const [cx, cy] of pts) {
      svg.append(svgEl("circle", { cx, cy, r: 4, fill: `var(${cfg.line.color})`, stroke: "var(--surface)", "stroke-width": 2, "pointer-events": "none" }));
    }
  }

  const step = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(pw / 46))));
  for (let i = 0; i < n; i += step) {
    const label = svgEl("text", { x: m.l + i * band + band / 2, y: H - 6, "text-anchor": "middle", class: "axis-label" });
    label.textContent = cfg.labels[i];
    svg.append(label);
  }
  root.append(svg);
}

function renderCrypto(s) {
  const coins = Object.entries(s.blocks).filter(([cur, b]) => cur !== "MYR" && cur !== "RUB" && num(b.expense) > 0);
  const got = Object.entries(s.crypto.got);
  $("#crypto-card").hidden = !coins.length && !got.length;
  const blocks = $("#crypto-blocks");
  blocks.textContent = "";
  for (const [cur, b] of coins) {
    const bars = el("div", { class: "bars" });
    blocks.append(el("div", { class: "kv" }, el("span", { text: `Расходы в ${cur}` }), el("span", { class: "v", text: fmtMoney(b.expense, cur) })), bars);
    barList(bars, b.categories, cur, false);
  }
  const info = $("#crypto-info");
  info.textContent = "";
  if (got.length) {
    const paid = Object.entries(s.crypto.paid).map(([cur, v]) => fmtMoney(v, cur)).join(" + ");
    info.append(el("span", { text: `Куплено за ${paid}` }), el("span", { class: "v", text: got.map(([cur, v]) => fmtMoney(v, cur)).join(" + ") }));
  }
}

function barList(root, items, cur, withBaseline) {
  root.textContent = "";
  if (!items.length) {
    root.append(el("div", { class: "empty", text: "Нет расходов за период" }));
    return;
  }
  const max = Math.max(...items.map((it) => Math.max(num(it.amount), withBaseline ? num(it.baseline) : 0)), 1);
  const scale = (v) => (num(v) / max) * 100;
  for (const it of items) {
    const fill = el("div", { class: "bar-fill" });
    fill.style.width = `${scale(it.amount)}%`;
    const track = el("div", { class: "bar-track" }, fill);
    if (withBaseline && num(it.baseline) > 0) {
      const tick = el("span", { class: "bar-base", title: `в среднем ${fmtMoney(it.baseline, cur)}` });
      tick.style.left = `calc(${scale(it.baseline)}% - 1px)`;
      track.append(tick);
    }
    root.append(el("div", { class: "bar-row" },
      el("span", { class: "name", text: it.label, title: it.label }),
      track,
      el("span", { class: "bar-value", text: fmtMoney(it.amount, cur) }),
    ));
  }
}

function dataTable(root, headers, rows) {
  root.textContent = "";
  const table = el("table", { class: "data" });
  table.append(el("thead", {}, el("tr", {}, headers.map((h, i) => el("th", { class: i ? "num" : null, text: h })))));
  const body = el("tbody");
  for (const r of rows) body.append(el("tr", {}, r.map((c, i) => el("td", { class: i ? "num" : null, text: c }))));
  table.append(body);
  root.append(table);
}

// ---------- sections ---------------------------------------------------------

function categoryLabel(code) {
  const c = state.meta.categories.find((x) => x.code === code);
  return c ? c.label : code;
}

function renderMonthly() {
  const data = state.monthly;
  if (!data) return;
  const totals = {};
  for (const mth of data) for (const [code, v] of Object.entries(mth.categories)) totals[code] = (totals[code] || 0) + num(v);
  const top = Object.entries(totals).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]).slice(0, 7).map(([c]) => c);
  const series = top.map((code, i) => ({ name: categoryLabel(code), color: SERIES[i], values: data.map((mth) => num(mth.categories[code])) }));
  const rest = data.map((mth) => Object.entries(mth.categories).filter(([c]) => !top.includes(c)).reduce((s, [, v]) => s + num(v), 0));
  if (rest.some((v) => v > 0)) series.push({ name: "Остальное", color: "--rest", values: rest });
  // a financial month is labelled by the month it ends in (26 Aug – 25 Sep -> "Сен")
  const labels = data.map((mth) => RU_MONTHS_NOM[Number(mth.month.slice(5)) - 1]);
  const fullLabels = data.map((mth) => state.meta.month_start_day > 1
    ? `${fmtDay(mth.start)} – ${fmtDay(mth.end)}`
    : `${RU_MONTHS_NOM[Number(mth.month.slice(5)) - 1]} ${mth.month.slice(0, 4)}`);
  // the month in progress has no salary yet: leave its point out instead of dropping to zero
  const last = data.length - 1;
  const line = { name: "Доход", color: "--ink-2", values: data.map((mth, i) => (i === last && !num(mth.income) ? null : num(mth.income))) };

  const legend = $("#monthly-legend");
  legend.textContent = "";
  for (const se of series) {
    const key = el("span", { class: "key-rect" });
    key.style.background = `var(${se.color})`;
    legend.append(el("li", {}, key, se.name));
  }
  const lineKey = el("span", { class: "key-line" });
  lineKey.style.background = "var(--ink-2)";
  legend.append(el("li", {}, lineKey, "Доход"));

  columnChart($("#monthly-chart"), {
    labels, fullLabels, series, line, height: 260, fmtVal: fmtRM, ariaLabel: "Расходы по месяцам по категориям",
  });
  dataTable(
    $("#monthly-table"),
    ["Месяц", ...series.map((s) => s.name), "Всего", "Доход"],
    data.map((mth, i) => [
      fullLabels[i], ...series.map((s) => fmtRM(s.values[i])),
      fmtRM(series.reduce((acc, s) => acc + s.values[i], 0)), fmtRM(mth.income),
    ]),
  );
}

function renderDaily(daily) {
  const labels = daily.map((d) => String(parseISO(d.day).getUTCDate()));
  const fullLabels = daily.map((d) => `${fmtDay(d.day)}, ${RU_WEEKDAYS[parseISO(d.day).getUTCDay()]}`);
  columnChart($("#daily-chart"), {
    labels, fullLabels, height: 220, fmtVal: fmtRM, ariaLabel: "Расходы по дням",
    series: [{ name: "Расходы", color: "--s1", values: daily.map((d) => num(d.amount)) }],
  });
  dataTable($("#daily-table"), ["День", "Расходы"], daily.map((d, i) => [fullLabels[i], fmtRM(d.amount)]));
}

function setDelta(node, current, baseline, periodWord) {
  node.textContent = "";
  if (baseline === null || baseline === undefined || num(baseline) <= 0) return;
  const change = Math.round(((num(current) - num(baseline)) / num(baseline)) * 100);
  const up = change > 0;
  node.append(
    el("span", { class: `delta ${Math.abs(change) >= 5 ? (up ? "up" : "down") : ""}`, text: `${up ? "↑" : change < 0 ? "↓" : "="} ${Math.abs(change)}%` }),
    ` к среднему (${fmtRM(baseline)}) ${periodWord}`,
  );
}

function renderSummary(s) {
  const myr = s.blocks.MYR || { expense: 0, income: 0, categories: [], merchants: [] };
  const rub = s.blocks.RUB || { expense: 0, income: 0, categories: [] };
  $("#k-expense").textContent = fmtRM(myr.expense);
  setDelta($("#k-expense-sub"), myr.expense, myr.baseline_expense, "за 3 таких же периода");
  $("#k-income").textContent = fmtRM(myr.income);
  $("#k-income-sub").textContent = rub.income > 0 ? `+ ${fmtRUB(rub.income)} в рублях` : "";
  const net = num(myr.income) - num(myr.expense) - num(s.fx.myr) + num(s.fx_back.myr);
  $("#k-net").textContent = fmtMoney(net, "MYR", true);
  $("#k-fx").textContent = fmtRM(s.fx.myr);
  $("#k-fx-sub").textContent = s.fx.count ? `→ ${fmtRUB(s.fx.rub)} · курс ${num(s.fx.rate).toFixed(2)}` : "переводов не было";
  $("#k-rub").textContent = fmtRUB(rub.expense);
  $("#k-rub-sub").textContent = rub.categories.length ? `больше всего: ${rub.categories[0].label}` : "";

  barList($("#cat-bars"), myr.categories, "MYR", true);
  barList($("#rub-bars"), rub.categories, "RUB", false);

  const merchants = $("#merchants");
  merchants.textContent = "";
  if (!myr.merchants.length) merchants.append(el("div", { class: "empty", text: "Нет расходов за период" }));
  else dataTable(merchants, ["Где", "Раз", "Сумма"], myr.merchants.map((mm) => [mm.merchant, String(mm.count), fmtRM(mm.amount)]));

  const fx = $("#fx-info");
  fx.textContent = "";
  if (s.fx.count) {
    fx.append(el("span", { text: "Пришло за период" }), el("span", { class: "v", text: fmtRUB(s.fx.rub) }));
    fx.append(el("span", { text: "Курс периода" }), el("span", { class: "v", text: `${num(s.fx.rate).toFixed(2)} ₽/RM` }));
  }
  if (num(s.fx_back.myr)) {
    fx.append(el("span", { text: "Вернули за рубли" }), el("span", { class: "v", text: `${fmtRUB(s.fx_back.rub)} → ${fmtRM(s.fx_back.myr)}` }));
  }
  if (state.meta.fx_rate) {
    fx.append(el("span", { text: "Курс за 90 дней" }), el("span", { class: "v", text: `${num(state.meta.fx_rate).toFixed(2)} ₽/RM` }));
  }

  renderCrypto(s);

  const balances = $("#balances");
  balances.textContent = "";
  for (const b of s.balances) {
    balances.append(
      el("span", {}, b.account, b.as_of ? el("div", { class: "m", text: `по данным на ${fmtDay(b.as_of)}` }) : el("div", { class: "m", text: "по записям" })),
      el("span", { class: "v", text: fmtMoney(b.amount, b.currency) }),
    );
  }
  if (!s.balances.length) balances.append(el("div", { class: "empty", text: "Пока нет данных" }));

  const fresh = $("#freshness");
  fresh.textContent = "";
  for (const f of s.freshness) {
    const stale = f.days_old > 7;
    fresh.append(
      el("span", {}, f.account),
      el("span", { class: `v ${stale ? "warn" : ""}`, text: `${fmtDay(f.last_day)} · ${f.days_old} дн.${stale ? " — пришли выписку" : ""}` }),
    );
  }
  if (!s.freshness.length) fresh.append(el("div", { class: "empty", text: "Выписок ещё не было — пришли файл боту" }));

  const badge = $("#review-badge");
  badge.hidden = !s.review_count;
  badge.className = "badge alert";
  badge.textContent = `Разобрать: ${s.review_count}`;
}

// ---------- review -----------------------------------------------------------

function categorySelect(kind, onPick, placeholder) {
  const select = el("select", { "aria-label": placeholder }, el("option", { value: "", text: placeholder }));
  for (const c of state.meta.categories.filter((x) => x.kind === kind)) select.append(el("option", { value: c.code, text: c.label }));
  select.addEventListener("change", () => select.value && onPick(select.value));
  return select;
}

async function act(promise) {
  try {
    await promise;
  } catch (err) {
    alert(`Не получилось: ${err.message}`);
  }
  await refresh();
}

function reviewItem(t, group = [t]) {
  const actions = el("div", { class: "actions" });
  const patch = (body) => act(api(`/api/transactions/${t.id}`, { method: "PATCH", body: JSON.stringify(body) }));
  const outgoing = num(t.amount) < 0;

  if (t.review === "p2p" || (t.review === "awaiting_pair" && outgoing)) {
    const rubInput = el("input", { type: "number", min: "1", step: "1", placeholder: "₽ пришло", "aria-label": "Сколько рублей пришло", hidden: true });
    const ok = el("button", { class: "btn primary", text: "OK", hidden: true });
    ok.addEventListener("click", () => {
      if (num(rubInput.value) > 0) act(api(`/api/transactions/${t.id}/fx`, { method: "POST", body: JSON.stringify({ rub_amount: rubInput.value }) }));
    });
    const rf = el("button", { class: "btn", text: "🇷🇺 На РФ-счёт" });
    rf.addEventListener("click", () => {
      rubInput.hidden = false;
      ok.hidden = false;
      rf.hidden = true;
      rubInput.focus();
    });
    actions.append(rf, rubInput, ok);
    actions.append(el("button", { class: "btn", text: "↔️ Мои счета", onclick: () => patch({ action: "own" }) }));
    // e.g. rent paid by DuitNow: remember it and next month's payment is categorized on import
    const remember = el("input", { type: "checkbox", id: `rem-${t.id}` });
    actions.append(categorySelect("expense", (code) => patch({ category: code, remember: remember.checked }), "Это расход…"));
    actions.append(el("label", { for: `rem-${t.id}`, class: "meta" }, remember, " запомнить"));
  } else if (t.review === "p2p_in" || t.review === "awaiting_pair") {
    actions.append(el("button", { class: "btn", text: "➕ Доход", onclick: () => patch({ action: "keep" }) }));
    actions.append(el("button", { class: "btn", text: "↔️ Мои счета", onclick: () => patch({ action: "own" }) }));
    actions.append(categorySelect("expense", (code) => patch({ category: code }), "Вернули за…"));
  } else {
    // Unknown merchants are remembered by default, as in the bot: next time they categorize themselves.
    const remember = el("input", { type: "checkbox", id: `rem-${t.id}`, checked: true });
    actions.append(categorySelect(outgoing ? "expense" : "income", (code) => patch({ category: code, remember: remember.checked }), "Категория…"));
    actions.append(el("label", { for: `rem-${t.id}`, class: "meta" }, remember, " запомнить"));
    actions.append(el("button", { class: "btn", text: "Оставить", onclick: () => patch({ action: "keep" }) }));
  }

  const total = group.reduce((acc, x) => acc + num(x.amount), 0);
  const headline = group.length > 1
    ? [el("b", { text: `${group.length}×` }), ` ${t.merchant} · всего ${fmtMoney(total, t.currency)}`]
    : [el("b", { class: outgoing ? "" : "amount-in", text: fmtMoney(t.amount, t.currency, true) }), " · ", t.description];
  const when = group.length > 1 ? `${fmtDay(group[group.length - 1].date)} – ${fmtDay(t.date)}` : fmtDay(t.date);
  const info = el("div", {},
    el("div", {}, ...headline),
    el("div", { class: "meta", text: `${t.account_name} · ${when} · ${REVIEW_LABELS[t.review] || ""}` }),
  );
  return el("div", { class: "review-item" }, info, actions);
}

const REVIEW_SHOWN = 12;

async function loadReview(showAll = false) {
  const data = await api("/api/transactions?review=true&limit=500");
  const groups = new Map();
  for (const t of data.items) {
    // Only uncategorized rows group: transfers to people each need their own answer.
    const key = t.review === "uncategorized" ? `${t.account}|${t.merchant}` : `id:${t.id}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(t);
  }
  const list = $("#review-list");
  list.textContent = "";
  if (!data.items.length) list.append(el("div", { class: "empty", text: "🎉 Всё разобрано" }));
  const all = [...groups.values()];
  for (const group of showAll ? all : all.slice(0, REVIEW_SHOWN)) list.append(reviewItem(group[0], group));
  if (!showAll && all.length > REVIEW_SHOWN) {
    list.append(el("button", { class: "btn more", text: `Показать все (${all.length})`, onclick: () => loadReview(true) }));
  }
}

// ---------- budget -------------------------------------------------------------

const PLAN_KINDS = { transfer: "перевод", income: "доход" };

function planWhen(p) {
  const when = fmtDay(p.due_on);
  if (p.repeat_months === 1) return `каждый месяц с ${when}`;
  if (p.repeat_months === 12) return `каждый год с ${when}`;
  if (p.repeat_months) return `раз в ${p.repeat_months} мес. с ${when}`;
  return when;
}

async function loadBudget() {
  const [b, plans] = await Promise.all([api("/api/budget"), api("/api/plans")]);
  const day = state.meta.month_start_day;
  $("#budget-hint").textContent = day > 1 ? `месяц — с ${day}-го по ${day - 1}-е, по зарплате` : "по календарным месяцам";

  const money = $("#budget-money");
  money.textContent = "";
  money.append(el("span", {}, "Можно тратить", el("div", { class: "m", text: b.liquid.map((x) => x.account).join(", ") })),
    el("span", { class: "v", text: fmtRM(b.liquid_total) }));
  for (const p of b.reserve_pots) money.append(el("span", {}, p.pot, el("div", { class: "m", text: "резерв" })), el("span", { class: "v", text: fmtRM(p.amount) }));
  for (const p of b.protected_pots) money.append(el("span", {}, p.pot, el("div", { class: "m", text: "цель, не трогаем" })), el("span", { class: "v", text: fmtRM(p.amount) }));

  const root = $("#budget-table");
  root.textContent = "";
  const first = b.cycles[0];
  if (first) {
    const parts = [`${fmtRM(first.opening)} на счетах`];
    if (num(first.salary)) parts.push(`+ ${fmtRM(first.salary)} зарплата ${fmtDay(first.salary_day)}`);
    const spent = num(first.spent_so_far) ? ` (${fmtRM(first.usual_spending_month)} за месяц − уже ${fmtRM(first.spent_so_far)})` : "";
    parts.push(`− ${fmtRM(first.usual_spending_left)} обычные траты${spent}`);
    if (first.planned.length) parts.push(`${num(first.planned_total) < 0 ? "−" : "+"} ${fmtRM(Math.abs(num(first.planned_total)))} план (${first.planned.map((p) => p.title).join(", ")})`);
    const formula = [el("b", { text: `${first.label}: ` }), parts.join(" "), " = ", el("b", { text: fmtRM(first.closing) }), ` к ${fmtDay(first.end)}.`];
    if (num(first.salary)) {
      const planBefore = num(first.planned_before_salary) ? ` ${num(first.planned_before_salary) < 0 ? "−" : "+"} ${fmtRM(Math.abs(num(first.planned_before_salary)))} плана` : "";
      formula.push(` Дно утром ${fmtDay(first.salary_day)}: ${fmtRM(first.opening)} − ${fmtRM(first.usual_spending_before_salary)} трат до зарплаты${planBefore} = `,
        el("b", { class: num(first.low_before_salary) < 0 ? "warn" : "", text: fmtRM(first.low_before_salary) }), ".");
    }
    root.append(el("p", { class: "formula" }, ...formula));
  }
  const table = el("table", { class: "data" });
  const heads = ["Месяц", "Старт", "+ Зарплата", "− Обычные траты", "± План", "= Конец месяца", "Дно перед зарплатой"];
  table.append(el("thead", {}, el("tr", {}, heads.map((h, i) => el("th", { class: i ? "num" : null, text: h })))));
  const body = el("tbody");
  for (const c of b.cycles) {
    const planned = c.planned.length ? fmtMoney(c.planned_total, "MYR", true) : "—";
    body.append(el("tr", {},
      el("td", {}, c.label, el("div", { class: "meta", text: `${fmtDay(c.start)} – ${fmtDay(c.end)}` })),
      el("td", { class: "num", text: fmtRM(c.opening) }),
      el("td", { class: "num", text: num(c.salary) ? fmtMoney(c.salary, "MYR", true) : "—" }),
      el("td", { class: "num", text: fmtMoney(-num(c.usual_spending_left), "MYR"), title: `за месяц ${fmtRM(c.usual_spending_month)}` }),
      el("td", { class: "num", text: planned, title: c.planned.map((p) => `${p.title}: ${fmtMoney(p.amount, "MYR", true)}`).join("\n") }),
      el("td", { class: `num ${num(c.closing) < 0 ? "warn" : ""}`, text: fmtMoney(c.closing, "MYR") }),
      el("td", { class: `num ${num(c.low_before_salary) < 0 ? "warn" : ""}`, text: fmtMoney(c.low_before_salary, "MYR"),
        title: `${fmtRM(c.opening)} − ${fmtRM(c.usual_spending_before_salary)} трат до зарплаты` }),
    ));
  }
  table.append(body);
  root.append(table);
  root.append(el("div", { class: "meta", text: "Конец = старт + зарплата − обычные траты ± план; конец месяца — старт следующего. Дно — сколько останется утром перед зарплатой." }));

  const notes = [];
  if (num(b.usual_spending_total)) notes.push(`обычные траты ${fmtRM(b.usual_spending_total)} в месяц — медиана за прошлые месяцы`);
  if (num(b.rf_transfers_per_cycle_not_in_forecast)) notes.push(`переводы на РФ (обычно ${fmtRM(b.rf_transfers_per_cycle_not_in_forecast)}) не учтены, пока их нет в плане`);
  if (num(b.trips_per_cycle_not_in_forecast)) notes.push(`поездки только из плана`);
  $("#budget-notes").textContent = [...notes, ...b.notes].join(" · ");

  const list = $("#plan-list");
  list.textContent = "";
  if (!plans.length) list.append(el("div", { class: "empty", text: "Ничего не запланировано" }));
  for (const p of plans) {
    const setStatus = (status) => act(api(`/api/plans/${p.id}`, { method: "PATCH", body: JSON.stringify({ status }) }).then(loadBudget));
    list.append(el("div", { class: "plan-item" },
      el("div", {},
        el("b", { class: num(p.amount) > 0 ? "amount-in" : "", text: fmtMoney(p.amount, "MYR", true) }), ` · ${p.title}`,
        el("div", { class: "meta", text: [planWhen(p), PLAN_KINDS[p.kind], p.category_label].filter(Boolean).join(" · ") })),
      el("div", { class: "actions" },
        el("button", { class: "btn", text: "✅ Оплачено", onclick: () => setStatus("done") }),
        el("button", { class: "btn", text: "✖️", title: "Не будет", onclick: () => setStatus("cancelled") })),
    ));
  }
}

function initPlanForm() {
  const form = $("#plan-form");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(form);
    const body = {
      title: f.get("title"), amount: f.get("amount"), due_on: f.get("due_on"), kind: f.get("kind"),
      repeat_months: f.get("monthly") ? 1 : null,
    };
    try {
      await api("/api/plans", { method: "POST", body: JSON.stringify(body) });
      form.reset();
      await loadBudget();
    } catch (err) {
      alert(`Не получилось: ${err.message}`);
    }
  });
}

// ---------- transactions -----------------------------------------------------

function txCategoryCell(t) {
  if (t.kind === "transfer") {
    if (t.transfer && t.transfer.kind === "fx") return el("span", { class: "pill", text: `🇷🇺 РФ · курс ${num(t.transfer.rate).toFixed(2)}` });
    if (t.transfer && t.transfer.kind === "crypto") return el("span", { class: "pill", text: "🪙 Крипто" });
    if (t.transfer && t.transfer.kind === "fx_back") return el("span", { class: "pill", text: "↩️ Возврат за ₽" });
    if (t.transfer) return el("span", { class: "pill", text: "↔️ Между счетами" });
    return el("span", { class: "pill", text: t.review === "p2p" ? "❔ Перевод" : "↔️ Перевод" });
  }
  const select = categorySelect(t.kind, async (code) => {
    await act(api(`/api/transactions/${t.id}`, { method: "PATCH", body: JSON.stringify({ category: code }) }));
  }, t.category_label || "—");
  select.value = "";
  select.options[0].textContent = t.category_label || "—";
  return select;
}

function txRow(t) {
  const meta = [t.account_name, t.status === "pending" ? "ждёт выписку" : null, t.note].filter(Boolean).join(" · ");
  return el("tr", {},
    el("td", { class: "meta", text: fmtDay(t.date) }),
    el("td", { class: "desc" }, el("div", { text: t.description }), el("div", { class: "meta", text: meta }),
      // on a phone the category column is hidden: the category goes under the description
      el("div", { class: "show-sm" }, txCategoryCell(t))),
    el("td", { class: "hide-sm" }, txCategoryCell(t)),
    el("td", { class: `num ${num(t.amount) > 0 ? "amount-in" : ""}`, text: fmtMoney(t.amount, t.currency, true) }),
  );
}

async function loadTx(reset = true) {
  if (reset) state.txOffset = 0;
  const params = new URLSearchParams({ limit: 100, offset: state.txOffset });
  for (const [key, id] of [["account", "#f-account"], ["category", "#f-category"], ["kind", "#f-kind"], ["q", "#f-q"]]) {
    const v = $(id).value.trim();
    if (v) params.set(key, v);
  }
  // a search looks through every month: an old row can be found and its category fixed
  const anytime = params.has("q");
  if (anytime) params.set("anytime", "true");
  else {
    params.set("start", state.start);
    params.set("end", state.end);
  }
  const data = await api(`/api/transactions?${params}`);
  const table = $("#tx-table");
  if (reset) {
    table.textContent = "";
    table.append(el("thead", {}, el("tr", {},
      el("th", { text: "Дата" }), el("th", { text: "Описание" }), el("th", { class: "hide-sm", text: "Категория" }), el("th", { class: "num", text: "Сумма" }))));
    table.append(el("tbody"));
  }
  const body = table.querySelector("tbody");
  for (const t of data.items) body.append(txRow(t));
  state.txOffset += data.items.length;
  $("#tx-count").textContent = `${data.total} шт.${anytime ? " за всё время" : ""}`;
  $("#tx-more").hidden = state.txOffset >= data.total;
}

// ---------- period & loading ------------------------------------------------

function applyPreset(preset) {
  const today = state.meta.today;
  if (preset === "month") [state.start, state.end] = [state.meta.cycle.start, today];
  else if (preset === "prev") [state.start, state.end] = [state.meta.prev_cycle.start, state.meta.prev_cycle.end];
  else if (preset === "30") [state.start, state.end] = [addDays(today, -29), today];
  else if (preset === "90") [state.start, state.end] = [addDays(today, -89), today];
  else if (preset === "year") [state.start, state.end] = [addDays(today, -364), today];
  state.preset = preset;
  for (const b of document.querySelectorAll("#presets button")) b.setAttribute("aria-pressed", String(b.dataset.preset === preset));
  $("#from").value = state.start;
  $("#to").value = state.end;
}

async function refresh() {
  const sections = [$("#main"), $("#kpis")];
  sections.forEach((s) => s.classList.add("loading"));
  try {
    const q = `start=${state.start}&end=${state.end}`;
    const [summary, daily] = await Promise.all([api(`/api/summary?${q}`), api(`/api/daily?${q}&currency=MYR`)]);
    $("#range-label").textContent = `${fmtDay(state.start)} – ${fmtDay(state.end)}`;
    renderSummary(summary);
    renderDaily(daily);
    await Promise.all([loadTx(true), loadReview()]);
  } finally {
    sections.forEach((s) => s.classList.remove("loading"));
  }
}

async function init() {
  state.meta = await api("/api/meta");
  const day = state.meta.month_start_day;
  if (day > 1) $("#monthly-hint").textContent = `месяц — с ${day}-го по ${day - 1}-е, по зарплате · линия — доход`;
  for (const a of state.meta.accounts) $("#f-account").append(el("option", { value: a.code, text: a.name }));
  for (const c of state.meta.categories) $("#f-category").append(el("option", { value: c.code, text: c.label }));

  applyPreset("month");
  for (const b of document.querySelectorAll("#presets button")) {
    b.addEventListener("click", () => {
      applyPreset(b.dataset.preset);
      refresh();
    });
  }
  const custom = () => {
    if (!$("#from").value || !$("#to").value) return;
    state.start = $("#from").value;
    state.end = $("#to").value;
    for (const b of document.querySelectorAll("#presets button")) b.setAttribute("aria-pressed", "false");
    refresh();
  };
  $("#from").addEventListener("change", custom);
  $("#to").addEventListener("change", custom);
  for (const id of ["#f-account", "#f-category", "#f-kind"]) $(id).addEventListener("change", () => loadTx(true));
  $("#f-q").addEventListener("input", debounce(() => loadTx(true), 300));
  $("#tx-more").addEventListener("click", () => loadTx(false));
  for (const btn of document.querySelectorAll("[data-toggle]")) {
    btn.addEventListener("click", () => {
      const name = btn.dataset.toggle;
      const table = $(`#${name}-table`);
      table.hidden = !table.hidden;
      $(`#${name}-chart`).hidden = !table.hidden;
      btn.textContent = table.hidden ? "Таблица" : "График";
    });
  }

  initPlanForm();
  state.monthly = await api("/api/monthly?currency=MYR&months=12");
  renderMonthly();
  await Promise.all([refresh(), loadBudget()]);
}

init();
