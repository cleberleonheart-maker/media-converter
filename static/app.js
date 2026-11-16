const ACTIVE_STATUSES = new Set(["queued", "downloading", "converting"]);
const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);
const AUDIO_FORMATS = new Set(["mp3", "m4a", "wav"]);
const POLL_INTERVAL_MS = 1200;
const POLL_MAX_BACKOFF_MS = 30000;
const INITIAL_LOAD_ATTEMPTS = 3;
const INITIAL_RETRY_BASE_MS = 600;

const state = {
  source: "upload",
  jobs: new Map(),
  cards: new Map(),
  polling: null,
  pollingActive: false,
  pollingInFlight: false,
  pollFailures: 0,
  initialLoadComplete: false,
  revision: 0,
  submitting: false,
  cancellingJobs: new Set(),
  removingJobs: new Set(),
  auth: { checked: false, authenticated: false, config: null },
};

const elements = {
  tabs: [...document.querySelectorAll("[data-source-tab]")],
  uploadPanel: document.querySelector("#panel-upload"),
  urlPanel: document.querySelector("#panel-url"),
  fileInput: document.querySelector("#file-input"),
  fileLabel: document.querySelector("#file-label"),
  dropZone: document.querySelector("#drop-zone"),
  urlInput: document.querySelector("#url-input"),
  format: document.querySelector("#format-select"),
  quality: document.querySelector("#quality-select"),
  convertButton: document.querySelector("#convert-button"),
  formError: document.querySelector("#form-error"),
  loadError: document.querySelector("#load-error"),
  jobsList: document.querySelector("#jobs-list"),
  emptyState: document.querySelector("#empty-state"),
  jobCount: document.querySelector("#job-count"),
  jobTemplate: document.querySelector("#job-template"),
  loginScreen: document.querySelector("#login-screen"),
  loginCopy: document.querySelector("#login-copy"),
  loginError: document.querySelector("#login-error"),
  googleButton: document.querySelector("#google-button"),
  logoutButton: document.querySelector("#logout-button"),
  installButton: document.querySelector("#install-button"),
};

const statusLabels = {
  queued: "Na fila",
  downloading: "Baixando",
  converting: "Convertendo",
  completed: "Pronto",
  failed: "Erro",
  cancelled: "Cancelado",
};

const qualityLabels = {
  original: "Sem redimensionamento",
  high: "Alta",
  medium: "Média",
  low: "Baixa",
};

function setSource(source) {
  state.source = source;
  for (const tab of elements.tabs) {
    const active = tab.dataset.sourceTab === source;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
  }
  elements.uploadPanel.classList.toggle("is-hidden", source !== "upload");
  elements.urlPanel.classList.toggle("is-hidden", source !== "url");
  elements.urlPanel.hidden = source !== "url";
  clearError();
}

function clearError() {
  elements.formError.hidden = true;
  elements.formError.textContent = "";
}

function showError(message) {
  elements.formError.textContent = message;
  elements.formError.hidden = false;
}

function showLoadError(message) {
  elements.loadError.textContent = message;
  elements.loadError.hidden = false;
}

function clearLoadError() {
  elements.loadError.hidden = true;
  elements.loadError.textContent = "";
}

function setBusy(busy) {
  elements.convertButton.disabled = busy;
  elements.convertButton.querySelector("span").textContent = busy ? "Enviando…" : "Converter agora";
}

function delay(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function normalizeApiDetail(detail) {
  if (typeof detail === "string") return detail.trim();
  if (Array.isArray(detail)) {
    return detail.map(normalizeApiDetail).filter(Boolean).join("; ");
  }
  if (detail && typeof detail === "object") {
    const message = ["msg", "message", "detail"].find((key) => {
      return typeof detail[key] === "string" && detail[key].trim();
    });
    if (message) {
      const location = Array.isArray(detail.loc)
        ? detail.loc.filter((part) => part !== null && part !== undefined).join(".")
        : "";
      return location ? `${location}: ${detail[message].trim()}` : detail[message].trim();
    }
    try {
      const serialized = JSON.stringify(detail);
      return serialized && serialized !== "{}" ? serialized : "";
    } catch {
      return "";
    }
  }
  return detail === null || detail === undefined ? "" : String(detail);
}

function showLoginScreen(config) {
  state.auth.authenticated = false;
  elements.loginScreen.hidden = false;
  elements.logoutButton.hidden = true;
  if (config && config.google_enabled) {
    elements.loginCopy.textContent = "Use sua conta Google para continuar.";
    renderGoogleButton(config.google_client_id);
  } else if (config && config.basic_enabled) {
    elements.loginCopy.textContent = "Este servidor exige usuário e senha. Recarregue a página para autenticar.";
  } else {
    elements.loginCopy.textContent = "Nenhum método de login configurado neste servidor.";
  }
}

function showApp(authenticated) {
  state.auth.authenticated = authenticated;
  elements.loginScreen.hidden = true;
  elements.logoutButton.hidden = !authenticated;
}

function showLoginError(message) {
  elements.loginError.textContent = message;
  elements.loginError.hidden = false;
}

function loadGoogleIdentityScript() {
  if (window.google && window.google.accounts && window.google.accounts.id) {
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://accounts.google.com/gsi/client";
    script.async = true;
    script.defer = true;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("Não foi possível carregar o login do Google."));
    document.head.appendChild(script);
  });
}

