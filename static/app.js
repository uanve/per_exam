"use strict";

const catalogSearch = document.querySelector("#catalog-search");
if (catalogSearch) {
  for (const radio of catalogSearch.querySelectorAll('input[name="mode"]')) {
    radio.addEventListener("change", () => catalogSearch.requestSubmit());
  }
}

const result = document.querySelector("[data-clear-storage]");
let storageAvailable = true;
function readStorage(key) {
  try {
    const saved = JSON.parse(localStorage.getItem(key) || "{}");
    return saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {};
  } catch (error) {
    storageAvailable = false;
    console.warn("No se pudieron recuperar las respuestas guardadas.", error);
    return {};
  }
}
function clearStorage(key) {
  try {
    localStorage.removeItem(key);
  } catch (error) {
    console.warn("No se pudieron borrar las respuestas guardadas.", error);
  }
}
if (result) clearStorage(result.dataset.clearStorage);

const historyKey = "rumbo-session-history-v1";
const historyPanel = document.querySelector("#session-history");
let historyAvailable = true;
function historyError(error) {
  historyAvailable = false;
  console.warn("No se pudo acceder al historial de esta sesión.", error);
  let notice = document.querySelector("#history-error");
  if (!notice) {
    notice = document.createElement("p");
    notice.className = "notice error";
    notice.id = "history-error";
    notice.setAttribute("role", "alert");
    (result || document.querySelector("main")).append(notice);
  }
  notice.textContent = "No se pudo guardar o leer el historial de esta sesión. Puedes seguir haciendo exámenes; tu resultado actual no cambia.";
  notice.hidden = false;
}
function validHistoryEntry(entry) {
  return entry && typeof entry === "object"
    && typeof entry.id === "string" && entry.id.length > 0
    && Number.isSafeInteger(entry.examId) && entry.examId > 0
    && typeof entry.title === "string" && typeof entry.modeLabel === "string"
    && ["per", "extension"].includes(entry.mode)
    && Number.isInteger(entry.score) && Number.isInteger(entry.total)
    && entry.score >= 0 && entry.score <= entry.total && entry.total <= 45
    && typeof entry.passed === "boolean"
    && typeof entry.completedAt === "string" && Number.isFinite(Date.parse(entry.completedAt));
}
function readHistory() {
  try {
    const entries = JSON.parse(sessionStorage.getItem(historyKey) || "[]");
    if (!Array.isArray(entries) || !entries.every(validHistoryEntry)) {
      throw new Error("Formato de historial inválido.");
    }
    return entries;
  } catch (error) {
    historyError(error);
    return [];
  }
}
function saveResultHistory() {
  if (!result || !result.dataset.sessionResult) return;
  try {
    const entry = JSON.parse(result.dataset.sessionResult);
    if (!validHistoryEntry(entry)) throw new Error("Resultado de sesión inválido.");
    const entries = readHistory();
    if (historyAvailable && !entries.some((previous) => previous.id === entry.id)) {
      sessionStorage.setItem(historyKey, JSON.stringify([entry, ...entries]));
    }
  } catch (error) {
    historyError(error);
  }
}
function renderHistory() {
  if (!historyPanel) return;
  historyPanel.hidden = false;
  const entries = readHistory();
  const rows = document.querySelector("#session-history-rows");
  rows.replaceChildren();
  document.querySelector("#history-empty").hidden = entries.length > 0 || !historyAvailable;
  document.querySelector("#history-table").hidden = entries.length === 0;
  for (const entry of entries) {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    const link = document.createElement("a");
    link.href = `/exams/${entry.examId}?mode=${entry.mode}`;
    link.textContent = entry.title;
    nameCell.append(link);
    row.append(nameCell);
    for (const value of [
      entry.modeLabel, new Date(entry.completedAt).toLocaleString("es-ES"),
      `${entry.score} / ${entry.total}`, entry.passed ? "Aprobado" : "Suspendido",
    ]) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    row.lastElementChild.className = entry.passed ? "history-passed" : "history-failed";
    rows.append(row);
  }
  for (const card of document.querySelectorAll("[data-history-exam]")) {
    const latest = entries.find((entry) => entry.examId === Number(card.dataset.historyExam)
      && entry.mode === card.dataset.historyMode);
    const status = card.querySelector("[data-attempt-status]");
    status.hidden = !latest;
    status.textContent = latest ? `Último intento: ${latest.passed ? "Aprobado" : "Suspendido"} · ${latest.score}/${latest.total}` : "";
    status.classList.toggle("history-passed", Boolean(latest?.passed));
    status.classList.toggle("history-failed", Boolean(latest && !latest.passed));
  }
}
if (result) saveResultHistory();
if (historyPanel) {
  renderHistory();
  document.querySelector("#clear-session-history").addEventListener("click", () => {
    try {
      sessionStorage.removeItem(historyKey);
      historyAvailable = true;
      document.querySelector("#history-error").hidden = true;
      renderHistory();
    } catch (error) {
      historyError(error);
    }
  });
}
window.addEventListener("pageshow", () => {
  if (historyPanel) renderHistory();
});

