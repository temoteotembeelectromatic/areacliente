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

  const clientSearch = document.querySelector("#client-search");
  const clientSuggestions = document.querySelector("#client-suggestions");
  const selectedClients = document.querySelector("#selected-client-numbers");
  const clientNumbers = document.querySelector("#client-numbers");
  const createUserForm = document.querySelector("#user-create-form");
  if (!clientSearch || !clientSuggestions || !selectedClients || !clientNumbers || !createUserForm) return;

  const chosen = new Map();
  let searchTimer;

  const syncChosen = () => {
    clientNumbers.value = Array.from(chosen.keys()).join(",");
    selectedClients.replaceChildren();
    chosen.forEach((name, number) => {
      const chip = document.createElement("span");
      chip.className = "selected-client-chip";
      chip.textContent = `${name} (${number})`;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.setAttribute("aria-label", `Remover ${name}`);
      remove.textContent = "×";
      remove.addEventListener("click", () => {
        chosen.delete(number);
        syncChosen();
      });
      chip.append(remove);
      selectedClients.append(chip);
    });
  };

  const showSuggestions = (items) => {
    clientSuggestions.replaceChildren();
    items.forEach((item) => {
      const option = document.createElement("button");
      option.type = "button";
      option.className = "client-suggestion";
      const name = document.createElement("strong");
      name.textContent = item.name;
      const number = document.createElement("small");
      number.textContent = item.number;
      option.append(name, number);
      option.addEventListener("click", () => {
        chosen.set(String(item.number), item.name);
        clientSearch.value = "";
        clientSuggestions.replaceChildren();
        clientSearch.setAttribute("aria-expanded", "false");
        syncChosen();
      });
      clientSuggestions.append(option);
    });
    clientSearch.setAttribute("aria-expanded", String(items.length > 0));
  };

  clientSearch.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    const query = clientSearch.value.trim();
    if (query.length < 2) {
      showSuggestions([]);
      return;
    }
    searchTimer = window.setTimeout(async () => {
      try {
        const response = await fetch(`/api/clientes?q=${encodeURIComponent(query)}`, {
          headers: { Accept: "application/json" },
        });
        if (response.ok) showSuggestions(await response.json());
      } catch (error) {
        showSuggestions([]);
      }
    }, 220);
  });

  createUserForm.addEventListener("submit", (event) => {
    if (!chosen.size) {
      event.preventDefault();
      clientSearch.focus();
    }
  });
});
