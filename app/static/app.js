"use strict";

const root = document.documentElement;
const themeButton = document.querySelector("#theme");

const applyTheme = (theme) => {
  root.dataset.theme = theme;
  localStorage.setItem("theme", theme);
  if (themeButton) {
    const dark = theme === "dark";
    themeButton.textContent = dark ? "Light mode" : "Dark mode";
    themeButton.setAttribute("aria-pressed", String(dark));
    themeButton.title = dark ? "Switch to light mode" : "Switch to dark mode";
  }
};

const preferredTheme = localStorage.getItem("theme")
  || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
applyTheme(preferredTheme);
themeButton?.addEventListener("click", () => {
  applyTheme(root.dataset.theme === "dark" ? "light" : "dark");
});

const investigationCheckboxes = Array.from(
  document.querySelectorAll('input[name="investigation_ids"]:not(:disabled)'),
);
const selectAllInvestigations = document.querySelector("#select-all-investigations");
const selectedInvestigationCount = document.querySelector("#selected-investigation-count");
const deleteConfirmation = document.querySelector("[data-delete-confirmation]");
const deleteSelectedButton = document.querySelector("[data-delete-selected]");
const compareSelectedButton = document.querySelector("[data-compare-selected]");
const refreshInvestigationSelection = () => {
  const selected = investigationCheckboxes.filter((checkbox) => checkbox.checked).length;
  if (compareSelectedButton) compareSelectedButton.disabled = selected < 2 || selected > 5;
  if (selectedInvestigationCount) {
    selectedInvestigationCount.textContent = `${selected} selected`;
  }
  if (selectAllInvestigations) {
    selectAllInvestigations.checked = selected > 0 && selected === investigationCheckboxes.length;
    selectAllInvestigations.indeterminate = selected > 0 && selected < investigationCheckboxes.length;
  }
  if (deleteSelectedButton) {
    deleteSelectedButton.disabled = selected === 0
      || deleteConfirmation?.value !== "DELETE SELECTED";
  }
};
selectAllInvestigations?.addEventListener("change", () => {
  investigationCheckboxes.forEach((checkbox) => {
    checkbox.checked = selectAllInvestigations.checked;
  });
  refreshInvestigationSelection();
});
investigationCheckboxes.forEach((checkbox) => {
  checkbox.addEventListener("change", refreshInvestigationSelection);
});
deleteConfirmation?.addEventListener("input", refreshInvestigationSelection);
refreshInvestigationSelection();

const copyTextValue = async (value) => {
  if (navigator.clipboard?.writeText && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  textarea.style.pointerEvents = "none";
  document.body.append(textarea);
  textarea.select();
  textarea.setSelectionRange(0, textarea.value.length);
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("copy failed");
};

document.querySelectorAll("[data-copy-value]").forEach((button) => {
  button.addEventListener("click", async () => {
    const value = button.dataset.copyValue || "";
    if (!value) return;
    const original = button.textContent;
    try {
      await copyTextValue(value);
      button.textContent = "Copied";
    } catch {
      button.textContent = "Copy failed";
    }
    window.setTimeout(() => { button.textContent = original; }, 1600);
  });
});

document.querySelectorAll("form[data-open-browser-desktop]").forEach((form) => {
  form.addEventListener("submit", () => {
    const target = form.dataset.openBrowserDesktop || "";
    if (!target) return;
    const opened = window.open(target, "_blank", "noopener,noreferrer");
    if (opened) opened.opener = null;
  });
});

const jsonTokenPattern = /"(?:\\u[\da-fA-F]{4}|\\[^u]|[^\\"])*"(?:\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;
const highlightJson = (container) => {
  const code = container.querySelector("code");
  if (!code || code.dataset.highlighted === "true") return;
  const source = code.textContent || "";
  const fragment = document.createDocumentFragment();
  let offset = 0;
  source.matchAll(jsonTokenPattern).forEach((match) => {
    const index = match.index ?? offset;
    if (index > offset) fragment.append(document.createTextNode(source.slice(offset, index)));
    const token = match[0];
    const span = document.createElement("span");
    if (token.startsWith('"')) {
      span.className = /:\s*$/.test(token) ? "json-token-key" : "json-token-string";
    } else if (token === "true" || token === "false") {
      span.className = "json-token-boolean";
    } else if (token === "null") {
      span.className = "json-token-null";
    } else {
      span.className = "json-token-number";
    }
    span.textContent = token;
    fragment.append(span);
    offset = index + token.length;
  });
  if (offset < source.length) fragment.append(document.createTextNode(source.slice(offset)));
  code.replaceChildren(fragment);
  code.dataset.highlighted = "true";
};

