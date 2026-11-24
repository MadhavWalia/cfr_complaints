/* DLS Date Picker and Date Range Picker: a calendar dialog for the console's date fields.
 *
 * Markup it works with (rendered by the templates, so the fields work without JavaScript):
 *   <div class="date-box">                                   one date
 *     <input type="text" ... pattern="\d{4}-\d{2}-\d{2}">
 *     <button type="button" data-datepicker aria-label="Choose …">…</button>
 *   </div>
 *   <div class="date-range"> <input …> – <input …> <button data-datepicker …> </div>   a range
 *
 * Behaviour follows the WAI-ARIA APG "Date Picker Dialog" pattern
 * (https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/examples/datepicker-dialog.html):
 * a modal <dialog> holding a role="grid" month with one roving tab stop; arrows move a day,
 * Home/End to the week's ends, Page Up/Down a month (with Shift, a year); Enter or Space picks
 * the day; Escape closes; focus returns to the button that opened it. A range is one calendar:
 * pick the start, then the end; the days between are marked.
 *
 * Self-contained: no other console script depends on it, and it on none. Values are ISO
 * (YYYY-MM-DD), what the server parses.
 */

const LOCALE = "en-US";
const MONTH_TITLE = new Intl.DateTimeFormat(LOCALE, { month: "long", year: "numeric" });
const FULL_DATE = new Intl.DateTimeFormat(LOCALE, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
const WEEKDAYS = Array.from({ length: 7 }, (_, i) => new Date(2026, 10, 1 + i)); // 1 Nov 2026 is a Sunday
const DAY_NAME = new Intl.DateTimeFormat(LOCALE, { weekday: "long" });
const DAY_LETTER = new Intl.DateTimeFormat(LOCALE, { weekday: "narrow" });

// --- dates: local calendar days, no times ------------------------------------------------------

const ISO = /^(\d{4})-(\d{2})-(\d{2})$/;

function parseISO(text) {
  const match = ISO.exec((text || "").trim());
  if (!match) return null;
  const [, y, m, d] = match.map(Number);
  const date = new Date(y, m - 1, d);
  return date.getMonth() === m - 1 ? date : null; // rejects 2026-02-31
}

const pad = (n) => String(n).padStart(2, "0");
const toISO = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const sameDay = (a, b) => Boolean(a && b) && toISO(a) === toISO(b);
const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);

function addMonths(d, n) {
  const last = new Date(d.getFullYear(), d.getMonth() + n + 1, 0).getDate(); // keep 31 Jan -> 28 Feb
  return new Date(d.getFullYear(), d.getMonth() + n, Math.min(d.getDate(), last));
}

function today() {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate());
}

// --- the dialog --------------------------------------------------------------------------------

class Calendar {
  constructor() {
    this.dialog = this.#build();
    this.grid = this.dialog.querySelector("tbody");
    this.title = this.dialog.querySelector(".cal-title");
    this.prompt = this.dialog.querySelector(".cal-prompt");
    this.trigger = null; // the button that opened it
    this.inputs = []; // one field, or [start, end]
    this.part = 0; // which of the inputs the next pick fills
    this.focus = today(); // the day holding the grid's tab stop

    this.dialog.addEventListener("click", (event) => this.#onClick(event));
    this.dialog.addEventListener("keydown", (event) => this.#onKeydown(event));
    this.dialog.addEventListener("close", () => this.trigger?.focus());
  }

  #build() {
    const dialog = document.createElement("dialog");
    dialog.className = "calendar";
    dialog.setAttribute("aria-labelledby", "cal-title");
    dialog.setAttribute("aria-describedby", "cal-prompt");
    const head = WEEKDAYS.map(
      (d) => `<th scope="col" abbr="${DAY_NAME.format(d)}">${DAY_LETTER.format(d)}</th>`,
    ).join("");
    // The content sits in a wrapper so a click on the dialog element itself means the backdrop.
    dialog.innerHTML = `<div class="cal-body">
      <div class="cal-head">
        <button type="button" class="cal-nav" data-move="-1" aria-label="Previous month"><svg class="icon" aria-hidden="true"><use href="#i-prev"/></svg></button>
        <h2 class="cal-title" id="cal-title" aria-live="polite"></h2>
        <button type="button" class="cal-nav" data-move="1" aria-label="Next month"><svg class="icon" aria-hidden="true"><use href="#i-next"/></svg></button>
      </div>
      <p class="cal-prompt" id="cal-prompt"></p>
      <table role="grid" aria-labelledby="cal-title">
        <thead><tr>${head}</tr></thead>
        <tbody></tbody>
      </table>
      <div class="cal-foot"><button type="button" class="btn secondary sm" data-close>Cancel</button></div>
    </div>`;
    document.body.append(dialog);
    return dialog;
  }

  open(trigger) {
    this.trigger = trigger;
    this.inputs = [...trigger.parentElement.querySelectorAll("input")];
    const values = this.inputs.map((input) => parseISO(input.value));
    // A range starts again from its start unless only the end is missing.
    this.part = this.inputs.length === 2 && values[0] && !values[1] ? 1 : 0;
    this.focus = values[this.part] || values[0] || today();
    this.#render();
    this.dialog.showModal();
    this.#place();
    this.#focusedCell()?.focus();
  }

  #isRange() {
    return this.inputs.length === 2;
  }