async function renderGoogleButton(clientId) {
  if (!clientId || elements.googleButton.dataset.rendered === clientId) return;
  try {
    await loadGoogleIdentityScript();
    if (!window.google || !window.google.accounts || !window.google.accounts.id) return;
    window.google.accounts.id.initialize({
      client_id: clientId,
      callback: handleGoogleCredential,
    });
    elements.googleButton.dataset.rendered = clientId;
    window.google.accounts.id.renderButton(elements.googleButton, {
      type: "standard",
      theme: "outline",
      size: "large",
      text: "continue_with",
      shape: "pill",
      width: 280,
    });
  } catch (error) {
    showLoginError(error.message);
  }
}

async function handleGoogleCredential(response) {
  if (!response || !response.credential) return;
  elements.loginError.hidden = true;
  try {
    await request("/api/auth/google", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ credential: response.credential }),
    });
    showApp(true);
    await loadInitialJobs();
  } catch (error) {
    showLoginError(error.message);
  }
}

async function checkAuth() {
  try {
    const config = await request("/api/auth/config");
    state.auth.config = config;
    state.auth.checked = true;
    if (config.authenticated) {
      showApp(true);
      return true;
    }
    showLoginScreen(config);
    return false;
  } catch {
    state.auth.checked = true;
    showApp(false);
    return false;
  }
}

async function logout() {
  try {
    await request("/api/auth/logout", { method: "POST" });
  } catch {
    // ignora falhas no logout; a sessao local sera limpa pelo navegador
  }
  window.location.reload();
}

async function request(url, options = {}) {
  let response;
  try {
    response = await fetch(url, options);
  } catch (error) {
    const requestError = new Error(
      error instanceof Error ? error.message : "Não foi possível conectar ao servidor.",
    );
    requestError.cause = error;
    throw requestError;
  }

  if (!response.ok) {
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    const detail = payload && typeof payload === "object" && !Array.isArray(payload)
      ? payload.detail
      : payload;
    const message = normalizeApiDetail(detail)
      || normalizeApiDetail(payload)
      || response.statusText
      || "Não foi possível concluir a solicitação.";
    const requestError = new Error(message);
    requestError.status = response.status;
    if (response.status === 401) {
      showLoginScreen(state.auth.config);
    }
    throw requestError;
  }

  if (response.status === 204) return null;

  try {
    return await response.json();
  } catch {
    const requestError = new Error("O servidor retornou uma resposta inválida.");
    requestError.status = response.status;
    throw requestError;
  }
}

function syncQualityAvailability() {
  const isAudio = AUDIO_FORMATS.has(elements.format.value);
  if (isAudio) elements.quality.value = "original";
  elements.quality.disabled = isAudio;
  elements.quality.closest(".select-field").classList.toggle("is-disabled", isAudio);
}

async function submitJob() {
  if (state.submitting) return;
  state.submitting = true;
  clearError();

  try {
    const source = state.source;
    const outputFormat = elements.format.value;
    const quality = elements.quality.value;

    if (source === "upload" && !elements.fileInput.files.length) {
      showError("Selecione um arquivo para converter.");
      elements.fileInput.focus();
      return;
    }
    if (source === "url" && !elements.urlInput.value.trim()) {
      showError("Cole um link de vídeo para converter.");
      elements.urlInput.focus();
      return;
    }

    setBusy(true);
    let job;
    if (source === "upload") {
      const data = new FormData();
      data.append("file", elements.fileInput.files[0]);
      data.append("output_format", outputFormat);
      data.append("quality", quality);
      job = await request("/api/jobs/upload", { method: "POST", body: data });
    } else {
      job = await request("/api/jobs/url", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: elements.urlInput.value.trim(), output_format: outputFormat, quality }),
      });
    }
    state.revision += 1;
    state.jobs.set(job.id, job);
    renderJob(job);
    startPolling();
  } catch (error) {
    showError(error.message);
  } finally {
    state.submitting = false;
    setBusy(false);
  }
}

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / 1024 ** index;
  return `${value.toFixed(index ? 1 : 0)} ${units[index]}`;
}

