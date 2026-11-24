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
  if (pre) pre.scrollTop = pre.scrollHeight;  // the end of a log says how it went
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

// Jobs whose status changed since you last saw them carry a "new" tag until they have been on
// screen for two seconds. A first visit marks nothing: everything is new then. Per browser.
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
  const mark = (scope) => {
    const seen = load();
    scope.querySelectorAll("tr[data-job]").forEach((row) => {
      if (first) { seen[row.dataset.job] = row.dataset.status; return; }
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

// A panel that refreshes itself says when it last did, and when it can't (the console stopped).
(() => {
  document.body.addEventListener("htmx:sendError", (event) => {
    const stamp = event.target.querySelector?.("[data-updated]");
    if (!stamp || stamp.classList.contains("stale")) return;
    stamp.classList.add("stale");
    stamp.textContent = stamp.textContent.replace("Updated", "Not updated since") + ": can't reach the console";
  });
})();

// Copy buttons: the value to the clipboard, and "Copied" for two seconds.
(() => {
  const fallback = (text) => {
    const area = Object.assign(document.createElement("textarea"), { value: text });
    area.setAttribute("readonly", ""); area.style.position = "fixed"; area.style.opacity = "0";
    document.body.append(area); area.select();
    try { document.execCommand("copy"); } finally { area.remove(); }
  };
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-copy]");
    if (!button) return;
    const text = button.dataset.copy;
    try { await navigator.clipboard.writeText(text); } catch (e) { fallback(text); }
    button.textContent = "Copied";
    setTimeout(() => { button.textContent = "Copy"; }, 2000);
  });
})();

// An estimate that depends on a field (cases in a trial) follows it as you type.
(() => {
  document.querySelectorAll("p.estimate[data-field]").forEach((p) => {
    const input = document.getElementById(p.dataset.field);
    if (!input) return;
    const update = () => {
      const n = Math.max(0, parseInt(input.value, 10) || 0);
      const calls = Math.round(n * parseFloat(p.dataset.per));
      p.textContent = p.dataset.text.replace("{n}", n.toLocaleString("en")).replace("{calls}", calls.toLocaleString("en"));
    };
    input.addEventListener("input", update);
  });
})();

// A running job's page: when it ends, the tab title says so, and (if asked) a notification.
(() => {
  const head = () => document.getElementById("job-head");
  const first = head();
  if (!first || first.dataset.status !== "running") return;
  const key = `cfr-notify-${location.pathname}`;
  const wanted = () => { try { return sessionStorage.getItem(key) === "1"; } catch (e) { return false; } };
  const offer = () => {
    const box = head()?.querySelector(".notify");
    if (!box || !("Notification" in window) || Notification.permission === "denied") return;
    box.hidden = false;
    const button = box.querySelector("[data-notify]");
    const note = box.querySelector("[data-notify-note]");
    if (wanted()) { button.hidden = true; note.textContent = "You'll get a notification when it ends."; return; }
    button.addEventListener("click", async () => {
      if ((await Notification.requestPermission()) !== "granted") { note.textContent = "Notifications are blocked for this site."; return; }
      try { sessionStorage.setItem(key, "1"); } catch (e) {}
      button.hidden = true; note.textContent = "You'll get a notification when it ends.";
    });
  };
  offer();
  document.body.addEventListener("htmx:afterSwap", () => {
    const now = head();
    if (!now || now.dataset.status === "running") { offer(); return; }
    const status = now.dataset.status;
    document.title = `${status[0].toUpperCase()}${status.slice(1)} · ${now.dataset.title} · CFR console`;
    if (wanted() && Notification.permission === "granted") new Notification(`${now.dataset.title}: ${status}`);
    try { sessionStorage.removeItem(key); } catch (e) {}
  });
})();

/* DLS Multi Select: a field that opens a panel of checkboxes. The field says how many are chosen;
   the panel closes on Escape or a click outside it. Without JavaScript it is still a working
   disclosure of checkboxes. */
