document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".work").forEach((work) => {
    const tabs = [...work.querySelectorAll("[data-work-tab]")];
    const panes = [...work.querySelectorAll("[data-work-pane]")];
    if (!tabs.length) return;
    const show = (name) => {
      tabs.forEach((tab) => {
        const on = tab.dataset.workTab === name;
        tab.classList.toggle("is-on", on);
        tab.setAttribute("aria-selected", on ? "true" : "false");
      });
      panes.forEach((pane) => {
        pane.hidden = pane.dataset.workPane !== name;
      });
    };
    tabs.forEach((tab) => {
      tab.addEventListener("click", () => show(tab.dataset.workTab));
    });
  });

  document.querySelectorAll("input[type=file]").forEach((input) => {
    const note = document.createElement("span");
    note.className = "file-picked";
    input.insertAdjacentElement("afterend", note);
    input.addEventListener("change", () => {
      const files = [...input.files];
      if (!files.length) note.textContent = "";
      else if (files.length === 1) note.textContent = files[0].name;
      else note.textContent = `Selected ${files.length} files`;
    });
  });

  const list = document.getElementById("student-list");
  const count = document.getElementById("pick-count");
  const rubricCount = document.getElementById("rubric-count");
  const rubricLabel = document.getElementById("rubric-count-label");
  if (list && count) {
    const required = () => {
      if (!rubricCount) return Number(count.dataset.required || "0");
      const max = Number(rubricCount.max || "1");
      const value = Number.parseInt(rubricCount.value, 10);
      if (!Number.isFinite(value)) return 0;
      return Math.min(max, Math.max(1, value));
    };
    const refresh = () => {
      const need = required();
      const chosen = list.querySelectorAll("input[type=checkbox]:checked").length;
      count.textContent = `Selected ${chosen}. Need exactly ${need}.`;
      count.classList.toggle("match", chosen === need);
      if (rubricLabel) rubricLabel.textContent = String(need || rubricCount.value || "");
    };
    list.addEventListener("change", refresh);
    if (rubricCount) rubricCount.addEventListener("input", refresh);
    const checkFirst = document.getElementById("check-first");
    if (checkFirst) {
      checkFirst.addEventListener("click", () => {
        const need = required();
        list.querySelectorAll("input[type=checkbox]").forEach((box, index) => {
          box.checked = index < need;
        });
        refresh();
      });
    }
    refresh();
  }

  const filter = document.getElementById("student-filter");
  if (filter && list) {
    filter.addEventListener("input", () => {
      const query = filter.value.trim().toLowerCase();
      list.querySelectorAll(".check").forEach((row) => {
        row.hidden = Boolean(query) && !row.textContent.toLowerCase().includes(query);
      });
    });
  }
});