document.querySelectorAll("[data-json-syntax]").forEach((container) => {
  if (!container.closest(".json-dialog")) highlightJson(container);
});

const avatarDialog = document.querySelector("#avatar-preview-dialog");
const avatarPreviewImage = document.querySelector("#avatar-preview-image");
const avatarPreviewOpen = document.querySelector("#avatar-preview-open");
const avatarPreviewReverseLinks = document.querySelectorAll("[data-reverse-provider]");

const bindFindingInteractions = (scope = document) => {
  scope.querySelectorAll("[data-json-dialog]:not([data-live-bound])").forEach((button) => {
    button.dataset.liveBound = "true";
    button.addEventListener("click", () => {
      const dialog = document.getElementById(button.dataset.jsonDialog || "");
      const jsonContainer = dialog?.querySelector("[data-json-syntax]");
      if (jsonContainer) highlightJson(jsonContainer);
      if (dialog?.showModal) dialog.showModal();
    });
  });
  scope.querySelectorAll("[data-close-json-dialog]:not([data-live-bound])").forEach((button) => {
    button.dataset.liveBound = "true";
    button.addEventListener("click", () => button.closest("dialog")?.close());
  });
  scope.querySelectorAll(".json-dialog:not([data-live-bound])").forEach((dialog) => {
    dialog.dataset.liveBound = "true";
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
  });
  scope.querySelectorAll("[data-copy-json-target]:not([data-live-bound])").forEach((button) => {
    button.dataset.liveBound = "true";
    button.addEventListener("click", async () => {
      const target = document.getElementById(button.dataset.copyJsonTarget || "");
      const value = target?.querySelector("code")?.textContent || "";
      if (!value) return;
      const original = button.textContent;
      try {
        await navigator.clipboard.writeText(value);
        button.textContent = "Copied";
      } catch {
        button.textContent = "Copy failed";
      }
      window.setTimeout(() => { button.textContent = original; }, 1600);
    });
  });
  scope.querySelectorAll("[data-toggle-json-wrap-target]:not([data-live-bound])").forEach((button) => {
    button.dataset.liveBound = "true";
    button.addEventListener("click", () => {
      const target = document.getElementById(button.dataset.toggleJsonWrapTarget || "");
      const code = target?.querySelector(".json-code");
      if (!code) return;
      const wrapped = code.classList.toggle("wrap-lines");
      button.textContent = wrapped ? "Keep long lines" : "Wrap lines";
    });
  });
  scope.querySelectorAll("[data-avatar-image]:not([data-live-bound])").forEach((image) => {
    image.dataset.liveBound = "true";
    image.addEventListener("error", () => {
      const link = image.closest("a");
      const fallback = link?.nextElementSibling;
      if (link) link.hidden = true;
      if (fallback instanceof HTMLElement) fallback.hidden = false;
    });
  });
  scope.querySelectorAll("[data-avatar-preview]:not([data-live-bound])").forEach((link) => {
    link.dataset.liveBound = "true";
    link.addEventListener("click", (event) => {
      if (!avatarDialog?.showModal || !avatarPreviewImage || !avatarPreviewOpen) return;
      event.preventDefault();
      const source = link.dataset.avatarPreview || link.href;
      avatarPreviewImage.hidden = false;
      avatarPreviewImage.src = source;
      avatarPreviewImage.alt = link.dataset.avatarAlt || "Full-size retained profile image";
      avatarPreviewOpen.href = source;
      const reverseBase = link.dataset.avatarReverseBase || "";
      avatarPreviewReverseLinks.forEach((providerLink) => {
        providerLink.href = reverseBase
          ? `${reverseBase}/${providerLink.dataset.reverseProvider}`
          : "#";
      });
      avatarDialog.showModal();
    });
  });
};

bindFindingInteractions();
avatarDialog?.addEventListener("click", (event) => {
  if (event.target === avatarDialog) avatarDialog.close();
});