const examForm = document.querySelector("#exam-form");
if (examForm) {
  const key = examForm.dataset.storageKey;
  const radios = [...examForm.querySelectorAll('input[type="radio"]')];
  const cards = [...examForm.querySelectorAll("[data-number]")];
  const saved = readStorage(key);
  let submitted = false;
  let storageWarningShown = false;
  for (const radio of radios) {
    radio.checked = saved[radio.name] === radio.value;
  }
  function warnStorage() {
    if (storageWarningShown) return;
    storageWarningShown = true;
    const warning = document.createElement("p");
    warning.className = "notice error";
    warning.textContent = "Este navegador no permite guardar las respuestas. Puedes hacer y corregir el examen, pero las selecciones se perderán al recargar.";
    examForm.prepend(warning);
  }
  function update(save) {
    const answers = {};
    for (const radio of radios) {
      if (radio.checked) answers[radio.name] = radio.value;
    }
    const count = Object.keys(answers).length;
    document.querySelector("#answered-count").textContent = count;
    document.querySelector("#exam-progress").value = count;
    const blank = cards.length - count;
    document.querySelector("#finish-summary").textContent = `${count} respondidas y ${blank} en blanco. Las preguntas en blanco cuentan como errores.`;
    for (const card of cards) {
      const answered = Boolean(answers[`q${card.dataset.number}`]);
      const nav = document.querySelector(`[data-nav="${card.dataset.number}"]`);
      nav.classList.toggle("answered", answered);
      nav.setAttribute("aria-label", `Pregunta ${card.dataset.number}${answered ? ", respondida" : ", en blanco"}`);
      card.querySelector("[data-clear]").hidden = !answered;
    }
    if (save && storageAvailable) {
      try {
        localStorage.setItem(key, JSON.stringify(answers));
      } catch (error) {
        storageAvailable = false;
        console.warn("No se pudieron guardar las respuestas.", error);
      }
    }
    if (!storageAvailable) warnStorage();
  }
  examForm.addEventListener("change", () => update(true));
  for (const button of examForm.querySelectorAll("[data-clear]")) {
    button.addEventListener("click", () => {
      for (const radio of radios) {
        if (radio.name === `q${button.dataset.clear}`) radio.checked = false;
      }
      update(true);
    });
  }
  examForm.addEventListener("submit", (event) => {
    const blank = cards.length - radios.filter((radio) => radio.checked).length;
    if (blank && !window.confirm(`Quedan ${blank} preguntas en blanco y contarán como errores. ¿Quieres corregir?`)) {
      event.preventDefault();
      return;
    }
    submitted = true;
  });
  window.addEventListener("beforeunload", (event) => {
    if (!submitted && radios.some((radio) => radio.checked)) event.preventDefault();
  });
  update(false);
}