function updateJobCard(card, job) {
  const name = String(job.source_name || "Conversão");
  const nameElement = card.querySelector(".job-name");
  nameElement.textContent = name;
  nameElement.title = name;

  const quality = qualityLabels[job.quality] || job.quality;
  card.querySelector(".job-meta").textContent = `${String(job.output_format || "").toUpperCase()} · ${quality}`;

  const status = String(job.status || "");
  const statusElement = card.querySelector(".job-status");
  statusElement.textContent = statusLabels[status] || status;
  statusElement.dataset.status = status;

  const progress = Math.min(100, Math.max(0, Number(job.progress) || 0));
  const progressTrack = card.querySelector(".progress-track");
  const progressBar = card.querySelector(".progress-bar");
  progressBar.style.width = `${progress}%`;
  progressTrack.setAttribute("aria-label", `Progresso de ${name}`);
  progressTrack.setAttribute("aria-valuenow", String(progress));
  progressTrack.setAttribute(
    "aria-valuetext",
    `${statusLabels[status] || status || "Atualizando"}: ${progress}%`,
  );

  const messageElement = card.querySelector(".job-message");
  const errorMessage = job.error ? String(job.error) : "";
  messageElement.textContent = errorMessage || String(job.message || "");
  messageElement.classList.toggle("is-error", Boolean(errorMessage));

  const cancel = card.querySelector(".cancel-button");
  const remove = card.querySelector(".remove-button");
  const download = card.querySelector(".download-button");
  const active = ACTIVE_STATUSES.has(status);
  const terminal = TERMINAL_STATUSES.has(status);
  cancel.hidden = !active;
  remove.hidden = !terminal;
  download.hidden = status !== "completed";
  cancel.disabled = state.cancellingJobs.has(job.id);
  remove.disabled = state.removingJobs.has(job.id);
  remove.setAttribute("aria-label", `Remover ${name}`);
  cancel.setAttribute("aria-label", `Cancelar ${name}`);

  if (status === "completed") {
    download.href = job.download_url || `/api/jobs/${job.id}/download`;
    download.setAttribute("download", job.output_name || "");
    messageElement.textContent = `${job.message || "Conversão concluída"} · ${formatBytes(job.output_size)}`;
  } else {
    download.removeAttribute("href");
    download.removeAttribute("download");
  }
}

function renderJob(job) {
  let card = state.cards.get(job.id);
  if (!card) {
    card = elements.jobTemplate.content.firstElementChild.cloneNode(true);
    card.dataset.jobId = job.id;
    state.cards.set(job.id, card);
    elements.emptyState.hidden = true;
    elements.jobsList.prepend(card);
  }
  updateJobCard(card, job);
  updateJobCount();
}

function updateJobCount() {
  elements.jobCount.textContent = String(state.jobs.size);
  elements.emptyState.hidden = state.cards.size > 0;
}

function removeJobCard(jobId) {
  const card = state.cards.get(jobId);
  if (card) card.remove();
  state.cards.delete(jobId);
  state.jobs.delete(jobId);
  updateJobCount();
}

function applyJobsSnapshot(jobs) {
  const incomingIds = new Set(jobs.map((job) => job.id));
  for (const [jobId, card] of state.cards) {
    if (!incomingIds.has(jobId)) {
      card.remove();
      state.cards.delete(jobId);
    }
  }
  state.jobs = new Map(jobs.map((job) => [job.id, job]));
  for (const job of jobs) renderJob(job);
  const orderedCards = jobs.map((job) => state.cards.get(job.id)).filter(Boolean);
  const currentCards = [...elements.jobsList.querySelectorAll("[data-job-id]")];
  if (orderedCards.some((card, index) => currentCards[index] !== card)) {
    elements.jobsList.append(...orderedCards);
  }
  updateJobCount();
}

function schedulePolling(milliseconds) {
  if (!state.pollingActive || !state.initialLoadComplete || state.polling !== null) return;
  state.polling = window.setTimeout(() => {
    state.polling = null;
    pollJobs();
  }, milliseconds);
}

function startPolling() {
  state.pollingActive = true;
  if (!state.initialLoadComplete) return;
  if (state.polling === null && !state.pollingInFlight) schedulePolling(0);
}

async function pollJobs() {
  if (!state.pollingActive || state.pollingInFlight) return;
  state.pollingInFlight = true;
  const revision = state.revision;
  let nextDelay = POLL_INTERVAL_MS;

  try {
    const jobs = await request("/api/jobs");
    if (revision !== state.revision) {
      nextDelay = 0;
    } else {
      applyJobsSnapshot(jobs);
      clearLoadError();
      state.pollFailures = 0;
      if (!jobs.some((job) => ACTIVE_STATUSES.has(job.status))) {
        state.pollingActive = false;
      }
    }
  } catch {
    state.pollFailures += 1;
    const backoff = 2 ** Math.min(state.pollFailures - 1, 5);
    nextDelay = Math.min(POLL_INTERVAL_MS * backoff, POLL_MAX_BACKOFF_MS);
  } finally {
    state.pollingInFlight = false;
    if (state.pollingActive) schedulePolling(nextDelay);
  }
}