const query = document.querySelector("#query");
const queryTypeOverride = document.querySelector("#query-type-override");
const resetQueryType = document.querySelector("#reset-query-type");
let toolsQueryKind = null;
const updateTools = () => {
  const kind = document.querySelector("input[name=query_type]:checked")?.value;
  if (!kind) return;
  const kindChanged = kind !== toolsQueryKind;
  document.querySelectorAll(".tool").forEach((element) => {
    const allowed = (element.dataset.types || "").split(",").includes(kind);
    const checkbox = element.querySelector("input[type=checkbox]");
    element.classList.toggle("hidden", !allowed);
    if (checkbox) {
      checkbox.disabled = !allowed;
      if (!allowed) checkbox.checked = false;
      else if (kindChanged) checkbox.checked = element.dataset.defaultSelected !== "false";
    }
  });
  const collectionHelp = document.querySelector("#collection-help");
  const nameSearchHelp = document.querySelector("#name-search-help");
  document.querySelector("#username-profile-help")?.classList.toggle("hidden", kind !== "username");
  const nameSearch = kind === "name";
  collectionHelp?.classList.toggle("hidden", nameSearch);
  nameSearchHelp?.classList.toggle("hidden", !nameSearch);
  const automaticPivotOption = document.querySelector("#auto-pivot-option");
  const automaticPivotHelp = document.querySelector("#auto-pivot-help");
  const showAutomaticPivots = kind === "email" || kind === "username";
  automaticPivotOption?.classList.toggle("hidden", !showAutomaticPivots);
  automaticPivotHelp?.classList.toggle("hidden", !showAutomaticPivots);
  const automaticPivotCheckbox = automaticPivotOption?.querySelector("input[type=checkbox]");
  if (automaticPivotCheckbox && kindChanged) automaticPivotCheckbox.checked = showAutomaticPivots;
  const extendedUsernameOption = document.querySelector("#extended-username-option");
  const extendedUsernameHelp = document.querySelector("#extended-username-help");
  const showExtendedUsername = kind === "username";
  extendedUsernameOption?.classList.toggle("hidden", !showExtendedUsername);
  extendedUsernameHelp?.classList.toggle("hidden", !showExtendedUsername);
  const extendedUsernameCheckbox = extendedUsernameOption?.querySelector("input[type=checkbox]");
  if (extendedUsernameCheckbox && !showExtendedUsername) extendedUsernameCheckbox.checked = false;
  toolsQueryKind = kind;
};

document.querySelectorAll("input[name=query_type]").forEach((element) => {
  element.addEventListener("change", () => {
    if (queryTypeOverride) queryTypeOverride.value = "on";
    resetQueryType?.classList.remove("hidden");
    updateTools();
  });
});
resetQueryType?.addEventListener("click", () => {
  if (queryTypeOverride) queryTypeOverride.value = "";
  resetQueryType.classList.add("hidden");
  query?.dispatchEvent(new Event("input"));
});
updateTools();

let detectTimer;
query?.addEventListener("input", () => {
  clearTimeout(detectTimer);
  detectTimer = setTimeout(async () => {
    const output = document.querySelector("#detected");
    if (!query.value || !output) return;
    try {
      const response = await fetch(`/api/detect?q=${encodeURIComponent(query.value)}`);
      if (!response.ok) throw new Error("detection request failed");
      const data = await response.json();
      if (data.query_type && queryTypeOverride?.value !== "on") {
        const detectedRadio = document.querySelector(
          `input[name=query_type][value="${CSS.escape(data.query_type)}"]`,
        );
        if (detectedRadio && !detectedRadio.checked) {
          detectedRadio.checked = true;
          updateTools();
        }
        output.textContent = `Detected ${data.query_type}; using the recommended collectors.`;
      } else if (queryTypeOverride?.value === "on") {
        const selected = document.querySelector("input[name=query_type]:checked")?.value;
        output.textContent = `Using your selected type: ${selected}.`;
      } else {
        output.textContent = "Could not safely detect a type.";
      }
    } catch {
      output.textContent = "Type detection unavailable; select the query type manually.";
    }
  }, 250);
});

const renderGroups = () => {
  document.querySelectorAll("#findings .group-row").forEach((row) => row.remove());
  if (!document.querySelector("#group-platform")?.checked) return;
  let previous = "";
  document.querySelectorAll("#findings .finding-row:not([hidden])").forEach((row) => {
    const platform = row.dataset.platform || "Uncategorised";
    if (platform !== previous) {
      const group = document.createElement("tr");
      const cell = document.createElement("td");
      group.className = "group-row";
      cell.colSpan = 5;
      cell.textContent = platform;
      group.append(cell);
      row.before(group);
      previous = platform;
    }
  });
};

