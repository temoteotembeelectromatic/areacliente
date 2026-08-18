document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-loading-form]").forEach((form) => {
    let liveTimer;
    let liveController;

    const updateResults = () => {
      const results = document.querySelector("#equipment-results");
      if (!results) return;
      const submitButton = form.querySelector("button[type='submit']");
      if (submitButton) {
        submitButton.disabled = true;
        submitButton.textContent = form.dataset.loadingText || "A carregar...";
      }
      form.setAttribute("aria-busy", "true");
      if (liveController) liveController.abort();
      const params = new URLSearchParams(new FormData(form));
      const url = `${form.action || window.location.pathname}?${params.toString()}`;
      const controller = new AbortController();
      liveController = controller;
      fetch(url, { headers: { "X-Requested-With": "equipment-search" }, signal: controller.signal }).then((response) => response.text()).then((html) => {
        const page = new DOMParser().parseFromString(html, "text/html");
        const nextResults = page.querySelector("#equipment-results");
        if (nextResults) {
          results.replaceWith(nextResults);
          window.history.replaceState({}, "", url);
        }
      }).catch((error) => {
        if (error.name !== "AbortError") {
          const status = results.querySelector(".equipment-results-meta span");
          if (status) status.textContent = "Não foi possível actualizar.";
        }
      }).finally(() => {
        if (liveController === controller) {
          liveController = null;
          form.removeAttribute("aria-busy");
          if (submitButton) {
            submitButton.disabled = false;
            submitButton.textContent = "Procurar";
          }
        }
      });
    };

    if (form.hasAttribute("data-live-search")) {
      const search = form.querySelector("input[name='q']");
      search.addEventListener("input", () => {
        clearTimeout(liveTimer);
        liveTimer = setTimeout(updateResults, 350);
      });
      form.querySelectorAll("select").forEach((select) => select.addEventListener("change", updateResults));
    }

    form.addEventListener("submit", (event) => {
      if (form.hasAttribute("data-live-search")) {
        event.preventDefault();
        updateResults();
        return;
      }
      const submitButton = form.querySelector("button[type='submit']");
      if (!submitButton) return;

      submitButton.disabled = true;
      submitButton.textContent = form.dataset.loadingText || "A carregar...";
      form.setAttribute("aria-busy", "true");
    });
  });
});
