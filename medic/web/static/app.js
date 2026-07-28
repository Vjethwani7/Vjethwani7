/* medic dashboard front end.
 *
 * Deliberately dependency-free and build-free: the tool it fronts has no
 * runtime dependencies either, and a diagnostic you reach for when the
 * machine is misbehaving should not need a toolchain to render.
 *
 * All DOM insertion goes through text nodes or `el()`, never innerHTML with
 * interpolated data - findings contain process names, file paths and log
 * lines straight off the machine, none of which is trustworthy markup.
 */

(() => {
  "use strict";

  // The token arrives as a query parameter; keep it in memory and strip it
  // from the visible URL so it is not left sitting in the address bar.
  const params = new URLSearchParams(location.search);
  const TOKEN = params.get("token") || "";
  if (params.has("token")) {
    history.replaceState(null, "", location.pathname);
  }

  const SEVERITY_ORDER = ["critical", "warn", "unknown", "info", "ok"];

  const state = {
    meta: null,
    diagnosis: null,
    pendingPlans: null,
  };

  // ---------- tiny DOM helper ----------

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (value === null || value === undefined || value === false) continue;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
        else node.setAttribute(key, value === true ? "" : value);
      }
    }
    for (const child of [].concat(children || [])) {
      if (child === null || child === undefined || child === false) continue;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    }
    return node;
  }

  const $ = (id) => document.getElementById(id);

  function show(node, visible) {
    node.hidden = !visible;
  }

  // ---------- api ----------

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {
        "X-Medic-Token": TOKEN,
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
      },
    });

    let payload = null;
    try {
      payload = await response.json();
    } catch {
      throw new Error(`${response.status} ${response.statusText}`);
    }
    if (!response.ok) {
      throw new Error(payload && payload.error ? payload.error : `request failed (${response.status})`);
    }
    return payload;
  }

  /** Start a job and poll until it finishes, reporting progress as it goes. */
  async function runJob(path, body, onProgress) {
    let job = await api(path, { method: "POST", body: JSON.stringify(body || {}) });

    while (job.status === "running") {
      await new Promise((resolve) => setTimeout(resolve, 220));
      job = await api(`/api/job/${encodeURIComponent(job.id)}`);
      if (onProgress) onProgress(job.progress || {});
    }

    if (job.status === "error") throw new Error(job.error || "the job failed");
    return job.result;
  }

  // ---------- toast ----------

  let toastTimer = null;

  function toast(message, isError) {
    const node = $("toast");
    node.textContent = message;
    node.classList.toggle("error", Boolean(isError));
    show(node, true);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => show(node, false), isError ? 8000 : 4000);
  }

  // ---------- rendering: findings ----------

  function renderFindings(diagnosis) {
    const container = $("findings");
    container.textContent = "";

    const findings = [];
    for (const report of diagnosis.reports) {
      for (const finding of report.findings) {
        findings.push({ ...finding, checkName: report.name });
      }
      if (report.error) {
        findings.push({
          check_id: report.check_id,
          checkName: report.name,
          severity: "unknown",
          title: `${report.name} could not complete`,
          detail: report.error,
          fix_ids: [],
          advice: "This is a bug in the check itself, not necessarily a problem with your machine.",
        });
      }
    }

    findings.sort(
      (a, b) => SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity)
    );

    for (const finding of findings) {
      container.appendChild(renderFinding(finding));
    }

    show(container, findings.length > 0);
    show($("all-clear"), findings.length === 0);
  }

  function renderFinding(finding) {
    const fixes = (finding.fix_ids || []).filter((id) => fixIsKnown(id));

    return el("article", { class: `finding finding-${finding.severity}` }, [
      el("div", { class: "finding-head" }, [
        el("span", { class: `chip chip-${finding.severity}`, text: finding.severity }),
        el("span", { class: "finding-title", text: finding.title }),
        el("span", { class: "finding-source", text: finding.check_id }),
      ]),
      finding.detail ? el("pre", { class: "finding-detail", text: finding.detail }) : null,
      finding.advice ? el("p", { class: "finding-advice", text: finding.advice }) : null,
      fixes.length
        ? el(
            "div",
            { class: "finding-actions" },
            fixes.map((id) =>
              el("button", {
                class: "btn btn-ghost btn-small",
                text: `Preview: ${fixName(id)}`,
                onclick: () => openPlans([id]),
              })
            )
          )
        : null,
    ]);
  }

  function fixIsKnown(id) {
    return state.meta && state.meta.fixes.some((fix) => fix.id === id);
  }

  function fixName(id) {
    const fix = state.meta && state.meta.fixes.find((item) => item.id === id);
    return fix ? fix.name : id;
  }

  // ---------- rendering: summary ----------

  function renderSummary(diagnosis) {
    const counts = diagnosis.counts || {};
    const pairs = [
      ["n-critical", counts.critical || 0],
      ["n-warn", counts.warn || 0],
      ["n-info", counts.info || 0],
      ["n-unknown", counts.unknown || 0],
      ["n-checks", counts.checks_run || 0],
    ];
    for (const [id, value] of pairs) {
      const node = $(id);
      node.textContent = String(value);
      node.parentElement.classList.toggle("muted", value === 0 && id !== "n-checks");
    }
    show($("summary"), true);
  }

  function renderSkipped(diagnosis) {
    const skipped = diagnosis.reports.filter((report) => report.skipped_reason);
    const list = $("skipped-list");
    list.textContent = "";

    for (const report of skipped) {
      list.appendChild(
        el("div", {}, [
          el("span", { class: "id", text: report.check_id }),
          el("span", { text: report.skipped_reason }),
        ])
      );
    }

    $("skipped-count").textContent = String(skipped.length);
    show($("skipped"), skipped.length > 0);
  }

  // ---------- rendering: repairs ----------

  function renderFixes() {
    const container = $("fix-list");
    container.textContent = "";

    for (const fix of state.meta.fixes) {
      container.appendChild(
        el("div", { class: `fix${fix.available ? "" : " unavailable"}` }, [
          el("div", { class: "fix-main" }, [
            el("div", { class: "fix-name" }, [
              fix.name,
              el("span", { class: `risk risk-${fix.risk}`, text: fix.risk }),
              fix.requires_root ? el("span", { class: "needs-root", text: "needs root" }) : null,
            ]),
            el("div", {
              class: "fix-desc",
              text: fix.available ? fix.description : fix.unavailable_reason,
            }),
          ]),
          el("button", {
            class: "btn btn-ghost btn-small",
            text: "Preview",
            disabled: !fix.available,
            onclick: () => openPlans([fix.id]),
          }),
        ])
      );
    }

    $("repairs-note").textContent = state.meta.allow_fixes
      ? "Every repair previews first. Nothing runs until you confirm the plan."
      : "This dashboard is read-only. Restart with `medic serve --allow-fixes` to apply repairs from here.";

    show($("repairs"), true);
  }

  // ---------- modal ----------

  function openModal(title) {
    $("modal-title").textContent = title;
    $("modal-cancel").textContent = "Cancel";
    show($("overlay"), true);
    $("modal-close").focus();
  }

  function closeModal() {
    show($("overlay"), false);
    state.pendingPlans = null;
    show($("modal-apply"), false);
  }

  async function openPlans(fixIds) {
    openModal("Repair preview");
    const body = $("modal-body");
    body.textContent = "";
    body.appendChild(el("p", { class: "plan-blocked", text: "Working out what this would do…" }));
    $("modal-note").textContent = "";

    let plans;
    try {
      plans = (await api("/api/fix/preview", {
        method: "POST",
        body: JSON.stringify({ fix_ids: fixIds }),
      })).plans;
    } catch (error) {
      body.textContent = "";
      body.appendChild(el("p", { class: "plan-blocked", text: `Could not build a preview: ${error.message}` }));
      return;
    }

    body.textContent = "";
    const actionable = plans.filter((plan) => !plan.blocked_reason && plan.actions.length);

    for (const plan of plans) {
      body.appendChild(renderPlan(plan));
    }

    if (!actionable.length) {
      $("modal-note").textContent = "Nothing to do — these repairs found nothing to change.";
      show($("modal-apply"), false);
      return;
    }

    const totalBytes = actionable.reduce((sum, plan) => sum + (plan.est_bytes || 0), 0);
    const riskiest = actionable.reduce(
      (worst, plan) => (rank(plan.risk) > rank(worst) ? plan.risk : worst),
      "safe"
    );

    if (!state.meta.allow_fixes) {
      $("modal-note").textContent =
        "Read-only dashboard — restart with `medic serve --allow-fixes` to apply this.";
      show($("modal-apply"), false);
      return;
    }

    $("modal-note").textContent = totalBytes
      ? `Applying this frees about ${humanBytes(totalBytes)}. Highest risk: ${riskiest}.`
      : `Highest risk: ${riskiest}. This cannot be undone automatically.`;

    state.pendingPlans = actionable.map((plan) => plan.fix_id);
    const applyBtn = $("modal-apply");
    applyBtn.textContent =
      riskiest === "safe" ? "Apply these changes" : `Apply anyway (${riskiest})`;
    show(applyBtn, true);
  }

  function rank(risk) {
    return { safe: 0, moderate: 1, risky: 2 }[risk] ?? 0;
  }

  function renderPlan(plan) {
    if (plan.blocked_reason) {
      return el("div", { class: "plan" }, [
        el("div", { class: "plan-head" }, [
          el("span", { class: "name", text: plan.name }),
          el("span", { class: `risk risk-${plan.risk}`, text: plan.risk }),
        ]),
        el("div", { class: "plan-blocked", text: `Cannot run: ${plan.blocked_reason}` }),
      ]);
    }

    if (!plan.actions.length) {
      return el("div", { class: "plan" }, [
        el("div", { class: "plan-head" }, [
          el("span", { class: "name", text: plan.name }),
          el("span", { class: `risk risk-${plan.risk}`, text: plan.risk }),
        ]),
        el("div", { class: "plan-blocked", text: "Nothing to do — found nothing to change." }),
      ]);
    }

    return el("div", { class: "plan" }, [
      el("div", { class: "plan-head" }, [
        el("span", { class: "name", text: plan.name }),
        el("span", { class: `risk risk-${plan.risk}`, text: plan.risk }),
        plan.est_bytes_human
          ? el("span", { class: "finding-source", text: `frees ~${plan.est_bytes_human}` })
          : null,
      ]),
      ...plan.notes.map((note) => el("div", { class: "plan-note", text: note })),
      ...plan.actions.map((action) =>
        el("div", { class: "action" }, [
          el("div", { class: "action-desc", text: action.description }),
          action.argv ? el("div", { class: "action-cmd", text: action.argv.join(" ") }) : null,
          el("div", { class: "action-meta" }, [
            action.undo_note ? el("span", { text: `undo: ${action.undo_note}` }) : null,
            action.requires_root ? el("span", { text: "needs root" }) : null,
          ]),
        ])
      ),
    ]);
  }

  async function applyPending() {
    const fixIds = state.pendingPlans;
    if (!fixIds || !fixIds.length) return;

    const applyBtn = $("modal-apply");
    applyBtn.disabled = true;
    applyBtn.textContent = "Applying…";

    try {
      const payload = await runJob("/api/fix/apply", { fix_ids: fixIds });
      renderResults(payload.results || []);
      const freed = (payload.results || []).reduce((sum, r) => sum + (r.bytes_freed || 0), 0);
      toast(freed ? `Done — reclaimed about ${humanBytes(freed)}.` : "Done.");
      await refreshMeta();
    } catch (error) {
      toast(`Could not apply: ${error.message}`, true);
    } finally {
      applyBtn.disabled = false;
      show(applyBtn, false);
      state.pendingPlans = null;
    }
  }

  function renderResults(results) {
    $("modal-title").textContent = "Repair results";
    // The work is done; "Cancel" would imply it could still be called off.
    $("modal-cancel").textContent = "Close";
    const body = $("modal-body");
    body.textContent = "";

    for (const result of results) {
      body.appendChild(
        el("div", { class: "plan" }, [
          el("div", { class: "plan-head" }, [el("span", { class: "name", text: result.name })]),
          ...(result.results || []).map((item) =>
            el("div", { class: "result-line" }, [
              el("span", {
                class: item.ok ? "result-ok" : "result-fail",
                text: item.ok ? "✓" : "✗",
              }),
              el("span", {}, [
                item.description,
                item.message ? el("div", { class: "action-meta", text: item.message }) : null,
              ]),
            ])
          ),
          result.blocked_reason
            ? el("div", { class: "plan-blocked", text: result.blocked_reason })
            : null,
          result.skipped_reason
            ? el("div", { class: "plan-blocked", text: result.skipped_reason })
            : null,
        ])
      );
    }

    $("modal-note").textContent = "Recorded in the journal — see `medic history`.";
  }

  // ---------- diagnostics run ----------

  async function runDiagnostics() {
    const button = $("run-btn");
    button.disabled = true;
    button.textContent = "Running…";

    show($("intro"), false);
    show($("progress"), true);
    setProgress({ current: 0, total: 0, label: "Starting…" });

    try {
      const diagnosis = await runJob(
        "/api/diagnose",
        { profile: $("profile").value },
        setProgress
      );
      state.diagnosis = diagnosis;
      renderSummary(diagnosis);
      renderFindings(diagnosis);
      renderSkipped(diagnosis);
      renderFixes();
    } catch (error) {
      toast(`Diagnostics failed: ${error.message}`, true);
      show($("intro"), true);
    } finally {
      show($("progress"), false);
      button.disabled = false;
      button.textContent = "Run again";
    }
  }

  function setProgress(progress) {
    const { current = 0, total = 0, label = "" } = progress || {};
    $("progress-label").textContent = label || "Working…";
    $("progress-count").textContent = total ? `${current} / ${total}` : "";
    $("progress-bar").style.width = total ? `${(current / total) * 100}%` : "8%";
  }

  // ---------- helpers ----------

  function humanBytes(bytes) {
    if (!bytes) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return index === 0 ? `${Math.round(value)} B` : `${value.toFixed(1)} ${units[index]}`;
  }

  // ---------- boot ----------

  async function refreshMeta() {
    state.meta = await api("/api/meta");
    return state.meta;
  }

  async function boot() {
    if (!TOKEN) {
      $("host-line").textContent = "no session token — open the URL medic printed";
      toast("This page needs the session token from the `medic serve` output.", true);
      return;
    }

    try {
      const meta = await refreshMeta();
      $("host-line").textContent = `${meta.host} · ${meta.platform}${meta.is_root ? " · root" : ""}`;
      $("version").textContent = meta.version;
      show($("mode-badge"), !meta.allow_fixes);
      $("run-btn").disabled = false;
    } catch (error) {
      $("host-line").textContent = "could not reach the medic server";
      toast(error.message, true);
      return;
    }

    $("run-btn").addEventListener("click", runDiagnostics);
    $("modal-close").addEventListener("click", closeModal);
    $("modal-cancel").addEventListener("click", closeModal);
    $("modal-apply").addEventListener("click", applyPending);
    $("overlay").addEventListener("click", (event) => {
      if (event.target === $("overlay")) closeModal();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !$("overlay").hidden) closeModal();
    });
  }

  boot();
})();