const filterRows = () => {
  const text = (document.querySelector("#finding-filter")?.value || "").toLowerCase();
  const status = document.querySelector("#status-filter")?.value || "";
  document.querySelectorAll("#findings .finding-row").forEach((row) => {
    const rowStatus = row.dataset.status || "";
    const statusMismatch = status === "positive"
      ? !["found", "likely"].includes(rowStatus)
      : Boolean(status && rowStatus !== status);
    const textMismatch = text && !(row.dataset.search || "").toLowerCase().includes(text);
    row.hidden = Boolean(statusMismatch || textMismatch);
  });
  renderGroups();
};

document.querySelector("#finding-filter")?.addEventListener("input", filterRows);
document.querySelector("#status-filter")?.addEventListener("change", filterRows);
document.querySelector("#group-platform")?.addEventListener("change", renderGroups);

document.querySelectorAll("#findings th[data-sort]").forEach((header) => {
  let direction = 1;
  const sort = () => {
    const key = header.dataset.sort;
    const body = document.querySelector("#findings tbody");
    const rows = [...document.querySelectorAll("#findings .finding-row")];
    if (!body || !key) return;
    rows.sort((left, right) => {
      const result = (left.dataset[key] || "").localeCompare(right.dataset[key] || "");
      return result * direction;
    });
    direction *= -1;
    rows.forEach((row) => body.append(row));
    renderGroups();
  };
  header.addEventListener("click", sort);
  header.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      sort();
    }
  });
});
filterRows();

const filterUnifiedAccounts = () => {
  const scope = document.querySelector("#unified-account-view");
  if (!scope) return;
  const search = (scope.querySelector("#account-view-search")?.value || "").trim().toLowerCase();
  const group = scope.querySelector("#account-view-group")?.value || "";
  const overlap = scope.querySelector("#account-view-overlap")?.value || "";
  let visibleCount = 0;
  scope.querySelectorAll("[data-unified-account]").forEach((card) => {
    const sourceCount = Number.parseInt(card.dataset.sourceCount || "0", 10);
    const textMismatch = Boolean(search && !(card.dataset.search || "").toLowerCase().includes(search));
    const groupMismatch = Boolean(group && card.dataset.group !== group);
    const overlapMismatch = overlap === "multi" ? sourceCount < 2 : overlap === "single" ? sourceCount !== 1 : false;
    card.hidden = textMismatch || groupMismatch || overlapMismatch;
    if (!card.hidden) visibleCount += 1;
  });
  const counter = scope.querySelector("[data-account-visible-count]");
  if (counter) counter.textContent = String(visibleCount);
  scope.querySelectorAll("[data-unified-group]").forEach((section) => {
    const cards = [...section.querySelectorAll("[data-unified-account]")];
    section.hidden = Boolean(cards.length && cards.every((card) => card.hidden));
  });
};

const bindUnifiedAccountView = (scope = document) => {
  const view = scope.querySelector?.("#unified-account-view") || (scope.id === "unified-account-view" ? scope : null);
  if (!view || view.dataset.bound === "true") return;
  view.dataset.bound = "true";
  view.querySelector("#account-view-search")?.addEventListener("input", filterUnifiedAccounts);
  view.querySelector("#account-view-group")?.addEventListener("change", filterUnifiedAccounts);
  view.querySelector("#account-view-overlap")?.addEventListener("change", filterUnifiedAccounts);
  bindFindingInteractions(view);
  filterUnifiedAccounts();
};

bindUnifiedAccountView();

