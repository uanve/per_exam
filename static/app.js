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