async function loadInitialJobs() {
  let attempts = 0;
  let lastError = null;

  while (attempts < INITIAL_LOAD_ATTEMPTS) {
    const revision = state.revision;
    try {
      const jobs = await request("/api/jobs");
      if (revision !== state.revision) {
        await delay(100);
        continue;
      }
      applyJobsSnapshot(jobs);
      state.initialLoadComplete = true;
      clearLoadError();
      if (jobs.some((job) => ACTIVE_STATUSES.has(job.status))) startPolling();
      else state.pollingActive = false;
      return;
    } catch (error) {
      lastError = error;
      attempts += 1;
      if (attempts < INITIAL_LOAD_ATTEMPTS) {
        await delay(INITIAL_RETRY_BASE_MS * 2 ** (attempts - 1));
      }
    }
  }

  state.initialLoadComplete = true;
  const detail = lastError && lastError.message ? ` ${lastError.message}` : "";
  showLoadError(`Não foi possível carregar as conversões.${detail}`);
  startPolling();
}

async function cancelJob(jobId, button) {
  if (state.cancellingJobs.has(jobId)) return;
  state.cancellingJobs.add(jobId);
  button.disabled = true;
  try {
    const job = await request(`/api/jobs/${jobId}/cancel`, { method: "POST" });
    state.revision += 1;
    state.jobs.set(job.id, job);
    renderJob(job);
  } catch (error) {
    if (error.status === 404) {
      state.revision += 1;
      removeJobCard(jobId);
    } else {
      showError(error.message);
    }
  } finally {
    state.cancellingJobs.delete(jobId);
    if (button.isConnected) button.disabled = false;
  }
}

async function removeJob(jobId, button) {
  if (state.removingJobs.has(jobId)) return;
  state.removingJobs.add(jobId);
  button.disabled = true;
  try {
    await request(`/api/jobs/${jobId}`, { method: "DELETE" });
    state.revision += 1;
    removeJobCard(jobId);
  } catch (error) {
    if (error.status === 404) {
      state.revision += 1;
      removeJobCard(jobId);
    } else {
      showError(error.message);
    }
  } finally {
    state.removingJobs.delete(jobId);
    if (button.isConnected) button.disabled = false;
  }
}

elements.tabs.forEach((tab) => {
  tab.addEventListener("click", () => setSource(tab.dataset.sourceTab));
});

elements.fileInput.addEventListener("change", () => {
  const file = elements.fileInput.files[0];
  elements.fileLabel.textContent = file ? file.name : "Arraste um vídeo ou clique para escolher";
  clearError();
});

for (const eventName of ["dragenter", "dragover"]) {
  elements.dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    elements.dropZone.classList.add("is-dragging");
  });
}

for (const eventName of ["dragleave", "drop"]) {
  elements.dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    elements.dropZone.classList.remove("is-dragging");
  });
}

elements.dropZone.addEventListener("drop", (event) => {
  const file = event.dataTransfer.files[0];
  if (!file) return;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  elements.fileInput.files = transfer.files;
  elements.fileLabel.textContent = file.name;
  clearError();
});

elements.format.addEventListener("change", syncQualityAvailability);
elements.convertButton.addEventListener("click", submitJob);
elements.logoutButton.addEventListener("click", logout);
elements.urlInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    submitJob();
  }
});

elements.jobsList.addEventListener("click", (event) => {
  const button = event.target.closest(".cancel-button, .remove-button");
  if (!button || button.disabled) return;
  const card = button.closest("[data-job-id]");
  if (!card) return;
  const jobId = card.dataset.jobId;
  if (button.classList.contains("cancel-button")) cancelJob(jobId, button);
  else removeJob(jobId, button);
});

function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return;
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}

function setupInstallPrompt() {
  let promptEvent = null;
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    promptEvent = event;
    if (elements.installButton) elements.installButton.hidden = false;
  });
  elements.installButton?.addEventListener("click", async () => {
    if (!promptEvent) return;
    promptEvent.prompt();
    await promptEvent.userChoice;
    promptEvent = null;
    elements.installButton.hidden = true;
  });
  window.addEventListener("appinstalled", () => {
    promptEvent = null;
    if (elements.installButton) elements.installButton.hidden = true;
  });
}

setSource("upload");
syncQualityAvailability();
registerServiceWorker();
setupInstallPrompt();
checkAuth().then((authenticated) => {
  if (authenticated) loadInitialJobs();
});
