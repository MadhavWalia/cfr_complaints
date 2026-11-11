// Theme toggle (light, dark, or the system's) and the narrow-screen menu. Nothing else.
(() => {
  const root = document.documentElement;
  const label = document.querySelector("[data-theme-label]");
  const system = () => (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const current = () => root.dataset.theme || system();
  const show = () => { if (label) label.textContent = current() === "dark" ? "Light theme" : "Dark theme"; };
  document.querySelector("[data-theme-toggle]")?.addEventListener("click", () => {
    const next = current() === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try { localStorage.setItem("cfr-theme", next); } catch (e) {}
    show();
  });
  show();

  const rail = document.getElementById("rail");
  const toggle = document.querySelector("[data-menu]");
  toggle?.addEventListener("click", () => {
    const open = rail.classList.toggle("open");
    toggle.setAttribute("aria-expanded", String(open));
  });
})();
