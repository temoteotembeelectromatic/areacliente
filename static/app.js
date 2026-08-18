document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-loading-form]").forEach((form) => {
    form.addEventListener("submit", () => {
      const submitButton = form.querySelector("button[type='submit']");
      if (!submitButton) return;

      submitButton.disabled = true;
      submitButton.textContent = form.dataset.loadingText || "A carregar...";
      form.setAttribute("aria-busy", "true");
    });
  });
});
