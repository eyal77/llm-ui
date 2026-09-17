/* LLM Compare — frontend (no framework, no build step). */
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const esc = window.TextDiff.escape;
  const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString());
  const fmtMs = (ms) => (ms == null ? "—" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);
  const wordCount = (s) => (s || "").trim().split(/\s+/).filter(Boolean).length;

  const state = {
    view: "single",
    providers: [],
    defaults: {},
    compare: [],        // [{id, provider, label, model, status, result}]
    baseline: null,
    compareView: "side",
    running: false,
  };

  // ---------- small helpers ----------
  const store = {
    get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { v == null ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v); } catch { /* ignore */ } },
  };

  async function api(path, { method = "GET", body, auth = false } = {}) {
    const headers = { "Content-Type": "application/json" };
    if (auth) headers.Authorization = `Bearer ${store.get("adminToken") || ""}`;
    const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
    let data = null;
    try { data = await res.json(); } catch { /* empty body */ }
    if (!res.ok) {
      const detail = data && data.detail;
      const msg = Array.isArray(detail) ? detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ") : detail;
      const err = new Error(msg || `${res.status} ${res.statusText}`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  let toastTimer;
  function toast(msg, kind = "info") {
    const t = $("#toast");
    t.textContent = msg;
    t.className = `toast ${kind}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add("hidden"), 3500);
  }

  function copy(text) {
    navigator.clipboard?.writeText(text).then(() => toast("Copied"), () => toast("Copy failed", "error"));
  }

  function download(name, content, type) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([content], { type }));
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  // ---------- navigation ----------
  function setView(view) {
    state.view = view;
    $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
    $("#view-run").classList.toggle("hidden", view === "admin");
    $("#view-admin").classList.toggle("hidden", view !== "admin");
    $$(".mode-single").forEach((el) => el.classList.toggle("hidden", view !== "single"));
    $$(".mode-compare").forEach((el) => el.classList.toggle("hidden", view !== "compare"));
    $("#run-btn").textContent = view === "compare" ? "Run on selected models" : "Run";
    if (view === "admin") renderAdmin();
    else updateParamWarnings();
    if (location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
  }

  // ---------- models ----------
  const configured = () => state.providers.filter((p) => p.configured);
  const allModels = () => configured().flatMap((p) => p.models.map((m) => ({ ...m, provider: p.provider, label: p.label })));
  const providerOf = (id) => state.providers.find((p) => p.provider === id);

  // Small provider icon: the logo file from app/static/logos/ if present, else a lettered badge.
  const MONOGRAM = { bedrock: "B", nvidia: "N", gemini: "G", openai: "O", anthropic: "A" };
  function providerIcon(provider, logo) {
    logo = logo ?? providerOf(provider)?.logo;
    if (logo) return `<img class="p-icon" src="${esc(logo)}" alt="" aria-hidden="true">`;
    return `<span class="p-icon p-mono ${esc(provider)}" aria-hidden="true">${esc(MONOGRAM[provider] || (provider || "?")[0].toUpperCase())}</span>`;
  }
  const pill = (provider, label, logo) =>
    `<span class="pill ${esc(provider)}">${providerIcon(provider, logo)}${esc(label)}</span>`;

  async function loadModels() {
    const data = await api("/api/models");
    state.providers = data.providers;
    if (!Object.keys(state.defaults).length) {
      state.defaults = data.defaults;
      applyDefaults();
    }
    renderModelPickers();
  }

  function renderModelPickers() {
    const models = allModels();
    $("#no-providers").classList.toggle("hidden", models.length > 0);

    const sel = $("#model");
    const previous = sel.value || store.get("model");
    sel.innerHTML = "";
    for (const p of state.providers) {
      const group = document.createElement("optgroup");
      group.label = p.configured ? p.label : `${p.label} — no credentials`;
      if (!p.configured) {
        const o = new Option("Set credentials in Admin", "", false, false);
        o.disabled = true;
        group.appendChild(o);
      }
      for (const m of p.models) group.appendChild(new Option(m.model, m.id));
      sel.appendChild(group);
    }
    if (models.some((m) => m.id === previous)) sel.value = previous;
    else if (models.length) sel.value = models[0].id;

    const box = $("#compare-models");
    const checked = new Set($$("input:checked", box).map((i) => i.value));
    const known = new Set($$("input", box).map((i) => i.value));
    const firstRender = !box.children.length;
    box.innerHTML = models.length
      ? models.map((m) => `
          <label class="check"><input type="checkbox" value="${esc(m.id)}" ${firstRender || checked.has(m.id) || !known.has(m.id) ? "checked" : ""}>
            ${pill(m.provider, m.label)}<span class="mono">${esc(m.model)}</span></label>`).join("")
      : `<p class="hint">No configured models.</p>`;
    const skipped = state.providers.filter((p) => !p.configured).map((p) => p.label);
    if (skipped.length) box.insertAdjacentHTML("beforeend", `<p class="hint">Skipped (no credentials): ${esc(skipped.join(", "))}</p>`);
    updateCompareCount();
    updateParamWarnings();
  }

  function updateCompareCount() {
    const n = $$("#compare-models input:checked").length;
    $("#compare-count").textContent = `(${n} selected)`;
  }

  // ---------- parameters ----------
  const NUM = ["temperature", "top_p", "top_k", "max_tokens"];
  const BOOL = ["use_temperature", "use_top_p", "use_top_k", "reasoning"];

  function applyDefaults() {
    const d = state.defaults;
    NUM.forEach((k) => { $(`#${k}`).value = d[k]; const r = $(`#${k}_range`); if (r) r.value = d[k]; });
    BOOL.forEach((k) => { $(`#${k}`).checked = !!d[k]; });
    syncParamStates();
  }

  function syncParamStates() {
    ["temperature", "top_p", "top_k"].forEach((k) => {
      const on = $(`#use_${k}`).checked && !$("#reasoning").checked;
      $(`#${k}`).disabled = !on;
      $(`#${k}_range`).disabled = !on;
      $(`#${k}`).closest(".param").classList.toggle("off", !on);
    });
    updateParamWarnings();
  }

  function readParams() {
    const p = {};
    NUM.forEach((k) => { p[k] = Number($(`#${k}`).value); });
    BOOL.forEach((k) => { p[k] = $(`#${k}`).checked; });
    p.system = $("#system").value;
    p.user = $("#user").value;
    return p;
  }

  function updateParamWarnings() {
    const out = $("#param-warnings");
    if (!out) return;
    const p = readParams();
    const targets = state.view === "compare"
      ? [...new Set($$("#compare-models input:checked").map((i) => i.value.split(":")[0]))]
      : [($("#model").value || "").split(":")[0]].filter(Boolean);
    const msgs = [];
    if (!p.reasoning) {
      for (const knob of ["temperature", "top_p", "top_k"]) {
        if (!p[`use_${knob}`]) continue;
        const unsupported = targets.map(providerOf).filter((s) => s && s.supports && !s.supports[knob]).map((s) => s.label);
        if (unsupported.length) msgs.push(`${knob} is not sent to ${unsupported.join(", ")}.`);
      }
    }
    if (state.view === "single") {
      const spec = providerOf(targets[0]);
      $("#model-notes").textContent = spec?.notes || "";
    }
    out.textContent = msgs.join(" ");
  }

  // ---------- run ----------
  async function callModel(id, params) {
    const i = id.indexOf(":");
    return api("/api/generate", {
      method: "POST",
      body: { ...params, provider: id.slice(0, i), model: id.slice(i + 1) },
    });
  }

  function validate(params) {
    if (!params.user.trim()) { toast("Write a user message first", "error"); $("#user").focus(); return false; }
    for (const k of NUM) {
      const el = $(`#${k}`);
      if (!el.disabled && !el.checkValidity()) { toast(`${k}: ${el.validationMessage}`, "error"); el.focus(); return false; }
    }
    return true;
  }

  async function onRun(ev) {
    ev?.preventDefault();
    if (state.running) return;
    const params = readParams();
    if (!validate(params)) return;
    store.set("model", $("#model").value);
    state.running = true;
    $("#run-btn").disabled = true;
    try {
      if (state.view === "compare") await runCompare(params);
      else await runSingle(params);
    } finally {
      state.running = false;
      $("#run-btn").disabled = false;
    }
  }

  // ----- single -----
  async function runSingle(params) {
    const id = $("#model").value;
    const root = $("#single-result");
    if (!id) { toast("No model selected — add credentials in Admin", "error"); return; }
    const m = allModels().find((x) => x.id === id);
    root.innerHTML = `<div class="panel result-card loading">
      <div class="result-head">${pill(m.provider, m.label)}<span class="mono">${esc(m.model)}</span></div>
      <div class="spinner-row"><span class="spinner"></span> Waiting for the model…</div></div>`;
    let r;
    try { r = await callModel(id, params); }
    catch (e) { r = { ok: false, provider: m.provider, model: m.model, error: e.message }; }
    root.innerHTML = singleHtml(r, m);
    $(".copy-btn", root)?.addEventListener("click", () => copy(r.text || ""));
  }

  function statusChip(r) {
    if (!r) return `<span class="chip wait"><span class="spinner sm"></span> running</span>`;
    if (!r.ok) return `<span class="chip err">error</span>`;
    if (!r.text) return `<span class="chip warn">empty</span>`;
    if (r.truncated) return `<span class="chip warn">truncated</span>`;
    return `<span class="chip ok">ok</span>`;
  }

  function paramsHtml(r) {
    const sent = Object.entries(r.sent || {}).map(([k, v]) => `<code>${esc(k)}=${esc(String(v))}</code>`).join(" ");
    const skipped = (r.skipped || []).map((s) => `<li>${esc(s)}</li>`).join("");
    return `<details class="params-used"><summary>Parameters sent${r.skipped?.length ? ` · ${r.skipped.length} skipped` : ""}</summary>
      <div>${sent || "—"}</div>${skipped ? `<ul>${skipped}</ul>` : ""}</details>`;
  }

  function singleHtml(r, m) {
    const head = `<div class="result-head">${pill(m.provider, m.label)}
      <span class="mono">${esc(m.model)}</span>${statusChip(r)}</div>`;
    if (!r.ok) {
      return `<div class="panel result-card">${head}
        <div class="error-box"><b>Request failed</b> after ${fmtMs(r.latency_ms)}<pre>${esc(r.error || "Unknown error")}</pre></div></div>`;
    }
    const reasoning = r.reasoning_tokens != null ? `<small>incl. ${fmt(r.reasoning_tokens)} reasoning</small>` : "";
    return `<div class="panel result-card">${head}
      <div class="metrics">
        <div class="metric"><span>Input tokens</span><b>${fmt(r.input_tokens)}</b></div>
        <div class="metric"><span>Output tokens</span><b>${fmt(r.output_tokens)}</b>${reasoning}</div>
        <div class="metric"><span>Total tokens</span><b>${fmt(r.total_tokens)}</b></div>
        <div class="metric"><span>Latency</span><b>${fmtMs(r.latency_ms)}</b></div>
      </div>
      ${r.warning ? `<div class="warn-box">${esc(r.warning)}</div>` : ""}
      <div class="response-head"><span class="field-label">Answer</span>
        <span class="muted">${fmt(wordCount(r.text))} words · ${fmt((r.text || "").length)} chars</span>
        <button type="button" class="ghost copy-btn">Copy</button></div>
      <div class="response">${esc(r.text || "")}</div>
      ${paramsHtml(r)}</div>`;
  }

  // ----- compare -----
  async function runCompare(params) {
    const ids = $$("#compare-models input:checked").map((i) => i.value);
    if (!ids.length) { toast("Select at least one model", "error"); return; }
    const models = allModels();
    state.compare = ids.map((id) => ({ ...models.find((m) => m.id === id), result: null }));
    if (!ids.includes(state.baseline)) state.baseline = ids[0];
    renderCompare();
    // Fire all requests at once; each row fills in as its provider answers.
    await Promise.all(state.compare.map(async (row) => {
      try { row.result = await callModel(row.id, params); }
      catch (e) { row.result = { ok: false, error: e.message, latency_ms: null }; }
      renderCompare();
    }));
    const failed = state.compare.filter((r) => !r.result.ok).length;
    toast(failed ? `Done — ${failed} of ${state.compare.length} failed` : "All models answered", failed ? "warn" : "ok");
  }

  function computeDiffs() {
    const base = state.compare.find((r) => r.id === state.baseline);
    const baseText = base?.result?.ok ? base.result.text : null;
    for (const row of state.compare) {
      row.diff = null;
      if (baseText == null || !row.result?.ok) continue;
      if (row.id === state.baseline) { row.diff = { similarity: 1, ops: null, unit: "self" }; continue; }
      row.diff = window.TextDiff.diff(baseText, row.result.text || "");
    }
  }

  function renderCompare() {
    const root = $("#compare-result");
    const rows = state.compare;
    if (!rows.length) return;
    computeDiffs();
    const done = rows.filter((r) => r.result).length;
    const okRows = rows.filter((r) => r.result?.ok);
    const maxOut = Math.max(1, ...okRows.map((r) => r.result.output_tokens || 0));
    const maxLat = Math.max(1, ...rows.map((r) => r.result?.latency_ms || 0));
    const best = (key, fn = Math.min) => (okRows.length > 1 ? fn(...okRows.map((r) => r.result[key] ?? Infinity)) : null);
    const fastest = best("latency_ms");
    const leanest = best("total_tokens");

    const tableRows = rows.map((row, i) => {
      const r = row.result;
      const sim = row.diff?.similarity;
      const simCell = row.id === state.baseline ? `<span class="muted">baseline</span>`
        : sim == null ? "—" : `<div class="bar-cell"><span>${Math.round(sim * 100)}%</span><i style="width:${Math.round(sim * 100)}%" class="bar sim"></i></div>`;
      return `<tr data-card="card-${i}" class="${row.id === state.baseline ? "is-baseline" : ""}">
        <td>${pill(row.provider, row.label)}<div class="mono small">${esc(row.model)}</div></td>
        <td>${statusChip(r)}</td>
        <td class="num">${r ? `<div class="bar-cell"><span>${fmtMs(r.latency_ms)}${r.latency_ms === fastest ? " ⚡" : ""}</span><i class="bar lat" style="width:${Math.round(100 * (r.latency_ms || 0) / maxLat)}%"></i></div>` : ""}</td>
        <td class="num">${r?.ok ? fmt(r.input_tokens) : ""}</td>
        <td class="num">${r?.ok ? `<div class="bar-cell"><span>${fmt(r.output_tokens)}${r.reasoning_tokens ? ` <small class="muted">(${fmt(r.reasoning_tokens)} rsn)</small>` : ""}</span><i class="bar out" style="width:${Math.round(100 * r.output_tokens / maxOut)}%"></i></div>` : ""}</td>
        <td class="num">${r?.ok ? `${fmt(r.total_tokens)}${r.total_tokens === leanest ? " ↓" : ""}` : ""}</td>
        <td class="num">${r?.ok ? fmt(wordCount(r.text)) : ""}</td>
        <td class="num">${simCell}</td>
      </tr>`;
    }).join("");

    const baselineOptions = rows.map((r) => `<option value="${esc(r.id)}" ${r.id === state.baseline ? "selected" : ""}>${esc(r.label)} · ${esc(r.model)}</option>`).join("");

    const cards = rows.map((row, i) => {
      const r = row.result;
      let body;
      if (!r) body = `<div class="spinner-row"><span class="spinner"></span> Waiting…</div>`;
      else if (!r.ok) body = `<div class="error-box"><pre>${esc(r.error || "Unknown error")}</pre></div>`;
      else if (state.compareView === "diff" && row.id !== state.baseline) {
        body = row.diff?.ops
          ? `<div class="response diff">${window.TextDiff.toHtml(row.diff.ops)}</div>`
          : `<div class="response">${esc(r.text)}</div><p class="hint">${row.diff ? "Too long to diff." : "Baseline has no answer to diff against."}</p>`;
      } else body = `<div class="response">${esc(r.text)}</div>`;
      const meta = r?.ok ? `${fmt(r.input_tokens)} in · ${fmt(r.output_tokens)} out · ${fmtMs(r.latency_ms)}` : r ? fmtMs(r.latency_ms) : "";
      return `<article class="panel compare-card ${row.id === state.baseline ? "is-baseline" : ""}" id="card-${i}">
        <div class="result-head">${pill(row.provider, row.label)}
          <span class="mono">${esc(row.model)}</span>${statusChip(r)}
          ${row.id === state.baseline ? `<span class="chip base">baseline</span>` : ""}</div>
        <div class="muted small">${meta}</div>
        ${r?.warning ? `<div class="warn-box">${esc(r.warning)}</div>` : ""}
        ${body}
        ${r?.ok ? `<div class="card-foot">${paramsHtml(r)}<button type="button" class="ghost" data-copy="${i}">Copy</button></div>` : ""}
      </article>`;
    }).join("");

    root.innerHTML = `
      <div class="panel">
        <div class="row-between">
          <h2>Comparison <span class="muted">${done}/${rows.length} done</span></h2>
          <div class="btn-row">
            <button type="button" class="ghost" id="export-csv" ${done < rows.length ? "disabled" : ""}>Export CSV</button>
            <button type="button" class="ghost" id="export-json" ${done < rows.length ? "disabled" : ""}>Export JSON</button>
          </div>
        </div>
        <div class="table-wrap"><table class="compare-table">
          <thead><tr><th>Model</th><th>Status</th><th class="num">Latency</th><th class="num">Input tok.</th>
            <th class="num">Output tok.</th><th class="num">Total tok.</th><th class="num">Words</th><th class="num">Similarity</th></tr></thead>
          <tbody>${tableRows}</tbody></table></div>
        <p class="hint">Similarity = share of words the answer has in common with the baseline (word-level diff). ⚡ fastest · ↓ fewest tokens. Click a row to jump to its answer.</p>
      </div>
      <div class="panel toolbar">
        <label>Baseline <select id="baseline">${baselineOptions}</select></label>
        <div class="segmented">
          <button type="button" data-cv="side" class="${state.compareView === "side" ? "active" : ""}">Side by side</button>
          <button type="button" data-cv="diff" class="${state.compareView === "diff" ? "active" : ""}">Diff vs baseline</button>
        </div>
        ${state.compareView === "diff" ? `<span class="legend"><del>only in baseline</del> <ins>only in this answer</ins></span>` : ""}
      </div>
      <div class="cards">${cards}</div>`;

    $("#baseline", root).addEventListener("change", (e) => { state.baseline = e.target.value; renderCompare(); });
    $$("[data-cv]", root).forEach((b) => b.addEventListener("click", () => { state.compareView = b.dataset.cv; renderCompare(); }));
    $$("tr[data-card]", root).forEach((tr) => tr.addEventListener("click", () => $(`#${tr.dataset.card}`)?.scrollIntoView({ behavior: "smooth", block: "start" })));
    $$("[data-copy]", root).forEach((b) => b.addEventListener("click", () => copy(rows[+b.dataset.copy].result.text || "")));
    $("#export-csv", root).addEventListener("click", exportCsv);
    $("#export-json", root).addEventListener("click", exportJson);
  }

  function exportRows() {
    return state.compare.map((row) => ({
      provider: row.provider,
      model: row.model,
      ok: !!row.result?.ok,
      latency_ms: row.result?.latency_ms ?? "",
      input_tokens: row.result?.input_tokens ?? "",
      output_tokens: row.result?.output_tokens ?? "",
      reasoning_tokens: row.result?.reasoning_tokens ?? "",
      total_tokens: row.result?.total_tokens ?? "",
      truncated: row.result?.truncated ?? "",
      stop_reason: row.result?.stop_reason ?? "",
      similarity_to_baseline: row.diff?.similarity != null ? row.diff.similarity.toFixed(3) : "",
      response: row.result?.ok ? row.result.text : "",
      error: row.result?.error ?? "",
    }));
  }

  const stamp = () => new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");

  function exportCsv() {
    const rows = exportRows();
    const cols = Object.keys(rows[0]);
    const cell = (v) => `"${String(v).replace(/"/g, '""')}"`;
    const csv = [cols.join(","), ...rows.map((r) => cols.map((c) => cell(r[c])).join(","))].join("\r\n");
    download(`llm-compare-${stamp()}.csv`, "﻿" + csv, "text/csv");
  }

  function exportJson() {
    const p = readParams();
    const baseline = state.compare.find((r) => r.id === state.baseline);
    download(`llm-compare-${stamp()}.json`, JSON.stringify({
      request: p, baseline: baseline ? `${baseline.provider}:${baseline.model}` : null, results: exportRows(),
    }, null, 2), "application/json");
  }

  // ---------- admin ----------
  async function renderAdmin() {
    const root = $("#admin-root");
    root.innerHTML = `<div class="panel"><span class="spinner"></span> Loading…</div>`;
    let status;
    try { status = await api("/api/admin/status"); }
    catch (e) { root.innerHTML = `<div class="panel error-box">${esc(e.message)}</div>`; return; }

    if (!status.password_set) return renderPasswordForm(root, "setup", status);
    if (!store.get("adminToken")) return renderPasswordForm(root, "login", status);
    try {
      const cfg = await api("/api/admin/config", { auth: true });
      renderConfig(root, cfg);
    } catch (e) {
      if (e.status === 401) { store.set("adminToken", null); return renderPasswordForm(root, "login", status); }
      root.innerHTML = `<div class="panel error-box">${esc(e.message)}</div>`;
    }
  }

  function renderPasswordForm(root, kind, status) {
    const setup = kind === "setup";
    root.innerHTML = `<form class="panel auth-card" id="auth-form">
      <h2>${setup ? "Set an admin password" : "Admin login"}</h2>
      <p class="hint">${setup
        ? `First run: choose the password that protects this page. It is saved as ADMIN_PASSWORD in <code>${esc(status.env_file)}</code>.`
        : "Enter the admin password (ADMIN_PASSWORD in your .env)."}</p>
      <label class="field-label" for="pw">Password</label>
      <input type="password" id="pw" required minlength="${setup ? 6 : 1}" autocomplete="${setup ? "new-password" : "current-password"}">
      ${setup ? `<label class="field-label" for="pw2">Confirm password</label><input type="password" id="pw2" required autocomplete="new-password">` : ""}
      <p class="hint warn" id="auth-error"></p>
      <button class="primary" type="submit">${setup ? "Save password" : "Log in"}</button>
    </form>`;
    $("#pw", root).focus();
    $("#auth-form", root).addEventListener("submit", async (e) => {
      e.preventDefault();
      const pw = $("#pw", root).value;
      if (setup && pw !== $("#pw2", root).value) { $("#auth-error").textContent = "Passwords don't match."; return; }
      try {
        const res = await api(`/api/admin/${setup ? "setup" : "login"}`, { method: "POST", body: { password: pw } });
        store.set("adminToken", res.token);
        renderAdmin();
      } catch (err) { $("#auth-error").textContent = err.message; }
    });
  }

  const normModels = (s) => [...new Set((s || "").split(/[,\n]/).map((m) => m.trim()).filter(Boolean))].join("\n");

  function fieldHtml(p, f) {
    const id = `f-${f.key}`;
    if (f.multiline) {
      const shown = f.is_set ? f.value : f.default;
      return `<div class="admin-field">
        <div class="row-between">
          <label class="field-label" for="${id}">${esc(f.label)}
            ${f.is_set ? "" : `<span class="chip base">defaults</span>`}</label>
          ${f.default ? `<a href="#" class="small" data-restore="${id}">Restore defaults</a>` : ""}
        </div>
        <textarea id="${id}" class="models-box mono" rows="3" spellcheck="false" data-key="${esc(f.key)}" data-models="1"
          data-original="${esc(shown)}" data-default="${esc(f.default)}" placeholder="${esc(f.default)}">${esc(shown)}</textarea>
        ${f.help ? `<p class="hint">${esc(f.help)}</p>` : ""}</div>`;
    }
    const req = f.required ? "" : ` <span class="muted">(optional)</span>`;
    const input = f.secret
      ? `<div class="secret-row">
           <input type="password" id="${id}" data-key="${esc(f.key)}" data-secret="1"
             placeholder="${f.is_set ? `${esc(f.value)} (saved — blank keeps it)` : "Not set"}" autocomplete="new-password">
           <button type="button" class="ghost sm" data-reveal="${id}" title="Show / hide">👁</button>
           ${f.is_set ? `<label class="clear-toggle"><input type="checkbox" data-clear="${esc(f.key)}"> clear</label>` : ""}
         </div>`
      : `<input type="text" id="${id}" data-key="${esc(f.key)}" data-original="${esc(f.value)}" value="${esc(f.value)}"
           placeholder="${esc(f.placeholder || f.default || "")}">`;
    return `<div class="admin-field">
      <label class="field-label" for="${id}">${esc(f.label)}${req}</label>
      ${input}${f.help ? `<p class="hint">${esc(f.help)}</p>` : ""}</div>`;
  }

  function renderConfig(root, cfg) {
    root.innerHTML = `
      <div class="panel row-between admin-top">
        <div><h2>Admin — provider credentials</h2>
          <p class="hint">Saved to <code>${esc(cfg.env_file)}</code>. A provider is used only when its credentials are set.</p></div>
        <div class="admin-actions">
          <div class="popover-wrap">
            <button type="button" class="ghost" id="pw-toggle" aria-expanded="false" aria-controls="pw-form">Change password</button>
            <form class="popover hidden" id="pw-form">
              <h3>Change admin password</h3>
              <label class="field-label" for="new-pw">New password</label>
              <input type="password" id="new-pw" minlength="6" required autocomplete="new-password">
              <label class="field-label" for="new-pw2">Confirm password</label>
              <input type="password" id="new-pw2" minlength="6" required autocomplete="new-password">
              <p class="hint warn" id="pw-error"></p>
              <div class="btn-row">
                <button type="submit" class="primary">Update</button>
                <button type="button" class="ghost" id="pw-cancel">Cancel</button>
              </div>
            </form>
          </div>
          <button type="button" class="ghost" id="logout">Log out</button>
        </div>
      </div>
      <div class="admin-grid">
        ${cfg.providers.map((p) => `
          <form class="panel provider-card" data-provider="${esc(p.provider)}">
            <div class="row-between">
              <h3>${pill(p.provider, p.label, p.logo)}</h3>
              <span class="chip ${p.configured ? "ok" : "off"}">${p.configured ? "configured" : "not configured"}</span>
            </div>
            ${p.fields.map((f) => fieldHtml(p, f)).join("")}
            ${p.notes ? `<p class="hint">${esc(p.notes)}</p>` : ""}
            <div class="btn-row">
              <button type="submit" class="primary">Save</button>
              <button type="button" class="ghost" data-test="${esc(p.provider)}" ${p.configured ? "" : "disabled"}>Test connection</button>
            </div>
            <div class="test-result hint"></div>
          </form>`).join("")}
      </div>`;

    $("#logout", root).addEventListener("click", async () => {
      try { await api("/api/admin/logout", { method: "POST", auth: true }); } catch { /* already gone */ }
      store.set("adminToken", null);
      renderAdmin();
    });

    $$("[data-restore]", root).forEach((a) => a.addEventListener("click", (e) => {
      e.preventDefault();
      const box = $(`#${a.dataset.restore}`);
      box.value = box.dataset.default;
      box.focus();
    }));

    $$("[data-reveal]", root).forEach((b) => b.addEventListener("click", () => {
      const inp = $(`#${b.dataset.reveal}`);
      inp.type = inp.type === "password" ? "text" : "password";
    }));

    $$("form.provider-card[data-provider]", root).forEach((form) => {
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const values = {}, clear = [];
        $$("[data-key]", form).forEach((inp) => {
          const key = inp.dataset.key;
          if (inp.dataset.models) {
            const v = normModels(inp.value);
            if (v === normModels(inp.dataset.original)) return;
            // Empty, or identical to the defaults -> unset, so the provider follows the defaults.
            if (!v || v === normModels(inp.dataset.default)) clear.push(key); else values[key] = v;
            return;
          }
          const v = inp.value.trim();
          if (inp.dataset.secret) { if (v) values[key] = v; }
          else if (v !== inp.dataset.original) { if (v) values[key] = v; else clear.push(key); }
        });
        $$("input[data-clear]:checked", form).forEach((c) => { if (!values[c.dataset.clear]) clear.push(c.dataset.clear); });
        if (!Object.keys(values).length && !clear.length) { toast("Nothing changed"); return; }
        try {
          const next = await api("/api/admin/config", { method: "PUT", auth: true, body: { values, clear } });
          renderConfig(root, next);
          await loadModels();
          toast(`${form.dataset.provider} saved`, "ok");
        } catch (err) { toast(err.message, "error"); }
      });
    });

    $$("[data-test]", root).forEach((b) => b.addEventListener("click", async () => {
      const out = $(".test-result", b.closest("form"));
      out.innerHTML = `<span class="spinner sm"></span> Testing…`;
      b.disabled = true;
      try {
        const r = await api(`/api/admin/test/${b.dataset.test}`, { method: "POST", auth: true });
        out.innerHTML = r.ok
          ? `<span class="chip ok">ok</span> ${esc(r.model)} answered in ${fmtMs(r.latency_ms)}: “${esc(r.text)}”`
          : `<span class="chip err">failed</span> ${esc(r.model || "")} <pre>${esc(r.error)}</pre>`;
      } catch (err) { out.innerHTML = `<span class="chip err">failed</span> ${esc(err.message)}`; }
      b.disabled = false;
    }));

    const pwForm = $("#pw-form", root);
    const pwToggle = $("#pw-toggle", root);
    const setPwOpen = (open) => {
      pwForm.classList.toggle("hidden", !open);
      pwToggle.setAttribute("aria-expanded", String(open));
      if (open) $("#new-pw", root).focus();
      else { pwForm.reset(); $("#pw-error", root).textContent = ""; }
    };
    pwToggle.addEventListener("click", () => setPwOpen(pwForm.classList.contains("hidden")));
    $("#pw-cancel", root).addEventListener("click", () => setPwOpen(false));
    pwForm.addEventListener("keydown", (e) => { if (e.key === "Escape") setPwOpen(false); });
    // Close when clicking anywhere outside the popover (the listener removes itself once the page is re-rendered).
    const outside = (e) => {
      if (!document.body.contains(pwForm)) { document.removeEventListener("click", outside); return; }
      if (!pwForm.classList.contains("hidden") && !e.target.closest(".popover-wrap")) setPwOpen(false);
    };
    document.addEventListener("click", outside);
    pwForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const pw = $("#new-pw", root).value;
      if (pw !== $("#new-pw2", root).value) { $("#pw-error", root).textContent = "Passwords don't match."; return; }
      try {
        await api("/api/admin/password", { method: "POST", auth: true, body: { password: pw } });
        setPwOpen(false);
        toast("Password updated", "ok");
      } catch (err) { $("#pw-error", root).textContent = err.message; }
    });
  }

  // ---------- wiring ----------
  function init() {
    $$(".tab").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
    document.addEventListener("click", (e) => {
      const go = e.target.closest("[data-goto]");
      if (go) { e.preventDefault(); setView(go.dataset.goto); }
    });
    $("#request-form").addEventListener("submit", onRun);
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && state.view !== "admin") onRun(e);
    });
    $("#model").addEventListener("change", updateParamWarnings);
    $("#compare-models").addEventListener("change", () => { updateCompareCount(); updateParamWarnings(); });
    $("#select-all").addEventListener("click", (e) => { e.preventDefault(); $$("#compare-models input").forEach((i) => { i.checked = true; }); updateCompareCount(); updateParamWarnings(); });
    $("#select-none").addEventListener("click", (e) => { e.preventDefault(); $$("#compare-models input").forEach((i) => { i.checked = false; }); updateCompareCount(); updateParamWarnings(); });
    $("#reset-params").addEventListener("click", (e) => { e.preventDefault(); applyDefaults(); });

    ["temperature", "top_p", "top_k"].forEach((k) => {
      const num = $(`#${k}`), range = $(`#${k}_range`);
      range.addEventListener("input", () => { num.value = range.value; });
      num.addEventListener("input", () => { range.value = num.value; });
      $(`#use_${k}`).addEventListener("change", syncParamStates);
    });
    $("#reasoning").addEventListener("change", syncParamStates);

    // Keep the drafted request across reloads (per browser tab).
    ["system", "user"].forEach((k) => {
      const el = $(`#${k}`);
      el.value = store.get(`draft-${k}`) || "";
      el.addEventListener("input", () => store.set(`draft-${k}`, el.value));
    });

    const initial = (location.hash || "").slice(1);
    setView(["single", "compare", "admin"].includes(initial) ? initial : "single");
    loadModels().catch((e) => toast(`Could not load models: ${e.message}`, "error"));
  }

  init();
})();
