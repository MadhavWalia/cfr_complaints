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

// A job's log, live: appended as the job writes it; the head refreshes once it ends.
(() => {
  const pre = document.querySelector("pre[data-log]");
  if (!pre || pre.dataset.finished === "true" || !("EventSource" in window)) return;
  const nearBottom = () => pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  pre.scrollTop = pre.scrollHeight;
  const source = new EventSource(pre.dataset.log);
  source.addEventListener("log", (e) => {
    const follow = nearBottom();
    pre.append(JSON.parse(e.data).text);
    if (follow) pre.scrollTop = pre.scrollHeight;
  });
  source.addEventListener("end", () => {
    source.close();
    document.body.dispatchEvent(new Event("job-ended"));
  });
})();

// Rows whose status changed since you last saw them (jobs and case runs) stay highlighted
// until they have been on screen for two seconds (the gate board's changed row). On a first
// visit only what needs attention is highlighted: running, failed, lost. Per browser.
(() => {
  const KEY = "cfr-seen";
  const load = () => { try { return JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { return {}; } };
  const save = (seen) => { try { localStorage.setItem(KEY, JSON.stringify(seen)); } catch (e) {} };
  let first = (() => { try { return !localStorage.getItem(KEY); } catch (e) { return false; } })();
  const watch = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const row = entry.target;
      clearTimeout(row._seenTimer);
      if (entry.isIntersecting) row._seenTimer = setTimeout(() => {
        const seen = load(); seen[row.dataset.job] = row.dataset.status; save(seen);
      }, 2000);
    }
  });
  const NEWS = new Set(["running", "failed", "lost", "FAILED"]);
  const mark = (scope) => {
    const seen = load();
    scope.querySelectorAll("tr[data-job]").forEach((row) => {
      if (first && !NEWS.has(row.dataset.status)) { seen[row.dataset.job] = row.dataset.status; return; }  // first visit: only what needs attention
      row.classList.toggle("lit", seen[row.dataset.job] !== row.dataset.status);
      if (row.classList.contains("lit")) watch.observe(row);
    });
    if (first) { save(seen); first = false; }  // later refreshes compare against it
  };
  mark(document);
  document.body.addEventListener("htmx:afterSwap", () => mark(document));
})();

// Commands: a link to #<command id> opens its form; a form sends once (no double-started jobs).
(() => {
  const open = () => {
    const target = location.hash && document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (target && target.tagName === "DETAILS") { target.open = true; target.scrollIntoView(); }
  };
  open();
  addEventListener("hashchange", open);
  const problem = document.querySelector(".form-error");  // a form sent back: show what to fix
  if (problem) { problem.tabIndex = -1; problem.closest("details").scrollIntoView(); problem.focus({ preventScroll: true }); }
  document.addEventListener("submit", (event) => {
    const button = event.target.querySelector("button[type=submit]");
    if (!button) return;
    if (button.dataset.sent) { event.preventDefault(); return; }
    button.dataset.sent = "1";
    button.setAttribute("aria-disabled", "true");
  });
  addEventListener("pageshow", (event) => {  // back to a cached page: sendable again
    if (event.persisted) document.querySelectorAll("button[data-sent]").forEach((b) => { delete b.dataset.sent; b.removeAttribute("aria-disabled"); });
  });
})();