  #values() {
    return this.inputs.map((input) => parseISO(input.value));
  }

  #render() {
    const year = this.focus.getFullYear();
    const month = this.focus.getMonth();
    const [start, end] = this.#values();
    const now = today();
    this.title.textContent = MONTH_TITLE.format(this.focus);
    this.prompt.textContent = this.#isRange()
      ? this.part === 0 ? "Choose the start date." : "Choose the end date."
      : "Choose a date.";

    const first = new Date(year, month, 1);
    const days = new Date(year, month + 1, 0).getDate();
    const rows = [];
    let cells = Array.from({ length: first.getDay() }, () => `<td></td>`); // blanks before the 1st
    for (let day = 1; day <= days; day += 1) {
      const date = new Date(year, month, day);
      const iso = toISO(date);
      const chosen = sameDay(date, start) || sameDay(date, end);
      const between = this.#isRange() && start && end && date > start && date < end;
      const classes = [between && "between", sameDay(date, start) && end && "start", sameDay(date, end) && "end"]
        .filter(Boolean)
        .join(" ");
      cells.push(
        `<td data-date="${iso}" tabindex="${sameDay(date, this.focus) ? 0 : -1}"` +
          ` aria-label="${FULL_DATE.format(date)}"` +
          (chosen ? ` aria-selected="true"` : "") +
          (sameDay(date, now) ? ` aria-current="date"` : "") +
          (classes ? ` class="${classes}"` : "") +
          `>${day}</td>`,
      );
      if (cells.length === 7) {
        rows.push(`<tr>${cells.join("")}</tr>`);
        cells = [];
      }
    }
    if (cells.length) rows.push(`<tr>${cells.join("")}${"<td></td>".repeat(7 - cells.length)}</tr>`);
    this.grid.innerHTML = rows.join("");
  }

  #focusedCell() {
    return this.grid.querySelector(`[data-date="${toISO(this.focus)}"]`);
  }

  #moveTo(date) {
    const changedMonth = date.getMonth() !== this.focus.getMonth() || date.getFullYear() !== this.focus.getFullYear();
    this.focus = date;
    if (changedMonth) this.#render();
    else {
      this.grid.querySelector('[tabindex="0"]')?.setAttribute("tabindex", "-1");
      this.#focusedCell()?.setAttribute("tabindex", "0");
    }
    this.#focusedCell()?.focus();
  }

  #choose(date) {
    const input = this.inputs[this.part];
    input.value = toISO(date);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    if (this.#isRange() && this.part === 0) {
      const end = parseISO(this.inputs[1].value);
      if (end && end < date) this.inputs[1].value = ""; // an end before the new start no longer holds
      this.part = 1;
      this.focus = date;
      this.#render();
      this.#focusedCell()?.focus();
      return;
    }
    this.#label();
    this.dialog.close();
  }

  #label() {
    const values = this.#values().filter(Boolean).map((d) => FULL_DATE.format(d));
    const what = this.trigger.dataset.label || "a date"; // "admission dates", "a date for From"
    this.trigger.setAttribute(
      "aria-label",
      values.length ? `Change ${what}, ${values.join(" to ")}` : `Choose ${what}`,
    );
  }

  // Next to the button on wide screens; centred (by CSS) on narrow ones.
  #place() {
    if (window.matchMedia("(max-width: 567px)").matches) {
      this.dialog.style.removeProperty("top");
      this.dialog.style.removeProperty("left");
      return;
    }
    const box = this.trigger.parentElement.getBoundingClientRect();
    const { offsetWidth: width, offsetHeight: height } = this.dialog;
    const below = box.bottom + 4;
    const top = below + height <= window.innerHeight - 8 ? below : Math.max(8, box.top - height - 4);
    const left = Math.min(Math.max(8, box.right - width), window.innerWidth - width - 8);
    this.dialog.style.top = `${top}px`;
    this.dialog.style.left = `${left}px`;
  }

  #onClick(event) {
    if (event.target === this.dialog) return this.dialog.close(); // the backdrop
    if (event.target.closest("[data-close]")) return this.dialog.close();
    const nav = event.target.closest("[data-move]");
    if (nav) {
      this.focus = addMonths(this.focus, Number(nav.dataset.move));
      this.#render();
      return;
    }
    const cell = event.target.closest("td[data-date]");
    if (cell) this.#choose(parseISO(cell.dataset.date));
  }

  #onKeydown(event) {
    const cell = event.target.closest("td[data-date]");
    if (!cell) return;
    const day = this.focus;
    const moves = {
      ArrowLeft: () => addDays(day, -1),
      ArrowRight: () => addDays(day, 1),
      ArrowUp: () => addDays(day, -7),
      ArrowDown: () => addDays(day, 7),
      Home: () => addDays(day, -day.getDay()),
      End: () => addDays(day, 6 - day.getDay()),
      PageUp: () => addMonths(day, event.shiftKey ? -12 : -1),
      PageDown: () => addMonths(day, event.shiftKey ? 12 : 1),
    };
    if (moves[event.key]) {
      event.preventDefault();
      this.#moveTo(moves[event.key]());
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      this.#choose(day);
    }
  }
}

// --- wiring: one dialog, opened by any [data-datepicker] button (also those htmx swaps in) -------

let calendar = null;

document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-datepicker]");
  if (!trigger) return;
  calendar ??= new Calendar();
  calendar.open(trigger);
});