let liveFindingsRefresh = null;
let liveFindingsRefreshQueued = false;
const refreshLiveFindings = async () => {
  if (liveFindingsRefresh) {
    liveFindingsRefreshQueued = true;
    return liveFindingsRefresh;
  }
  liveFindingsRefresh = (async () => {
    const response = await fetch(window.location.pathname, {
      headers: { Accept: "text/html", "X-OSINT-Live-Refresh": "findings" },
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`live findings refresh failed (${response.status})`);
    const incoming = new DOMParser().parseFromString(await response.text(), "text/html");
    const currentBody = document.querySelector("#findings tbody");
    const incomingBody = incoming.querySelector("#findings tbody");
    if (!currentBody || !incomingBody) throw new Error("live findings table was not available");
    currentBody.replaceChildren(
      ...[...incomingBody.childNodes].map((node) => document.importNode(node, true)),
    );
    const currentMetrics = document.querySelector(".case-metrics");
    const incomingMetrics = incoming.querySelector(".case-metrics");
    if (currentMetrics && incomingMetrics) {
      currentMetrics.replaceWith(document.importNode(incomingMetrics, true));
    }
    const currentAccountView = document.querySelector("#unified-account-view");
    const incomingAccountView = incoming.querySelector("#unified-account-view");
    if (currentAccountView && incomingAccountView) {
      const replacement = document.importNode(incomingAccountView, true);
      currentAccountView.replaceWith(replacement);
      bindUnifiedAccountView(replacement);
    }
    bindFindingInteractions(currentBody);
    filterRows();
  })();
  try {
    await liveFindingsRefresh;
  } finally {
    liveFindingsRefresh = null;
    if (liveFindingsRefreshQueued) {
      liveFindingsRefreshQueued = false;
      window.setTimeout(() => refreshLiveFindings().catch(() => {}), 50);
    }
  }
};

const activityPanel = document.querySelector("#overview.activity-panel");
const terminalRunStatuses = ["completed", "failed", "timed_out", "cancelled"];

const updateActivitySummary = () => {
  if (!activityPanel) return;
  const chips = [...document.querySelectorAll("#activity-tools .activity-chip")];
  const running = chips.filter((chip) => chip.dataset.status === "running");
  const queued = chips.filter((chip) => chip.dataset.status === "queued");
  const finished = chips.filter((chip) => terminalRunStatuses.includes(chip.dataset.status));
  const title = document.querySelector("#activity-title");
  const message = document.querySelector("#activity-message");
  const progress = document.querySelector("#activity-progress-bar");
  const overall = activityPanel.dataset.overallStatus || "queued";
  const phaseMessage = activityPanel.dataset.phaseMessage;
  const phaseName = activityPanel.dataset.phaseName;

  if (progress) progress.style.width = `${chips.length ? Math.round((finished.length / chips.length) * 100) : 100}%`;
  if (!title || !message) return;
  if (phaseMessage && !running.length) {
    const labels = {
      username_pivots: "Following identity pivots",
      profile_verification: "Verifying profile claims",
      historical_checks: "Checking archived history",
    };
    title.textContent = labels[phaseName] || "Running automatic follow-up";
    message.textContent = phaseMessage;
  } else if (running.length) {
    title.textContent = `Running ${running.length} collector${running.length === 1 ? "" : "s"}`;
    message.textContent = running.map((chip) => chip.dataset.name).join(", ");
  } else if (queued.length) {
    title.textContent = "Preparing collectors";
    message.textContent = `Waiting to start ${queued.map((chip) => chip.dataset.name).join(", ")}`;
  } else if (overall === "completed") {
    title.textContent = "Investigation complete";
    message.textContent = "Collection and automatic follow-up have finished. Review the findings below.";
  } else if (overall === "cancelled") {
    title.textContent = "Investigation cancelled";
    message.textContent = "Active collection was stopped by the operator.";
  } else if (overall === "running") {
    title.textContent = "Finishing analysis";
    message.textContent = "Verifying profiles and checking historical coverage.";
  }
};

const activityElapsedSeconds = (chip) => {
  const value = chip.dataset.startedAt;
  if (!value) return null;
  const normalised = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`;
  const started = Date.parse(normalised);
  if (Number.isNaN(started)) return null;
  return Math.max(0, Math.floor((Date.now() - started) / 1000));
};

const renderActivityChipStatus = (chip) => {
  const state = chip.querySelector(".activity-chip-status");
  if (!state) return;
  const status = (chip.dataset.status || "unknown").replaceAll("_", " ");
  const parts = [status];
  if (chip.dataset.sessionMode === "managed_browser") parts.push("sock session");
  if (chip.dataset.status === "running") {
    if (chip.dataset.tool === "social_analyzer") parts.push("checking the top 50 sites");
    const elapsed = activityElapsedSeconds(chip);
    const timeout = Number.parseInt(chip.dataset.timeoutSeconds || "", 10);
    if (elapsed !== null) parts.push(Number.isFinite(timeout) ? `${elapsed}s / ${timeout}s` : `${elapsed}s`);
  }
  state.textContent = parts.join(" · ");
};

const updateActivityChip = (data) => {
  const tools = document.querySelector("#activity-tools");
  if (!tools) return;
  const selector = data.run_id
    ? `.activity-chip[data-run="${CSS.escape(data.run_id)}"]`
    : `.activity-chip[data-tool="${CSS.escape(data.tool)}"]`;
  let chip = tools.querySelector(selector);
  if (!chip) {
    chip = document.createElement("span");
    chip.className = "activity-chip";
    chip.dataset.run = data.run_id || "";
    chip.dataset.tool = data.tool;
    chip.dataset.name = data.display_name || data.tool;
    const dot = document.createElement("i");
    dot.setAttribute("aria-hidden", "true");
    const name = document.createElement("strong");
    name.textContent = data.display_name || data.tool;
    const state = document.createElement("small");
    state.className = "activity-chip-status";
    chip.append(dot, name, state);
    tools.append(chip);
  }
  chip.dataset.status = data.status;
  chip.className = `activity-chip ${data.status}`;
  if (data.session_mode) chip.dataset.sessionMode = data.session_mode;
  if (data.started_at) chip.dataset.startedAt = data.started_at;
  if (data.timeout_seconds) chip.dataset.timeoutSeconds = String(data.timeout_seconds);
  if (data.status !== "running") delete chip.dataset.startedAt;
  renderActivityChipStatus(chip);
};

document.querySelectorAll("#activity-tools .activity-chip").forEach(renderActivityChipStatus);
window.setInterval(() => {
  document.querySelectorAll('#activity-tools .activity-chip[data-status="running"]').forEach(renderActivityChipStatus);
}, 1000);
updateActivitySummary();

const live = document.querySelector("#live-config");
if (live) {
  const config = JSON.parse(live.textContent);
  const source = new EventSource(config.events);
  const applyToolState = (data) => {
    const selector = data.run_id
      ? `.run[data-run="${CSS.escape(data.run_id)}"] .run-status`
      : `.run[data-tool="${CSS.escape(data.tool)}"] .run-status`;
    const row = document.querySelector(selector);
    if (row) {
      row.textContent = data.status;
      row.className = `run-status badge ${data.status}`;
    }
    updateActivityChip(data);
  };

  source.addEventListener("snapshot", (event) => {
    const data = JSON.parse(event.data);
    (data.runs || []).forEach(applyToolState);
    document.querySelectorAll("#overall, #activity-overall").forEach((badge) => {
      badge.textContent = data.status;
      badge.className = `badge large ${data.status}`;
    });
    if (activityPanel) activityPanel.dataset.overallStatus = data.status;
    updateActivitySummary();
  });

  source.addEventListener("tool", (event) => {
    const data = JSON.parse(event.data);
    applyToolState(data);
    updateActivitySummary();
    if (terminalRunStatuses.includes(data.status)) {
      refreshLiveFindings().catch(() => {
        const message = document.querySelector("#activity-message");
        if (message) message.textContent = "Collector finished, but the live findings view could not refresh. Reload the page to see its results.";
      });
    }
  });
  source.addEventListener("phase", (event) => {
    const data = JSON.parse(event.data);
    if (activityPanel) {
      activityPanel.dataset.phaseName = data.phase;
      activityPanel.dataset.phaseMessage = data.message;
    }
    updateActivitySummary();
  });
  source.addEventListener("verification", () => {
    refreshLiveFindings().catch(() => {});
  });
  source.addEventListener("status", (event) => {
    const data = JSON.parse(event.data);
    document.querySelectorAll("#overall, #activity-overall").forEach((badge) => {
      badge.textContent = data.status;
      badge.className = `badge large ${data.status}`;
    });
    if (activityPanel) activityPanel.dataset.overallStatus = data.status;
    updateActivitySummary();
  });
  source.addEventListener("complete", (event) => {
    const data = JSON.parse(event.data);
    if (activityPanel) {
      activityPanel.dataset.overallStatus = data.status;
      delete activityPanel.dataset.phaseName;
      delete activityPanel.dataset.phaseMessage;
    }
    updateActivitySummary();
    source.close();
    location.reload();
  });
  source.onerror = () => {
    const message = document.querySelector("#activity-message");
    if (message) message.textContent = "Live status connection interrupted; reconnecting automatically…";
  };
}

// Pages that show background progress declare a refresh interval in seconds.
const autoReload = document.querySelector("[data-auto-reload]");
if (autoReload) {
  const seconds = Math.max(5, Number(autoReload.dataset.autoReload) || 15);
  window.setTimeout(() => window.location.reload(), seconds * 1000);
}