(() => {
  const label = (ms) => {
    const chosen = [...ms.querySelectorAll('input[type="checkbox"]:checked')];
    const out = ms.querySelector("[data-ms-value]");
    if (!out) return;
    out.textContent = chosen.length === 0 ? (ms.dataset.none || "Any")
      : chosen.length === 1 ? chosen[0].closest("label").textContent.trim()
      : `${chosen.length} items`;
  };
  document.addEventListener("change", (event) => {
    const ms = event.target.closest("details.ms");
    if (ms) label(ms);
  });
  document.addEventListener("click", (event) => {
    document.querySelectorAll("details.ms[open]").forEach((ms) => { if (!ms.contains(event.target)) ms.open = false; });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const ms = event.target.closest && event.target.closest("details.ms[open]");
    if (ms) { ms.open = false; ms.querySelector("summary").focus(); }
  });
})();

/* DLS Split Button for uploads: the main action opens the file chooser; the chevron's menu holds the
   related actions (replace, remove). The chosen files are named under it. */
(() => {
  const names = (box) => {
    const input = box.querySelector('input[type="file"]');
    const out = box.querySelector("[data-upload-names]");
    const files = [...input.files].map((f) => f.name);
    out.textContent = files.length ? files.join(", ") : (input.multiple ? "No files chosen." : "No file chosen.");
    out.classList.toggle("chosen", files.length > 0);
  };
  document.addEventListener("click", (event) => {
    const box = event.target.closest("[data-upload]");
    if (!box) return;
    const input = box.querySelector('input[type="file"]');
    if (event.target.closest("[data-upload-choose]")) input.click();
    else if (event.target.closest("[data-upload-clear]")) { input.value = ""; names(box); }
    else return;
    const more = box.querySelector("details.split-more");
    if (more) more.open = false;
  });
  document.addEventListener("change", (event) => {
    const box = event.target.closest && event.target.closest("[data-upload]");
    if (box && event.target.type === "file") names(box);
  });
  document.addEventListener("click", (event) => {
    document.querySelectorAll("details.split-more[open]").forEach((d) => { if (!d.contains(event.target)) d.open = false; });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const d = event.target.closest && event.target.closest("details.split-more[open]");
    if (d) { d.open = false; d.querySelector("summary").focus(); }
  });
})();

/* DLS Stepper: minus and plus step the field within its minimum and maximum; each is marked
   unavailable at its end. The field still takes a typed value. */
(() => {
  const sync = (box) => {
    const input = box.querySelector("input");
    const value = parseFloat(input.value);
    const [down, up] = box.querySelectorAll("[data-step]");
    const atMin = input.min !== "" && !Number.isNaN(value) && value <= parseFloat(input.min);
    const atMax = input.max !== "" && !Number.isNaN(value) && value >= parseFloat(input.max);
    down.setAttribute("aria-disabled", String(atMin));
    up.setAttribute("aria-disabled", String(atMax));
  };
  document.querySelectorAll("[data-stepper]").forEach(sync);
  document.body.addEventListener("htmx:afterSwap", () => document.querySelectorAll("[data-stepper]").forEach(sync));
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-stepper] [data-step]");
    if (!button || button.getAttribute("aria-disabled") === "true") return;
    const box = button.closest("[data-stepper]");
    const input = box.querySelector("input");
    if (input.value === "") input.value = input.min || "0";
    try { button.dataset.step === "1" ? input.stepUp() : input.stepDown(); } catch (e) { return; }
    const step = input.step && input.step !== "any" ? input.step : "1";
    const places = (step.split(".")[1] || "").length;
    input.value = parseFloat(input.value).toFixed(places);  // 0.35, not 0.35000000000000003
    input.dispatchEvent(new Event("input", { bubbles: true }));
    sync(box);
  });
  document.addEventListener("input", (event) => {
    const box = event.target.closest && event.target.closest("[data-stepper]");
    if (box) sync(box);
  });
})();
