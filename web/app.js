/* Hospital Management System — single-page UI logic.
   All operations live on the front page; clicking a tile opens its
   form in the central workspace. No page reloads, no separate pages. */

let state = null;
let currentOp = "dashboard";
let currentView = "dashboard"; /* "dashboard" = Step 7 view, "module" = workspace view */
let dashData = null; /* latest GET /api/dashboard payload */
let chartMode = "year"; /* "year" (Jan–Dec) or "last12" (rolling 12 months) */
let sortField = "id";
let currentUser = null;

/* Step 2 (Login): which tile each role may use. The server enforces
   the same rules — this only hides what you cannot click. */
const OP_PERMS = {
  "add-patient": ["Admin", "Receptionist"],
  "view-sorted": ["Admin", "Doctor", "Receptionist"],
  "find-patient": ["Admin", "Doctor", "Receptionist"],
  "add-note": ["Admin", "Doctor"],
  "doctors": ["Admin", "Doctor", "Receptionist"],
  "book-appointment": ["Admin", "Receptionist"],
  "beds": ["Admin", "Receptionist"],
  "pharmacy": ["Admin", "Receptionist"],
  "shifts": ["Admin", "Doctor", "Receptionist"],
  "dashboard": ["Admin", "Doctor", "Receptionist"],
  "undo": ["Admin"],
  "users": ["Admin"],
  "change-password": ["Admin", "Doctor", "Receptionist"],
  /* Step 3 (Billing + Reports): every role may open them; creating or
     changing a bill is Admin/Receptionist only — the server enforces
     it, the forms below just hide those buttons for Doctors. */
  "billing": ["Admin", "Doctor", "Receptionist"],
  "reports": ["Admin", "Doctor", "Receptionist"],
  /* Step 4 (Schedule): every role may view appointments by date — a
     Doctor linked to one doctor only sees that doctor's day. */
  "schedule": ["Admin", "Doctor", "Receptionist"],
  /* Step 9 (All Appointments table + details): every role may open
     it — a Doctor linked to one doctor only sees that doctor's
     appointments. Editing is Admin/Receptionist only (the server
     enforces it; the table just hides those buttons for Doctors). */
  "all-appointments": ["Admin", "Doctor", "Receptionist"],
  /* Step 10 (Calendar): every role may open it — a Doctor linked
     to one doctor only sees that doctor's appointments. */
  "calendar": ["Admin", "Doctor", "Receptionist"],
  /* Step 11 (Doctor Availability & Scheduling): every role may open
     the availability and holidays views — a Doctor linked to one
     doctor only sees their own. Editing is Admin only (leave: Admin
     or the doctor themself); the server enforces it, the forms below
     just hide those buttons for other roles. */
  "availability": ["Admin", "Doctor", "Receptionist"],
  "holidays": ["Admin", "Doctor", "Receptionist"],
  /* Step 12 (Notifications + Reports & Analytics): every role may
     open them — a Doctor linked to one doctor only sees their own
     queue's notifications and figures. The server enforces it. */
  "notifications": ["Admin", "Doctor", "Receptionist"],
};

function canUse(op) {
  if (!currentUser) return false;
  return (OP_PERMS[op] || []).includes(currentUser.role);
}

/* Step 6 (Appointment Statuses): the seven statuses. A Doctor may
   only set Checked In / In Progress / Completed — the server enforces
   it, this only offers the right options in the dropdown. */
const APPOINTMENT_STATUSES = ["Pending", "Confirmed", "Checked In",
  "In Progress", "Completed", "Cancelled", "No Show"];
const DOCTOR_STATUSES = ["Checked In", "In Progress", "Completed"];
function statusOptions() {
  return (currentUser && currentUser.role === "Doctor")
    ? DOCTOR_STATUSES : APPOINTMENT_STATUSES;
}
function statusTagClass(status) {
  return {"Pending": "tag-pending", "Confirmed": "tag-confirmed",
    "Checked In": "tag-checkin", "In Progress": "tag-progress",
    "Completed": "tag-done", "Cancelled": "tag-cancel",
    "No Show": "tag-noshow"}[status] || "tag-nor";
}

function applyRoleVisibility() {
  /* Hide nav entries the logged-in role cannot use (a group hides
     when none of its children are usable). The server enforces the
     same rules — this only hides what you cannot click. */
  document.querySelectorAll("#side-nav [data-op]").forEach((el) => {
    el.classList.toggle("hidden", !canUse(el.dataset.op));
  });
  document.querySelectorAll("#side-nav .nav-group").forEach((g) => {
    const kids = [...g.querySelectorAll("[data-op]")];
    g.classList.toggle("hidden", kids.length > 0 && kids.every((k) => k.classList.contains("hidden")));
  });
  const nameEl = document.getElementById("profile-name");
  const roleEl = document.getElementById("profile-role");
  const avEl = document.getElementById("profile-avatar");
  if (currentUser) {
    if (nameEl) nameEl.textContent = currentUser.username;
    if (roleEl) roleEl.textContent = currentUser.role === "Admin" ? "Administrator" : currentUser.role;
    if (avEl) avEl.textContent = (currentUser.username || "A")[0].toUpperCase();
  }
}

async function loadMe() {
  /* Who is logged in? 401 means the session is gone (server
     restarted) — back to the login page. */
  const urlToken = new URLSearchParams(window.location.search).get("token");
  if (urlToken) {
    try { localStorage.setItem("hms_token", urlToken); } catch(e){}
    try { document.cookie = "hms_session=" + urlToken + "; path=/; max-age=43200; SameSite=Lax"; } catch(e){}
  }
  const res = await api("/api/me");
  if (res && res.ok) {
    currentUser = res.user;
    applyRoleVisibility();
    return true;
  }
  try { localStorage.removeItem("hms_token"); } catch(e){}
  window.location.replace("/login.html?logout=1");
  return false;
}

const $ = (sel) => document.querySelector(sel);
const body = document.getElementById("workspace-body");
const titleEl = document.getElementById("workspace-title");
const subEl = document.getElementById("workspace-sub");
const statusDot = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* Fallback text so an error NEVER renders as an empty red box. */
const FALLBACK_ERR = "Kuch ghalat hua — dobara try karo.";

function errOf(res) {
  return (res && res.error) || FALLBACK_ERR;
}

function offlineBanner() {
  return `<div class="msg msg-err">Server se connect nahi ho raha. Terminal mein <b>python server.py</b> chalao, phir page <b>http://127.0.0.1:8000</b> se kholo — index.html file double-click se nahi.</div>`;
}

async function api(path, method = "GET", payload = null) {
  /* NEVER throws: every failure comes back as { ok:false, error }
     so the UI can always show a real message instead of dying
     silently. (Old version threw on fetch/JSON failure and the
     whole page went blank with no message at all.) */
  let res;
  const headers = { "Content-Type": "application/json" };
  const urlToken = new URLSearchParams(window.location.search).get("token");
  let token = urlToken;
  if (!token) {
    try { token = localStorage.getItem("hms_token"); } catch(e){}
  }
  if (token) {
    headers["Authorization"] = "Bearer " + token;
  }
  try {
    res = await fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      body: payload ? JSON.stringify(payload) : null,
    });
  } catch (e) {
    // Server not running, or page opened via file:// double-click.
    return { ok: false, error: "Server se connect nahi ho raha. Terminal mein 'python server.py' chalao, phir page refresh karo." };
  }
  let data;
  try {
    data = await res.json();
  } catch (e) {
    return { ok: false, error: `Server se ghalat jawab mila (HTTP ${res.status}). 'python server.py' se chalao, file double-click se nahi.` };
  }
  if (!res.ok && data && !data.error) {
    data.error = `Server error (HTTP ${res.status}).`;
  }
  data._status = res.status;
  return data;
}

let toastTimer = null;
function toast(msg, isErr = false) {
  const t = document.getElementById("toast");
  t.textContent = msg || FALLBACK_ERR;
  t.classList.toggle("err", isErr);
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 3200);
}

function setOnlineStatus(online) {
  if (statusDot) statusDot.classList.toggle("dot-err", !online);
  if (statusText) statusText.textContent = online ? "All systems operational" : "Server offline";
}

function refreshStats() {
  /* The dashboard numbers live in dashData (GET /api/dashboard) and
     are rendered by renderDashboardView; this only stamps the footer
     time. A missing number must never show "undefined" anywhere. */
  const lu = document.getElementById("last-update");
  if (lu) lu.textContent =
    new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

async function loadState() {
  /* Never throws and never kills the boot: on failure state stays
     null and the dashboard shows the offline banner instead.
     A 401 means the login session ended — back to the login page. */
  const res = await api("/api/state");
  if (res && res._status === 401) {
    try { localStorage.removeItem("hms_token"); } catch(e){}
    try { document.cookie = "hms_session=; path=/; max-age=0"; } catch(e){}
    window.location.replace("/login.html?logout=1");
    return null;
  }
  if (res && res.ok) {
    state = res.state;
    setOnlineStatus(true);
    refreshStats();
    /* The dashboard shows live numbers — re-render it when its data
       changed underneath (a booking, a bill, a status change). */
    if (currentView === "dashboard") showDashboard();
  } else {
    state = null;
    setOnlineStatus(false);
  }
  return state;
}

/* ------------------------------------------------------------------ ops */

const OPS = {
  "add-patient": ["Add Patient", "Register a new patient"],
  "view-sorted": ["View Sorted", "Browse patients sorted by ID or Age"],
  "find-patient": ["Find Patient", "Find by ID, or search by name — edit or delete from the card"],
  "add-note": ["Add Note", "Record visit note (increments visits)"],
  "doctors": ["Doctors", "View doctor queues or add a doctor"],
  "book-appointment": ["Book Appointment", "Search patient, pick department/doctor, date/time, type"],
  "beds": ["Beds", "Assign or free hospital beds (1–10)"],
  "pharmacy": ["Pharmacy Queue", "FIFO medicine counter queue"],
  "shifts": ["Shifts", "Circular doctor duty rotation"],
  "dashboard": ["Dashboard", "Live hospital overview"],
  "undo": ["Undo", "Revert the last recorded action"],
  "users": ["Users", "Manage user accounts (Admin)"],
  "change-password": ["Change Password", "Change your own password"],
  "billing": ["Billing", "Bills from appointments + extra charges"],
  "reports": ["Reports & Analytics", "Daily, weekly, monthly — appointments, doctors, departments, revenue"],
  "schedule": ["Schedule", "Appointments by date — today or any day"],
  "all-appointments": ["All Appointments", "Search, filter, sort and export every appointment"],
  "availability": ["Doctor Availability", "Working days, hours, breaks, slots and leave per doctor"],
  "holidays": ["Holidays", "Hospital holidays — no appointments on these days"],
  "notifications": ["Notifications", "Booking, cancellation, reschedule, reminder and schedule-change alerts"],
};

/* ---------------- Step 7: views + sidebar navigation ---------------- */

function showModuleView() {
  document.getElementById("dashboard-view").classList.add("hidden");
  document.getElementById("module-view").classList.remove("hidden");
}
function showDashboardSection() {
  document.getElementById("module-view").classList.add("hidden");
  document.getElementById("dashboard-view").classList.remove("hidden");
}
function setModuleHead(title, sub) {
  titleEl.textContent = title;
  if (subEl) subEl.textContent = sub || "";
}

/* Which sidebar section an op belongs to (for the active highlight). */
const OP_NAV = {
  "view-sorted": "patients", "add-patient": "patients",
  "find-patient": "patients", "add-note": "patients",
  "doctors": "doctors", "shifts": "doctors",
  "book-appointment": "appointments", "schedule": "appointments",
  "all-appointments": "appointments", "calendar": "appointments",
  "availability": "doctors", "holidays": "appointments",
  "beds": "beds", "pharmacy": "pharmacy", "billing": "billing",
  "reports": "reports", "departments": "departments",
  "settings": "settings", "change-password": "settings",
  "users": "settings", "dashboard": "dashboard",
  "notifications": "notifications",
};
function setActiveNav(key) {
  document.querySelectorAll("#side-nav .nav-item, #side-nav .nav-sub").forEach((el) => {
    el.classList.toggle("active", el.dataset.nav === key);
  });
  /* Expand the group that holds the active item, collapse the rest. */
  document.querySelectorAll("#side-nav .nav-group").forEach((g) => {
    g.classList.toggle("open", g.dataset.nav === key);
  });
}

function openOp(op) {
  if (op === "dashboard") return showDashboard();
  currentOp = op;
  currentView = "module";
  showModuleView();
  const meta = OPS[op] || [op, ""];
  setModuleHead(meta[0], meta[1]);
  setActiveNav(OP_NAV[op] || op);
  if (!canUse(op)) {
    body.innerHTML = `<div class="msg msg-err">Not allowed for your role (${currentUser ? currentUser.role : "—"}).</div>`;
    return;
  }
  ({
    "add-patient": renderAddPatient,
    "view-sorted": renderViewSorted,
    "find-patient": renderFindPatient,
    "add-note": renderAddNote,
    "doctors": renderDoctors,
    "book-appointment": renderBook,
    "beds": renderBeds,
    "pharmacy": renderPharmacy,
    "shifts": renderShifts,
    "undo": renderUndo,
    "users": renderUsers,
    "change-password": renderChangePassword,
    "billing": renderBilling,
    "reports": renderReports,
    "schedule": renderSchedule,
    "all-appointments": renderAllAppointments,
    "calendar": renderCalendar,
    "availability": renderAvailability,
    "holidays": renderHolidays,
    "notifications": renderNotifications,
  })[op]();
}

function showView(view) {
  if (view === "dashboard") return showDashboard();
  if (view === "departments") return renderDepartments();
  if (view === "settings") return renderSettings();
}

/* Sidebar items. "op" opens an existing module op; "view" opens a
   Step 7 view; "soon" opens an honest "coming in a later step" note —
   never a dead link that pretends to work. */
const NAV = [
  { key: "dashboard", label: "Dashboard", icon: "home", view: "dashboard" },
  { key: "notifications", label: "Notifications", icon: "bell", op: "notifications" },
  { key: "patients", label: "Patient Management", icon: "users", children: [
    { op: "view-sorted", label: "All Patients" },
    { op: "add-patient", label: "Add Patient" },
    { op: "find-patient", label: "Find Patient" },
    { op: "add-note", label: "Add Note" },
  ] },
  { key: "doctors", label: "Doctor Management", icon: "steth", children: [
    { op: "doctors", label: "All Doctors" },
    { op: "availability", label: "Availability" },
    { op: "shifts", label: "Shifts" },
  ] },
  { key: "appointments", label: "Appointments", icon: "cal", children: [
    { op: "all-appointments", label: "All Appointments" },
    { op: "book-appointment", label: "Book Appointment" },
    { op: "schedule", label: "Schedule" },
    { op: "calendar", label: "Calendar" },
    { op: "holidays", label: "Holidays" },
  ] },
  { key: "departments", label: "Departments", icon: "building", view: "departments" },
  { key: "beds", label: "Bed & Ward Management", icon: "bed", op: "beds" },
  { key: "opd", label: "OPD / IPD Management", icon: "clipboard", soon: true,
    note: "OPD / IPD Management is part of the plan and comes in a later step. For now, patients are admitted through Bed & Ward Management and appointments through Schedule." },
  { key: "lab", label: "Laboratory Management", icon: "flask", soon: true,
    note: "Laboratory Management is part of the plan and comes in a later step. Lab charges can already be added to a bill from Billing & Payments." },
  { key: "pharmacy", label: "Pharmacy Management", icon: "pill", op: "pharmacy" },
  { key: "billing", label: "Billing & Payments", icon: "bill", op: "billing" },
  { key: "staff", label: "Staff Management", icon: "staff", soon: true,
    note: "Staff Management is part of the plan and comes in a later step. Login accounts are managed under Settings → Users (Admin only)." },
  { key: "records", label: "Medical Records", icon: "record", soon: true,
    note: "Medical Records is part of the plan and comes in a later step. A patient's notes already live on their patient card — use Find Patient under Patient Management to read them." },
  { key: "reports", label: "Reports & Analytics", icon: "chart", op: "reports" },
  { key: "settings", label: "Settings", icon: "gear", view: "settings" },
];

const ICONS = {
  home: '<path d="M3 11.5 12 3l9 8.5M5 10.5V21h14V10.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  users: '<circle cx="9" cy="8" r="3.5" fill="none" stroke="currentColor" stroke-width="2"/><path d="M2.5 20c.8-3.2 3.4-5 6.5-5s5.7 1.8 6.5 5M16 8.5a3 3 0 1 0-2-5.2M17.5 15.5c2 .6 3.5 2 4 4.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  steth: '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 7v6M9 12h6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  cal: '<rect x="3" y="5" width="18" height="16" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M3 10h18M8 3v4M16 3v4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  building: '<rect x="4" y="3" width="16" height="18" rx="1" fill="none" stroke="currentColor" stroke-width="2"/><path d="M8 7h2M14 7h2M8 11h2M14 11h2M8 15h2M14 15h2M10 21v-3h4v3" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  bed: '<path d="M3 18v-6a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v6M3 18h18M3 18v2m18-2v2M6 10V6h12v4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="7.5" cy="8" r="1" fill="currentColor"/>',
  clipboard: '<rect x="5" y="4" width="14" height="17" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M9 4.5V3h6v1.5M9 11h6M9 15h4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  flask: '<path d="M9 3h6M10 3v5.5L4.5 18a2 2 0 0 0 1.8 3h11.4a2 2 0 0 0 1.8-3L14 8.5V3" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M7.5 14h9" stroke="currentColor" stroke-width="2"/>',
  pill: '<path d="M10.5 20.5 3 13a4.5 4.5 0 0 1 6.4-6.4l7.5 7.5a4.5 4.5 0 0 1-6.4 6.4zM14 7l3 3" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="m8 12 2 2M11 9l2 2" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  bill: '<path d="M6 2h9l5 5v15H6z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M14 2v6h6M9 12h6M9 16h6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  staff: '<circle cx="12" cy="8" r="3.5" fill="none" stroke="currentColor" stroke-width="2"/><path d="M5 20c.8-3.2 3.4-5 7-5s6.2 1.8 7 5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  record: '<path d="M6 2h9l5 5v15H6z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M12 11v5M9.5 13.5h5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  chart: '<path d="M4 20V10M10 20V4M16 20v-8M22 20H2" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  gear: '<circle cx="12" cy="12" r="3.2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 2.8v3M12 18.2v3M2.8 12h3M18.2 12h3M5.5 5.5l2.1 2.1M16.4 16.4l2.1 2.1M18.5 5.5l-2.1 2.1M7.6 16.4l-2.1 2.1" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  bell: '<path d="M18 9a6 6 0 1 0-12 0c0 6-2.5 7-2.5 7h17S18 15 18 9M10 20a2.2 2.2 0 0 0 4 0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
};
function navIcon(name) {
  return `<svg viewBox="0 0 24 24" class="nav-ic" aria-hidden="true">${ICONS[name] || ""}</svg>`;
}

function buildSideNav() {
  const nav = document.getElementById("side-nav");
  nav.innerHTML = NAV.map((item) => {
    const soon = item.soon ? `<span class="soon-badge">Soon</span>` : "";
    if (item.children) {
      return `<div class="nav-group" data-nav="${item.key}">
        <button type="button" class="nav-item" data-nav="${item.key}" data-group="${item.key}">
          ${navIcon(item.icon)}<span class="nav-label">${item.label}</span>
          <span class="nav-chev" aria-hidden="true">▾</span>
        </button>
        <div class="nav-children">
          ${item.children.map((c) => `<button type="button" class="nav-sub" data-op="${c.op}" data-nav="${item.key}">${c.label}</button>`).join("")}
        </div>
      </div>`;
    }
    const attrs = item.op ? `data-op="${item.op}"` : item.view ? `data-view="${item.view}"` : `data-soon="${item.key}"`;
    return `<button type="button" class="nav-item" data-nav="${item.key}" ${attrs}>
      ${navIcon(item.icon)}<span class="nav-label">${item.label}</span>${soon}
    </button>`;
  }).join("");
  nav.querySelectorAll("[data-op]").forEach((el) =>
    el.addEventListener("click", () => openOp(el.dataset.op)));
  nav.querySelectorAll("[data-view]").forEach((el) =>
    el.addEventListener("click", () => showView(el.dataset.view)));
  nav.querySelectorAll("[data-soon]").forEach((el) =>
    el.addEventListener("click", () => openSoon(NAV.find((n) => n.key === el.dataset.soon))));
  nav.querySelectorAll("[data-group]").forEach((el) =>
    el.addEventListener("click", () => {
      const group = el.closest(".nav-group");
      const wasOpen = group.classList.contains("open");
      document.querySelectorAll("#side-nav .nav-group").forEach((g) => g.classList.remove("open"));
      if (!wasOpen) {
        group.classList.add("open");
      } else {
        /* Clicking the open group again opens its first child. */
        const first = group.querySelector("[data-op]");
        if (first) openOp(first.dataset.op);
      }
    }));
}

function msgHtml(text, ok = true) {
  return text ? `<div class="msg ${ok ? "msg-ok" : "msg-err"}">${esc(text)}</div>` : "";
}

function renderAddPatient(msg = "", ok = true) {
  body.innerHTML = `
    ${msgHtml(msg, ok)}
    ${state ? "" : offlineBanner()}
    <form id="f-add-patient" class="form-grid">
      <div class="field"><label>Patient ID</label>
        <input type="number" name="id" min="1" placeholder="e.g. 101" required></div>
      <div class="field"><label>Full Name</label>
        <input type="text" name="name" placeholder="e.g. Ahmed Khan" required></div>
      <div class="field-row">
        <div class="field"><label>Age</label>
          <input type="number" name="age" min="0" max="150" placeholder="e.g. 32" required></div>
        <div class="field"><label>Problem / Symptoms</label>
          <input type="text" name="problem" placeholder="e.g. Cough, fever" required></div>
      </div>
      <div class="field-row">
        <div class="field"><label>Father Name (optional)</label>
          <input type="text" name="father_name" placeholder="e.g. Muhammad Ali"></div>
        <div class="field"><label>Phone (optional)</label>
          <input type="text" name="phone" placeholder="e.g. 0300-1234567"></div>
      </div>
      <div class="field"><label>Gender (optional)</label>
        <select name="gender">
          <option value="">—</option><option>Male</option><option>Female</option><option>Other</option>
        </select></div>
      <div class="btn-row">
        <button type="submit" class="btn btn-primary">Create Patient +</button>
        <button type="button" class="btn btn-ghost" onclick="openOp('dashboard')">Cancel</button>
      </div>
    </form>`;
  document.getElementById("f-add-patient").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const res = await api("/api/patients", "POST", {
      id: Number(f.get("id")), name: f.get("name"),
      age: Number(f.get("age")), problem: f.get("problem"),
      father_name: f.get("father_name"), phone: f.get("phone"),
      gender: f.get("gender"),
    });
    await loadState();
    if (res.ok) {
      toast(res.message);
      renderAddPatient(res.message, true);
    } else {
      const msg = errOf(res);
      toast(msg, true);
      renderAddPatient(msg, false);
    }
  });
}

function patientsTable(patients) {
  if (!patients.length) return `<p class="muted">No patients yet. Add one first.</p>`;
  return `<table class="tbl"><thead><tr>
      <th>ID</th><th>Name</th><th>Age</th><th>Problem</th><th>Visits</th>
    </tr></thead><tbody>
    ${patients.map((p) => `<tr><td>${p.id}</td><td>${esc(p.name)}</td><td>${p.age}</td>
      <td>${esc(p.problem)}</td><td>${p.visits}</td></tr>`).join("")}
    </tbody></table>`;
}

function renderViewSorted() {
  const list = state ? state.patients : [];
  const sorted = [...list].sort((a, b) =>
    sortField === "age" ? a.age - b.age : a.id - b.id);
  body.innerHTML = `
    ${state ? "" : offlineBanner()}
    <div class="field" style="margin-bottom:12px"><label>Sort by</label>
      <div class="seg">
        <button type="button" id="sort-id" class="${sortField === "id" ? "on" : ""}">ID</button>
        <button type="button" id="sort-age" class="${sortField === "age" ? "on" : ""}">Age</button>
      </div>
    </div>
    ${patientsTable(sorted)}`;
  document.getElementById("sort-id").onclick = () => { sortField = "id"; renderViewSorted(); };
  document.getElementById("sort-age").onclick = () => { sortField = "age"; renderViewSorted(); };
}

function patientCard(p) {
  /* Step 5: Edit (Admin/Receptionist) and Delete (Admin only) live on
     the card — the server enforces the same rules. */
  const canEdit = currentUser && ["Admin", "Receptionist"].includes(currentUser.role);
  const canDelete = currentUser && currentUser.role === "Admin";
  return `<div class="card">
    <h3>${esc(p.name)} <span class="muted">#${p.id}</span></h3>
    <p>Age: <b>${p.age}</b> &nbsp;•&nbsp; Problem: <b>${esc(p.problem)}</b> &nbsp;•&nbsp; Visits: <b>${p.visits}</b></p>
    ${p.father_name || p.phone || p.gender ? `<p class="muted">${p.father_name ? `Father: <b>${esc(p.father_name)}</b> &nbsp;•&nbsp; ` : ""}${p.phone ? `Phone: <b>${esc(p.phone)}</b> &nbsp;•&nbsp; ` : ""}${p.gender ? `Gender: <b>${esc(p.gender)}</b>` : ""}</p>` : ""}
    <p class="muted">Notes (${p.notes.length}):</p>
    ${p.notes.length
      ? `<ul class="q-list">${p.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>`
      : `<p class="muted">No notes yet.</p>`}
    ${canEdit || canDelete ? `<div class="btn-row" style="margin-top:10px">
      ${canEdit ? `<button type="button" class="btn btn-primary" data-edit-patient="${p.id}">Edit</button>` : ""}
      ${canDelete ? `<button type="button" class="btn btn-danger" data-delete-patient="${p.id}">Delete</button>` : ""}
    </div>` : ""}
    <div data-edit-slot="${p.id}"></div>
  </div>`;
}

/* Step 5: wire the Edit / Delete buttons of one patient card. Edit
   opens an inline form (name/age/problem prefilled); Delete asks for
   confirmation first, then calls DELETE /api/patients/<id>. */
function wirePatientCard(container, p) {
  const editBtn = container.querySelector(`[data-edit-patient="${p.id}"]`);
  if (editBtn) {
    editBtn.addEventListener("click", () => {
      const slot = container.querySelector(`[data-edit-slot="${p.id}"]`);
      slot.innerHTML = `
        <form class="form-grid" data-edit-form="${p.id}" style="margin-top:12px">
          <div class="field"><label>Name</label>
            <input type="text" name="name" value="${esc(p.name)}" required></div>
          <div class="field-row">
            <div class="field"><label>Age</label>
              <input type="number" name="age" min="0" max="150" value="${p.age}" required></div>
            <div class="field"><label>Problem</label>
              <input type="text" name="problem" value="${esc(p.problem)}" required></div>
          </div>
          <div class="field-row">
            <div class="field"><label>Father Name</label>
              <input type="text" name="father_name" value="${esc(p.father_name || "")}"></div>
            <div class="field"><label>Phone</label>
              <input type="text" name="phone" value="${esc(p.phone || "")}"></div>
          </div>
          <div class="field"><label>Gender</label>
            <select name="gender">
              ${["", "Male", "Female", "Other"].map((g) => `<option value="${g}"${(p.gender || "") === g ? " selected" : ""}>${g || "—"}</option>`).join("")}
            </select></div>
          <div class="btn-row">
            <button type="submit" class="btn btn-primary">Save Changes</button>
            <button type="button" class="btn btn-ghost" data-cancel-edit="${p.id}">Cancel</button>
          </div>
        </form>`;
      slot.querySelector(`[data-edit-form="${p.id}"]`).addEventListener("submit", async (e) => {
        e.preventDefault();
        const f = new FormData(e.target);
        const res = await api(`/api/patients/${p.id}`, "POST", {
          name: f.get("name"), age: Number(f.get("age")), problem: f.get("problem"),
          father_name: f.get("father_name"), phone: f.get("phone"),
          gender: f.get("gender"),
        });
        await loadState();
        toast(res.ok ? res.message : errOf(res), !res.ok);
        renderFindPatient(res.ok ? res.message : errOf(res), res.ok);
      });
      slot.querySelector(`[data-cancel-edit="${p.id}"]`).addEventListener("click", () => {
        slot.innerHTML = "";
      });
    });
  }
  const delBtn = container.querySelector(`[data-delete-patient="${p.id}"]`);
  if (delBtn) {
    delBtn.addEventListener("click", async () => {
      if (!confirm(`Delete patient ${p.name} (#${p.id})?\n\nThis removes their record, notes and queued appointments. Their bills are KEPT.`)) return;
      const res = await api(`/api/patients/${p.id}`, "DELETE");
      await loadState();
      toast(res.ok ? res.message : errOf(res), !res.ok);
      renderFindPatient(res.ok ? res.message : errOf(res), res.ok);
    });
  }
}

function renderFindPatient(msg = "", ok = true) {
  body.innerHTML = `
    ${msgHtml(msg, ok)}
    ${state ? "" : offlineBanner()}
    <form id="f-find" class="form-grid">
      <div class="field"><label>Patient ID</label>
        <input type="number" name="id" min="1" placeholder="e.g. 101" required></div>
      <div class="btn-row"><button type="submit" class="btn btn-primary">Find by ID</button></div>
    </form>
    <form id="f-find-name" class="form-grid" style="margin-top:14px">
      <div class="field"><label>Search by Name</label>
        <input type="text" name="name" placeholder="e.g. Ahmed (part of the name is enough)" required></div>
      <div class="btn-row"><button type="submit" class="btn btn-primary">Search by Name</button></div>
    </form>
    <div id="find-result" style="margin-top:14px"></div>`;
  document.getElementById("f-find").addEventListener("submit", async (e) => {
    e.preventDefault();
    const id = Number(new FormData(e.target).get("id"));
    const res = await api(`/api/patients/${id}`);
    const slot = document.getElementById("find-result");
    if (!res.ok) {
      slot.innerHTML = `<div class="msg msg-err">${esc(errOf(res))}</div>`;
      return;
    }
    slot.innerHTML = patientCard(res.patient);
    wirePatientCard(slot, res.patient);
  });
  document.getElementById("f-find-name").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = new FormData(e.target).get("name");
    const res = await api(`/api/patients?name=${encodeURIComponent(name)}`);
    const slot = document.getElementById("find-result");
    if (!res.ok) {
      slot.innerHTML = `<div class="msg msg-err">${esc(errOf(res))}</div>`;
      return;
    }
    if (!res.patients.length) {
      slot.innerHTML = `<div class="msg">No patient found with that name.</div>`;
      return;
    }
    slot.innerHTML = `<p class="muted">${res.patients.length} patient(s) found:</p>`
      + res.patients.map(patientCard).join("");
    res.patients.forEach((p) => wirePatientCard(slot, p));
  });
}

function renderAddNote(msg = "", ok = true) {
  body.innerHTML = `
    ${msgHtml(msg, ok)}
    ${state ? "" : offlineBanner()}
    <form id="f-note" class="form-grid">
      <div class="field"><label>Patient ID</label>
        <input type="number" name="id" min="1" placeholder="e.g. 101" required></div>
      <div class="field"><label>Note</label>
        <textarea name="note" rows="3" placeholder="e.g. Follow-up in 2 weeks, BP normal" required></textarea></div>
      <div class="btn-row"><button type="submit" class="btn btn-primary">Add Note +</button></div>
    </form>`;
  document.getElementById("f-note").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const res = await api(`/api/patients/${Number(f.get("id"))}/notes`, "POST", { note: f.get("note") });
    await loadState();
    if (res.ok) {
      toast(res.message);
      renderAddNote(res.message, true);
    } else {
      const msg = errOf(res);
      toast(msg, true);
      renderAddNote(msg, false);
    }
  });
}

function renderDoctors(msg = "", ok = true) {
  const docs = state ? state.doctors : [];
  body.innerHTML = `
    ${msgHtml(msg, ok)}
    ${state ? "" : offlineBanner()}
    ${docs.map((d) => `
      <div class="card">
        <h3>${esc(d.name)} <span class="muted">${esc(d.id)} • ${esc(d.spec || "—")} • Fee Rs ${d.fee ?? "—"}</span></h3>
        <p>Waiting: <b>${d.waiting ?? 0}</b> ${d.queue.length
          ? "— " + d.queue.map((q) => {
              /* Step 4: show when the appointment is scheduled for.
                 Step 6: show its status as a coloured badge. */
              const when = [q.appt_date, q.appt_time].filter(Boolean).join(" ");
              const st = q.status || "Pending";
              return `<span class="tag ${q.emergency ? "tag-emg" : "tag-nor"}">${esc(q.token)} · ${esc(q.patient_name)}${when ? " · " + esc(when) : ""}</span> <span class="tag ${statusTagClass(st)}">${esc(st)}</span>`;
            }).join(" ")
          : `<span class="muted">queue empty</span>`}</p>
        ${d.queue.length ? `
        <div class="field-row" style="margin-top:10px">
          <div class="field"><label>Appointment</label>
            <select id="st-token-${esc(d.id)}">
              ${d.queue.map((q) => `<option value="${esc(q.token)}">${esc(q.token)} · ${esc(q.patient_name)}</option>`).join("")}
            </select></div>
          <div class="field"><label>New status</label>
            <select id="st-status-${esc(d.id)}">
              ${statusOptions().map((s) => `<option>${s}</option>`).join("")}
            </select></div>
          <div class="btn-row" style="align-items:flex-end">
            <button type="button" class="btn btn-primary" data-doc="${esc(d.id)}" data-act="set-status">Update Status</button>
            <button type="button" class="btn btn-ghost" data-doc="${esc(d.id)}" data-act="show-history">History</button>
          </div>
        </div>
        <div id="st-history-${esc(d.id)}" style="margin-top:8px"></div>` : ""}
      </div>`).join("") || `<p class="muted">No doctors yet.</p>`}
    ${currentUser && currentUser.role === "Admin" ? `
    <h3 style="margin:16px 0 8px">Add Doctor</h3>
    <form id="f-doc" class="form-grid">
      <div class="field-row">
        <div class="field"><label>Doctor ID</label><input type="text" name="id" placeholder="e.g. D6" required></div>
        <div class="field"><label>Full Name</label><input type="text" name="name" placeholder="e.g. Dr. Sana" required></div>
      </div>
      <div class="field-row">
        <div class="field"><label>Specialty</label><input type="text" name="spec" placeholder="e.g. Physician" required></div>
        <div class="field"><label>Token Fee (Rs)</label><input type="number" name="fee" min="0" placeholder="e.g. 1200" required></div>
      </div>
      <div class="btn-row"><button type="submit" class="btn btn-primary">Add Doctor +</button></div>
    </form>` : `<p class="muted">Only Admin can add a doctor.</p>`}`;
  const docForm = document.getElementById("f-doc");
  /* Step 6: status controls on each doctor card — change a status or
     read its history. The server enforces the role rules; a failed
     change just shows the server's message. */
  document.querySelectorAll('[data-act="set-status"]').forEach((btn) => {
    btn.addEventListener("click", async () => {
      const docId = btn.dataset.doc;
      const token = document.getElementById(`st-token-${docId}`).value;
      const status = document.getElementById(`st-status-${docId}`).value;
      const res = await api(`/api/appointments/${docId}/${token}/status`, "POST", { status });
      await loadState();
      if (res.ok) {
        toast(res.message);
        renderDoctors(res.message, true);
      } else {
        const msg = errOf(res);
        toast(msg, true);
        renderDoctors(msg, false);
      }
    });
  });
  document.querySelectorAll('[data-act="show-history"]').forEach((btn) => {
    btn.addEventListener("click", async () => {
      const docId = btn.dataset.doc;
      const token = document.getElementById(`st-token-${docId}`).value;
      const box = document.getElementById(`st-history-${docId}`);
      const res = await api(`/api/appointments/${docId}/${token}/history`);
      if (!res.ok) {
        box.innerHTML = `<div class="msg msg-err">${esc(errOf(res))}</div>`;
        return;
      }
      const rows = res.history || [];
      box.innerHTML = rows.length
        ? `<div class="card"><h3>History — ${esc(token)}</h3>${rows.map((h) => `<p>${esc(h.changed_at)} | ${esc(h.old_status || "—")} → ${esc(h.new_status)} by ${esc(h.changed_by)}</p>`).join("")}</div>`
        : `<p class="muted">No status changes yet for ${esc(token)}.</p>`;
    });
  });
  if (!docForm) return;
  docForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const res = await api("/api/doctors", "POST", {
      id: f.get("id"), name: f.get("name"), spec: f.get("spec"), fee: Number(f.get("fee")),
    });
    await loadState();
    if (res.ok) {
      toast(res.message);
      renderDoctors(res.message, true);
    } else {
      const msg = errOf(res);
      toast(msg, true);
      renderDoctors(msg, false);
    }
  });
}

/* Step 11 (Doctor Availability & Scheduling): working days, hours,
   breaks, consultation duration, leave and slots per doctor (Admin
   edits; a Doctor sees only their own; Receptionist views only),
   plus the hospital holidays. The server enforces the role rules —
   a failed save just shows the server's message. */
const AVAIL_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const AVAIL_DAY_FULL = { Mon: "Monday", Tue: "Tuesday", Wed: "Wednesday",
  Thu: "Thursday", Fri: "Friday", Sat: "Saturday", Sun: "Sunday" };

async function renderAvailability(msg = "", ok = true) {
  const docs = state ? state.doctors : [];
  const isAdmin = currentUser && currentUser.role === "Admin";
  const ownDoctor = currentUser && currentUser.role === "Doctor"
    ? currentUser.doctor_id : null;
  body.innerHTML = `
    ${msgHtml(msg, ok)}
    ${state ? "" : offlineBanner()}
    ${docs.length ? `
    <div class="field-row">
      <div class="field"><label>Doctor</label>
        <select id="av-doctor">
          ${ownDoctor
            ? docs.filter((d) => d.id === ownDoctor).map((d) => `<option value="${esc(d.id)}">${esc(d.name)} — ${esc(d.spec || "—")}</option>`).join("")
            : `<option value="">— Select doctor —</option>` + docs.map((d) => `<option value="${esc(d.id)}">${esc(d.name)} — ${esc(d.spec || "—")}</option>`).join("")}
        </select></div>
      <div class="field"><label>Show slots for date</label>
        <input type="date" id="av-date"></div>
    </div>
    <div id="av-body" style="margin-top:12px"></div>` : `<p class="muted">No doctors yet.</p>`}`;
  if (!docs.length) return;
  const docSel = document.getElementById("av-doctor");
  const dateInput = document.getElementById("av-date");
  const now = new Date();
  dateInput.value = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;

  async function loadSlots(docId, date, box) {
    const res = await api(`/api/appointments/slots?doctor_id=${encodeURIComponent(docId)}&date=${encodeURIComponent(date)}`);
    if (!res.ok) {
      box.innerHTML = `<div class="msg msg-err">${esc(errOf(res))}</div>`;
      return;
    }
    if (res.unavailable_reason) {
      box.innerHTML = `<div class="msg msg-err">${esc(res.unavailable_reason)}</div>`;
      return;
    }
    const slots = res.slots || [];
    box.innerHTML = slots.length
      ? `<p>${slots.map((s) => s.available
          ? `<span class="tag tag-done" style="margin:2px">${esc(s.time)} — free</span>`
          : `<span class="tag tag-cancel" style="margin:2px">${esc(s.time)} · ${esc(s.token || "")} — TAKEN</span>`).join(" ")}</p>`
      : `<p class="muted">${res.availability ? "No free slots on this date." : "Availability not set yet — any time is allowed."}</p>`;
  }

  async function loadDoctor() {
    const docId = docSel.value;
    const box = document.getElementById("av-body");
    if (!docId) { box.innerHTML = `<p class="muted">Pehle doctor choose karo.</p>`; return; }
    const res = await api(`/api/availability?doctor_id=${encodeURIComponent(docId)}`);
    if (!res.ok) { box.innerHTML = `<div class="msg msg-err">${esc(errOf(res))}</div>`; return; }
    const a = res.availability;
    const leaves = res.leaves || [];
    const canLeave = isAdmin || !!ownDoctor;
    box.innerHTML = `
    <div class="card"><h3>Working Hours</h3>
      ${a ? `<p><b>${esc(a.working_days.join(", "))}</b> • ${esc(a.start_time)}–${esc(a.end_time)} • ${a.duration_minutes} min per appointment${a.breaks.length ? ` • Breaks: ${a.breaks.map((b) => esc(b[0]) + "–" + esc(b[1])).join(", ")}` : " • No breaks"}</p>`
        : `<p class="muted">Availability not set yet — any time is allowed for this doctor.</p>`}
      ${isAdmin ? `
      <form id="f-avail" class="form-grid">
        <div class="field"><label>Working days</label>
          <div>${AVAIL_DAYS.map((d) => `<label style="margin-right:10px"><input type="checkbox" name="day" value="${d}" ${a && a.working_days.includes(d) ? "checked" : ""}> ${AVAIL_DAY_FULL[d]}</label>`).join("")}</div></div>
        <div class="field-row">
          <div class="field"><label>Start time</label><input type="time" name="start" value="${a ? esc(a.start_time) : "09:00"}" required></div>
          <div class="field"><label>End time</label><input type="time" name="end" value="${a ? esc(a.end_time) : "17:00"}" required></div>
          <div class="field"><label>Consultation duration (minutes)</label>
            <select name="duration">${[15, 20, 30, 45, 60].map((m) => `<option ${a && a.duration_minutes === m ? "selected" : ""}>${m}</option>`).join("")}</select></div>
        </div>
        <div class="field"><label>Breaks (one per line, e.g. 13:00-14:00)</label>
          <textarea name="breaks" rows="2" placeholder="13:00-14:00">${a ? esc(a.breaks.map((b) => b[0] + "-" + b[1]).join("\n")) : ""}</textarea></div>
        <div class="btn-row">
          <button type="submit" class="btn btn-primary">Save Availability</button>
          ${a ? `<button type="button" class="btn btn-ghost" id="av-clear">Clear Availability</button>` : ""}
        </div>
      </form>` : `<p class="muted">Only Admin can change availability.</p>`}
    </div>
    <div class="card" style="margin-top:12px"><h3>Slots — <span id="av-slots-date">${esc(dateInput.value)}</span></h3>
      <div id="av-slots"></div></div>
    <div class="card" style="margin-top:12px"><h3>Leave</h3>
      ${leaves.length ? leaves.map((lv) => `<p>#${lv.id} • ${esc(lv.start_date)} to ${esc(lv.end_date)}${lv.reason ? " • " + esc(lv.reason) : ""} ${canLeave ? `<button type="button" class="btn btn-ghost" data-del-leave="${lv.id}">Delete</button>` : ""}</p>`).join("") : `<p class="muted">No leave recorded.</p>`}
      ${canLeave ? `
      <form id="f-leave" class="form-grid">
        <div class="field-row">
          <div class="field"><label>Leave from</label><input type="date" name="from" required></div>
          <div class="field"><label>Leave to</label><input type="date" name="to" required></div>
          <div class="field"><label>Reason (optional)</label><input type="text" name="reason" placeholder="e.g. Annual leave"></div>
        </div>
        <div class="btn-row"><button type="submit" class="btn btn-primary">Add Leave</button></div>
      </form>` : `<p class="muted">Only Admin (or the doctor themself) can add leave.</p>`}
    </div>`;
    const slotsBox = document.getElementById("av-slots");
    loadSlots(docId, dateInput.value, slotsBox);
    const availForm = document.getElementById("f-avail");
    if (availForm) {
      availForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const f = new FormData(e.target);
        const days = f.getAll("day");
        const breaks = f.get("breaks").split("\n").map((s) => s.trim()).filter(Boolean);
        const res2 = await api("/api/availability", "POST", {
          doctor_id: docId, working_days: days,
          start_time: f.get("start"), end_time: f.get("end"),
          duration_minutes: Number(f.get("duration")), breaks,
        });
        await loadState();
        if (res2.ok) {
          toast(res2.message);
          renderAvailability(res2.message, true);
        } else {
          const m = errOf(res2);
          toast(m, true);
          renderAvailability(m, false);
        }
      });
      const clearBtn = document.getElementById("av-clear");
      if (clearBtn) {
        clearBtn.onclick = async () => {
          const res2 = await api("/api/availability/clear", "POST", { doctor_id: docId });
          await loadState();
          if (res2.ok) {
            toast(res2.message);
            renderAvailability(res2.message, true);
          } else {
            const m = errOf(res2);
            toast(m, true);
            renderAvailability(m, false);
          }
        };
      }
    }
    const leaveForm = document.getElementById("f-leave");
    if (leaveForm) {
      leaveForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const f = new FormData(e.target);
        const res2 = await api("/api/leaves", "POST", {
          doctor_id: docId, start_date: f.get("from"),
          end_date: f.get("to"), reason: f.get("reason"),
        });
        await loadState();
        if (res2.ok) {
          toast(res2.message);
          renderAvailability(res2.message, true);
        } else {
          const m = errOf(res2);
          toast(m, true);
          renderAvailability(m, false);
        }
      });
    }
    box.querySelectorAll("[data-del-leave]").forEach((btn) => {
      btn.onclick = async () => {
        const res2 = await api(`/api/leaves/${btn.dataset.delLeave}/delete`, "POST", {});
        await loadState();
        if (res2.ok) {
          toast(res2.message);
          renderAvailability(res2.message, true);
        } else {
          const m = errOf(res2);
          toast(m, true);
          renderAvailability(m, false);
        }
      };
    });
  }

  docSel.onchange = loadDoctor;
  dateInput.onchange = () => {
    const box = document.getElementById("av-slots");
    const label = document.getElementById("av-slots-date");
    if (label) label.textContent = dateInput.value;
    if (box && docSel.value) loadSlots(docSel.value, dateInput.value, box);
  };
  if (docSel.value) loadDoctor();
}

async function renderHolidays(msg = "", ok = true) {
  const isAdmin = currentUser && currentUser.role === "Admin";
  const res = await api("/api/holidays");
  body.innerHTML = `
    ${msgHtml(msg, ok)}
    ${!res.ok ? `<div class="msg msg-err">${esc(errOf(res))}</div>`
      : (res.holidays.length
        ? res.holidays.map((h) => `<div class="card"><p><b>${esc(h.date)}</b> — ${esc(h.name)} ${isAdmin ? `<button type="button" class="btn btn-ghost" data-del-holiday="${h.id}">Delete</button>` : ""}</p></div>`).join("")
        : `<p class="muted">No holidays recorded — the hospital is open every day.</p>`)}
    ${isAdmin ? `
    <h3 style="margin:16px 0 8px">Add Holiday</h3>
    <form id="f-holiday" class="form-grid">
      <div class="field-row">
        <div class="field"><label>Date</label><input type="date" name="date" required></div>
        <div class="field"><label>Holiday name</label><input type="text" name="name" placeholder="e.g. Eid-ul-Fitr" required></div>
      </div>
      <div class="btn-row"><button type="submit" class="btn btn-primary">Add Holiday</button></div>
    </form>` : `<p class="muted">Only Admin can add or delete holidays.</p>`}`;
  document.querySelectorAll("[data-del-holiday]").forEach((btn) => {
    btn.onclick = async () => {
      const res2 = await api(`/api/holidays/${btn.dataset.delHoliday}/delete`, "POST", {});
      await loadState();
      if (res2.ok) {
        toast(res2.message);
        renderHolidays(res2.message, true);
      } else {
        const m = errOf(res2);
        toast(m, true);
        renderHolidays(m, false);
      }
    };
  });
  const form = document.getElementById("f-holiday");
  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      const res2 = await api("/api/holidays", "POST", {
        date: f.get("date"), name: f.get("name"),
      });
      await loadState();
      if (res2.ok) {
        toast(res2.message);
        renderHolidays(res2.message, true);
      } else {
        const m = errOf(res2);
        toast(m, true);
        renderHolidays(m, false);
      }
    });
  }
}

/* Step 8 (Book Appointment form) state — the patient picked (or the
   new patient being registered), the department/doctor picked, and
   the booked times for the chosen doctor/day. */
let bookPatient = null;      /* {id, name, ...} or null */
/* Step 10 (Calendar): a date to prefill the booking form with when
   an empty calendar day is clicked — consumed by renderBook. */
let bookPrefillDate = null;
let bookNewPatient = false;  /* inline registration form open */
let bookDept = "";
let bookDoctor = "";
let bookSlots = [];          /* booked times for bookDoctor + date */

function renderBook(msg = "", ok = true) {
  const docs = state ? state.doctors : [];
  if (!state) {
    /* No doctors at all = state never loaded (server off). An empty
       required <select> would silently block the submit with zero
       feedback — show the real reason instead. */
    body.innerHTML = `
    ${msgHtml(msg, ok)}
    ${offlineBanner()}
    <p class="muted">Jab server connect ho jayega to yahan tumhare doctors aa jayenge.</p>`;
    return;
  }
  /* Step 4: today's date in local time (toISOString is UTC and can
     be yesterday after midnight in Pakistan). */
  const now = new Date();
  const todayLocal = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  /* Step 10: an empty calendar day opens this form with that date
     already filled in (consumed here, so a later re-render falls
     back to today). */
  const startDate = bookPrefillDate || todayLocal;
  bookPrefillDate = null;
  const depts = [...new Set(docs.map((d) => d.spec || "—"))].sort();
  body.innerHTML = `
    ${msgHtml(msg, ok)}
    <form id="f-book" class="form-grid">
      <h3 style="margin:4px 0 8px">1. Patient</h3>
      <div class="field-row">
        <div class="field"><label>Search Patient (ID / name / phone)</label>
          <input type="text" id="bk-search" placeholder="e.g. Ahmed, 101, or 0300"></div>
        <div class="btn-row" style="align-items:flex-end">
          <button type="button" id="bk-search-btn" class="btn btn-primary">Search</button>
          <button type="button" id="bk-new-btn" class="btn btn-ghost">+ New Patient</button>
        </div>
      </div>
      <div id="bk-search-results" style="margin-top:8px"></div>
      <div id="bk-patient-card" style="margin-top:8px"></div>
      <div id="bk-new-patient" class="hidden" style="margin-top:8px">
        <div class="field-row">
          <div class="field"><label>Full Name *</label>
            <input type="text" id="bk-np-name" placeholder="e.g. Ahmed Khan"></div>
          <div class="field"><label>Age *</label>
            <input type="number" id="bk-np-age" min="0" max="150" placeholder="e.g. 32"></div>
        </div>
        <div class="field-row">
          <div class="field"><label>Problem / Symptoms *</label>
            <input type="text" id="bk-np-problem" placeholder="e.g. Cough, fever"></div>
          <div class="field"><label>Father Name</label>
            <input type="text" id="bk-np-father" placeholder="optional"></div>
        </div>
        <div class="field-row">
          <div class="field"><label>Phone</label>
            <input type="text" id="bk-np-phone" placeholder="e.g. 0300-1234567"></div>
          <div class="field"><label>Gender</label>
            <select id="bk-np-gender">
              <option value="">—</option><option>Male</option><option>Female</option><option>Other</option>
            </select></div>
        </div>
        <p class="muted">Patient ID auto-assign ho jayega (naya ID number khud ban jayega).</p>
      </div>

      <h3 style="margin:16px 0 8px">2. Doctor</h3>
      <div class="field-row">
        <div class="field"><label>Department</label>
          <select id="bk-dept">
            <option value="">— Select department —</option>
            ${depts.map((d) => `<option>${esc(d)}</option>`).join("")}
          </select></div>
        <div class="field"><label>Doctor</label>
          <select id="bk-doctor" disabled>
            <option value="">— Pick a department first —</option>
          </select></div>
      </div>
      <div id="bk-doctor-info" style="margin-top:8px"></div>
      <div id="bk-slots" style="margin-top:8px">
        <p class="muted">Doctor aur date choose karo to yahan us din ke booked times dikhenge.</p>
      </div>

      <h3 style="margin:16px 0 8px">3. Appointment</h3>
      <div class="field-row">
        <div class="field"><label>Date</label>
          <input type="date" id="bk-date" value="${startDate}" required></div>
        <div class="field"><label>Time (24-hour, optional)</label>
          <input type="time" id="bk-time" placeholder="e.g. 10:30"></div>
      </div>
      <div class="field-row">
        <div class="field"><label>Type</label>
          <select id="bk-type">
            <option>General</option><option>Follow-up</option><option>Emergency</option>
          </select></div>
        <div class="field"><label>Status</label>
          <select id="bk-status">
            <option>Pending</option><option>Confirmed</option>
          </select></div>
      </div>
      <div class="field"><label>Reason for Visit</label>
        <input type="text" id="bk-reason" placeholder="e.g. Follow-up checkup"></div>
      <div class="field"><label>Additional Notes</label>
        <input type="text" id="bk-notes" placeholder="optional"></div>
      <p class="muted">Emergency type = E- token (queue mein pehle). General / Follow-up = N- token.</p>
      <div class="btn-row">
        <button type="submit" class="btn btn-primary">✓ Confirm &amp; Book</button>
        <button type="button" class="btn btn-ghost" onclick="openOp('dashboard')">Cancel</button>
      </div>
    </form>
    <div id="book-result" style="margin-top:14px"></div>`;

  const deptSel = document.getElementById("bk-dept");
  const docSel = document.getElementById("bk-doctor");
  const dateInput = document.getElementById("bk-date");
  const infoBox = document.getElementById("bk-doctor-info");
  const slotsBox = document.getElementById("bk-slots");

  function renderSelectedPatient() {
    const card = document.getElementById("bk-patient-card");
    if (!bookPatient) { card.innerHTML = ""; return; }
    const p = bookPatient;
    card.innerHTML = `<div class="card"><h3>${esc(p.name)} <span class="muted">#${p.id}</span></h3>
      <p>Age: <b>${p.age}</b> &nbsp;•&nbsp; Problem: <b>${esc(p.problem)}</b>${p.phone ? ` &nbsp;•&nbsp; Phone: <b>${esc(p.phone)}</b>` : ""}${p.father_name ? ` &nbsp;•&nbsp; Father: <b>${esc(p.father_name)}</b>` : ""}${p.gender ? ` &nbsp;•&nbsp; Gender: <b>${esc(p.gender)}</b>` : ""}</p>
      <button type="button" class="btn btn-ghost" id="bk-clear-patient">Change Patient</button></div>`;
    document.getElementById("bk-clear-patient").onclick = () => {
      bookPatient = null;
      renderSelectedPatient();
    };
  }

  async function refreshSlots() {
    if (!bookDoctor) {
      slotsBox.innerHTML = `<p class="muted">Doctor aur date choose karo to yahan us din ke booked times dikhenge.</p>`;
      return;
    }
    const date = dateInput.value || todayLocal;
    const res = await api(`/api/appointments/slots?doctor_id=${encodeURIComponent(bookDoctor)}&date=${encodeURIComponent(date)}`);
    if (!res.ok) {
      slotsBox.innerHTML = `<div class="msg msg-err">${esc(errOf(res))}</div>`;
      return;
    }
    bookSlots = res.booked || [];
    /* Step 11: the live availability for this doctor + day — the
       working pattern, the free slot chips (click one to fill the
       time), and the plain reason when there is no slot at all
       (holiday, leave, not a working day). When no availability is
       set yet, any time is allowed and the free time field below is
       the way to book. */
    let html = "";
    if (res.unavailable_reason) {
      html += `<div class="msg msg-err">${esc(res.unavailable_reason)}</div>`;
    }
    if (res.availability) {
      const a = res.availability;
      html += `<p><b>${esc(a.start_time)}–${esc(a.end_time)}</b> • ${esc(a.working_days.join(", "))} • ${a.duration_minutes} min per appointment${a.breaks.length ? ` • Breaks: ${a.breaks.map((b) => esc(b[0]) + "–" + esc(b[1])).join(", ")}` : ""}</p>`;
      if (!res.unavailable_reason) {
        const slots = res.slots || [];
        html += slots.length
          ? `<p><b>Available slots on ${esc(res.date)}:</b></p><p>${slots.map((s) => s.available
              ? `<button type="button" class="btn btn-ghost" data-slot="${esc(s.time)}" style="margin:2px">${esc(s.time)}</button>`
              : `<span class="tag tag-cancel" style="margin:2px">${esc(s.time)} · ${esc(s.token || "")} — TAKEN</span>`).join(" ")}</p><p class="muted">Slot par click karo to time bhar jayega.</p>`
          : `<p class="muted">No free slots on ${esc(res.date)}.</p>`;
      }
    } else {
      html += `<p class="muted">Availability not set for this doctor yet — pick any time below.</p>`;
    }
    html += bookSlots.length
      ? `<p><b>Booked on ${esc(res.date)}:</b></p>
        <p>${bookSlots.map((b) => {
          const taken = !["Cancelled", "No Show"].includes(b.status);
          return `<span class="tag ${taken ? "tag-cancel" : "tag-done"}">${esc(b.time)} · ${esc(b.token)} · ${esc(b.patient_name)}</span> <span class="tag ${statusTagClass(b.status)}">${esc(b.status)}</span>${taken ? " — TAKEN" : " — free again"}`;
        }).join("<br>")}</p>`
      : `<p class="muted">Nothing booked on ${esc(res.date)} yet.</p>`;
    slotsBox.innerHTML = html;
    slotsBox.querySelectorAll("[data-slot]").forEach((btn) => {
      btn.onclick = () => {
        document.getElementById("bk-time").value = btn.dataset.slot;
      };
    });
  }

  function refreshDoctors() {
    const docsInDept = docs.filter((d) => (d.spec || "—") === bookDept);
    docSel.innerHTML = docsInDept.length
      ? `<option value="">— Select doctor —</option>` + docsInDept.map((d) => `<option value="${esc(d.id)}">${esc(d.name)} — Rs ${d.fee ?? "—"}</option>`).join("")
      : `<option value="">— No doctors in this department —</option>`;
    docSel.disabled = !docsInDept.length;
    bookDoctor = "";
    infoBox.innerHTML = "";
    refreshSlots();
  }

  document.getElementById("bk-search-btn").onclick = async () => {
    const q = document.getElementById("bk-search").value.trim();
    const box = document.getElementById("bk-search-results");
    if (!q) { box.innerHTML = `<p class="muted">Kuch likho — ID, naam ya phone number.</p>`; return; }
    const res = await api(`/api/patients?name=${encodeURIComponent(q)}`);
    if (!res.ok) { box.innerHTML = `<div class="msg msg-err">${esc(errOf(res))}</div>`; return; }
    if (!res.patients.length) {
      box.innerHTML = `<p class="muted">No patient found — "New Patient" se register kar lo.</p>`;
      return;
    }
    box.innerHTML = `<p class="muted">${res.patients.length} patient(s) found — select karo:</p>` +
      res.patients.map((p) => `<button type="button" class="btn btn-ghost" data-pick-patient="${p.id}" style="margin:2px">${esc(p.name)} (#${p.id})${p.phone ? " · " + esc(p.phone) : ""}</button>`).join(" ");
    box.querySelectorAll("[data-pick-patient]").forEach((btn) => {
      btn.onclick = () => {
        bookPatient = res.patients.find((p) => String(p.id) === btn.dataset.pickPatient);
        bookNewPatient = false;
        document.getElementById("bk-new-patient").classList.add("hidden");
        document.getElementById("bk-new-btn").textContent = "+ New Patient";
        box.innerHTML = "";
        renderSelectedPatient();
      };
    });
  };

  document.getElementById("bk-new-btn").onclick = () => {
    bookNewPatient = !bookNewPatient;
    bookPatient = bookNewPatient ? null : bookPatient;
    document.getElementById("bk-new-patient").classList.toggle("hidden", !bookNewPatient);
    document.getElementById("bk-new-btn").textContent = bookNewPatient ? "✕ Cancel New Patient" : "+ New Patient";
    if (bookNewPatient) renderSelectedPatient();
  };

  deptSel.onchange = () => {
    bookDept = deptSel.value;
    refreshDoctors();
  };
  docSel.onchange = () => {
    bookDoctor = docSel.value;
    const doc = docs.find((d) => d.id === bookDoctor);
    infoBox.innerHTML = doc
      ? `<p><b>${esc(doc.name)}</b> — ${esc(doc.spec || "—")} • Consultation Fee: <b>Rs ${doc.fee ?? "—"}</b></p>`
      : "";
    refreshSlots();
  };
  dateInput.onchange = refreshSlots;

  document.getElementById("f-book").addEventListener("submit", async (e) => {
    e.preventDefault();
    const resultBox = document.getElementById("book-result");
    const type = document.getElementById("bk-type").value;
    const status = document.getElementById("bk-status").value;
    const date = dateInput.value || "";
    const time = document.getElementById("bk-time").value || "";
    const reason = document.getElementById("bk-reason").value.trim();
    const notes = document.getElementById("bk-notes").value.trim();
    if (!bookDoctor) {
      resultBox.innerHTML = `<div class="msg msg-err">Pehle department aur doctor choose karo.</div>`;
      return;
    }
    let payload = { doctor_id: bookDoctor, type, status, date, time, reason, notes };
    if (bookNewPatient) {
      const npName = document.getElementById("bk-np-name").value.trim();
      const npAge = Number(document.getElementById("bk-np-age").value);
      const npProblem = document.getElementById("bk-np-problem").value.trim();
      if (!npName || !npProblem || !npAge) {
        resultBox.innerHTML = `<div class="msg msg-err">New patient ke liye Name, Age aur Problem zaroori hain.</div>`;
        return;
      }
      payload.new_patient = {
        name: npName, age: npAge, problem: npProblem,
        father_name: document.getElementById("bk-np-father").value.trim(),
        phone: document.getElementById("bk-np-phone").value.trim(),
        gender: document.getElementById("bk-np-gender").value,
      };
    } else if (bookPatient) {
      payload.patient_id = bookPatient.id;
    } else {
      resultBox.innerHTML = `<div class="msg msg-err">Pehle patient search kar ke select karo, ya "New Patient" se register karo.</div>`;
      return;
    }
    const res = await api("/api/appointments", "POST", payload);
    await loadState();
    if (!res.ok) {
      const msg = errOf(res);
      toast(msg, true);
      resultBox.innerHTML = `<div class="msg msg-err">${esc(msg)}</div>`;
      return;
    }
    toast(res.message);
    const a = res.appointment;
    resultBox.innerHTML = `
      <div class="card"><h3>✓ Booked — Appointment ID ${esc(a.apt_id)} <span class="tag ${a.emergency ? "tag-emg" : "tag-nor"}">${esc(a.token)} · ${esc(a.appt_type)}</span></h3>
      <p>${esc(a.patient_name)} (#${a.patient_id}) with ${esc(a.doctor_name)} (${esc(a.department)})</p>
      <p>${esc(a.appt_date)}${a.appt_time ? " at " + esc(a.appt_time) : ""} • Fee Rs ${a.fee} • Status: <span class="tag ${statusTagClass(a.status)}">${esc(a.status)}</span></p>
      ${a.reason ? `<p>Reason: ${esc(a.reason)}</p>` : ""}
      <div class="btn-row" style="margin-top:10px">
        <button type="button" class="btn btn-primary" id="bk-bill-btn">Generate Bill (Rs ${a.fee})</button>
        <button type="button" class="btn btn-ghost" onclick="openOp('schedule')">View Schedule</button>
      </div>
      <div id="bk-bill-result" style="margin-top:10px"></div></div>`;
    document.getElementById("bk-bill-btn").onclick = async () => {
      const billRes = await api("/api/bills", "POST", {
        patient_id: a.patient_id, doctor_id: a.doctor_id, token: a.token,
      });
      await loadState();
      const slot = document.getElementById("bk-bill-result");
      if (!billRes.ok) {
        toast(errOf(billRes), true);
        slot.innerHTML = `<div class="msg msg-err">${esc(errOf(billRes))}</div>`;
        return;
      }
      toast(billRes.message);
      slot.innerHTML = `<div class="msg msg-ok">Bill #${billRes.bill.id} created — Total Rs ${billRes.bill.total} (Unpaid).</div>`;
      document.getElementById("bk-bill-btn").disabled = true;
    };
    /* Reset the form for the next booking, keep the doctor picked. */
    bookPatient = null;
    bookNewPatient = false;
    renderSelectedPatient();
  });
}

function renderBeds(msg = "", ok = true) {
  const beds = state ? state.beds : [];
  body.innerHTML = `
    ${msgHtml(msg, ok)}
    ${state ? "" : offlineBanner()}
    <div class="beds-grid">
      ${beds.map((b) => `<div class="bed ${b.status === "Free" ? "free" : "occ"}">
          Bed ${b.bed}<small>${b.status === "Free" ? "Free" : "Patient " + b.patient_id}</small>
        </div>`).join("")}
    </div>
    <form id="f-bed" class="form-grid">
      <div class="field-row">
        <div class="field"><label>Bed (1–10)</label>
          <input type="number" name="bed" min="1" max="10" placeholder="e.g. 3" required></div>
        <div class="field"><label>Patient ID</label>
          <input type="number" name="patient_id" min="1" placeholder="e.g. 101" required></div>
      </div>
      <div class="btn-row">
        <button type="submit" class="btn btn-primary">Assign Bed</button>
        <button type="button" id="btn-free-bed" class="btn btn-ghost">Free This Bed</button>
      </div>
    </form>`;
  document.getElementById("f-bed").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const res = await api("/api/beds/assign", "POST", {
      bed: Number(f.get("bed")), patient_id: Number(f.get("patient_id")),
    });
    await loadState();
    toast(res.ok ? res.message : errOf(res), !res.ok);
    renderBeds(res.ok ? res.message : errOf(res), res.ok);
  });
  document.getElementById("btn-free-bed").addEventListener("click", async () => {
    const bed = Number(document.querySelector('#f-bed [name="bed"]').value);
    if (!bed) { toast("Enter a bed number first.", true); return; }
    const res = await api("/api/beds/free", "POST", { bed });
    await loadState();
    toast(res.ok ? res.message : errOf(res), !res.ok);
    renderBeds(res.ok ? res.message : errOf(res), res.ok);
  });
}

function renderPharmacy(msg = "", ok = true) {
  const q = state ? state.pharmacy : [];
  body.innerHTML = `
    ${msgHtml(msg, ok)}
    ${state ? "" : offlineBanner()}
    <form id="f-pharm" class="form-grid">
      <div class="field"><label>Token / Name</label>
        <input type="text" name="token" placeholder="e.g. T-12 or patient name" required></div>
      <div class="btn-row">
        <button type="submit" class="btn btn-primary">Add Token +</button>
        <button type="button" id="btn-serve" class="btn btn-green">Serve Next</button>
      </div>
    </form>
    <h3 style="margin:16px 0 8px">Queue (${q.length})</h3>
    ${q.length
      ? `<ol class="q-list">${q.map((t, i) => `<li>${i === 0 ? "▶ " : ""}${esc(t)}</li>`).join("")}</ol>`
      : `<p class="muted">Pharmacy queue is empty.</p>`}`;
  document.getElementById("f-pharm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const res = await api("/api/pharmacy", "POST", { token: new FormData(e.target).get("token") });
    await loadState();
    toast(res.ok ? res.message : errOf(res), !res.ok);
    renderPharmacy(res.ok ? res.message : errOf(res), res.ok);
  });
  document.getElementById("btn-serve").addEventListener("click", async () => {
    const res = await api("/api/pharmacy/serve", "POST", {});
    await loadState();
    toast(res.ok ? res.message : errOf(res), !res.ok);
    renderPharmacy(res.ok ? res.message : errOf(res), res.ok);
  });
}

function renderShifts(msg = "", ok = true) {
  const shifts = state ? state.shifts : [];
  body.innerHTML = `
    ${msgHtml(msg, ok)}
    ${state ? "" : offlineBanner()}
    <p>On duty now: <b>${state && state.on_duty ? esc(state.on_duty) : "—"}</b></p>
    <ol class="q-list">
      ${shifts.map((s, i) => `<li>${esc(s)} ${i === 0 ? '<span class="tag tag-duty">ON DUTY</span>' : ""}</li>`).join("")}
    </ol>
    <div class="btn-row" style="margin-top:14px">
      <button type="button" id="btn-rotate" class="btn btn-primary">Rotate Shift ↻</button>
    </div>`;
  document.getElementById("btn-rotate").addEventListener("click", async () => {
    const res = await api("/api/shifts/rotate", "POST", {});
    await loadState();
    toast(res.ok ? res.message : errOf(res), !res.ok);
    renderShifts(res.ok ? res.message : errOf(res), res.ok);
  });
}

/* ------------------------------------------------------------------ Step 7 dashboard */

function fmtTime12(hhmm) {
  if (!hhmm) return "—";
  const [h, m] = hhmm.split(":").map(Number);
  const ap = h >= 12 ? "PM" : "AM";
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${String(h12).padStart(2, "0")}:${String(m).padStart(2, "0")} ${ap}`;
}

function relTime(when) {
  if (!when) return "";
  if (when.length > 10) {
    /* Full timestamp: "x min ago" / "x h ago" / date. */
    const mins = Math.max(0, Math.round((Date.now() - new Date(when).getTime()) / 60000));
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins} min ago`;
    if (mins < 60 * 24) return `${Math.round(mins / 60)} h ago`;
    return when.slice(0, 10);
  }
  const now = new Date();
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  return when === today ? "Today" : when;
}

const CHART_COLORS = ["#1769AA", "#16a34a", "#f59e0b", "#7c3aed",
  "#0ea5e9", "#ec4899", "#14b8a6", "#ef4444"];

/* Monthly appointments as an inline SVG line chart — no external
   library, so the page works offline on Waleed's laptop. */
function lineChartSVG(series) {
  const W = 640, H = 260, PL = 40, PR = 14, PT = 20, PB = 30;
  if (!series.length) {
    return `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" role="img" aria-label="Monthly appointments chart"><text x="${W / 2}" y="${H / 2}" text-anchor="middle" font-size="14" fill="#64748b">No data yet</text></svg>`;
  }
  const maxV = Math.max(0, ...series.map((s) => s.count));
  /* Y-axis scale that fits the data. A fixed 0–50 scale squashed the
     line flat along the bottom whenever a month had only a few
     appointments, so the chart looked dead. Pick the smallest
     "nice" step whose 4 grid intervals cover the biggest month, so
     the line always fills the chart (max 5 → 0–8, max 17 → 0–20). */
  const step = [1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000]
    .find((s) => maxV / s <= 4) || 1000;
  const gridMax = step * 4;
  const xs = (i) => PL + (i * (W - PL - PR)) / (series.length - 1 || 1);
  const ys = (v) => PT + (H - PT - PB) * (1 - v / gridMax);
  const pts = series.map((s, i) => [xs(i), ys(s.count)]);
  /* Smooth curve through the points (Catmull-Rom → Bézier). */
  let d = `M ${pts[0][0]} ${pts[0][1]}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[Math.max(0, i - 1)], p1 = pts[i], p2 = pts[i + 1], p3 = pts[Math.min(pts.length - 1, i + 2)];
    d += ` C ${p1[0] + (p2[0] - p0[0]) / 6} ${p1[1] + (p2[1] - p0[1]) / 6}, ${p2[0] - (p3[0] - p1[0]) / 6} ${p2[1] - (p3[1] - p1[1]) / 6}, ${p2[0]} ${p2[1]}`;
  }
  const area = `${d} L ${pts[pts.length - 1][0]} ${H - PB} L ${pts[0][0]} ${H - PB} Z`;
  const grid = [0, 1, 2, 3, 4].map((f) => {
    const v = step * f;
    const y = ys(v);
    return `<line x1="${PL}" y1="${y}" x2="${W - PR}" y2="${y}" stroke="#e2e8f0" stroke-width="1"/>
      <text x="${PL - 8}" y="${y + 4}" text-anchor="end" font-size="11" fill="#64748b">${v}</text>`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" role="img" aria-label="Monthly appointments chart">
    <defs><linearGradient id="chartFill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#1769AA" stop-opacity="0.25"/>
      <stop offset="100%" stop-color="#1769AA" stop-opacity="0.02"/>
    </linearGradient></defs>
    ${grid}
    <path d="${area}" fill="url(#chartFill)"/>
    <path d="${d}" fill="none" stroke="#1769AA" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
    ${pts.map(([x, y], i) => `<circle class="chart-dot" data-i="${i}" cx="${x}" cy="${y}" r="4.5" fill="#fff" stroke="#1769AA" stroke-width="2.5"/>`).join("")}
    ${series.map((s, i) => `<text x="${xs(i)}" y="${H - 8}" text-anchor="middle" font-size="11" fill="#64748b">${esc(s.label)}</text>`).join("")}
  </svg>`;
}

/* Department split as an inline SVG donut (stroke-dasharray arcs). */
function donutSVG(depts) {
  const total = depts.reduce((s, x) => s + x.count, 0);
  if (!total) return "";
  const R = 70, C = 2 * Math.PI * R;
  let off = 0;
  const arcs = depts.map((x, i) => {
    const len = (x.count / total) * C;
    const el = `<circle cx="90" cy="90" r="${R}" fill="none" stroke="${CHART_COLORS[i % CHART_COLORS.length]}" stroke-width="34" stroke-dasharray="${len} ${C - len}" stroke-dashoffset="${-off}" transform="rotate(-90 90 90)"/>`;
    off += len;
    return el;
  }).join("");
  const top = depts[0];
  return `<svg viewBox="0 0 180 180" class="donut-svg" role="img" aria-label="Department statistics donut">
    ${arcs}
    <text x="90" y="86" text-anchor="middle" font-size="22" font-weight="800" fill="#172033">${top.percent}%</text>
    <text x="90" y="106" text-anchor="middle" font-size="12" fill="#475569">${esc(top.name)}</text>
  </svg>`;
}

function dashStatCard(key, icon, value, label, sub, op) {
  return `<div class="dash-stat dash-stat-${key}" data-stat="${key}" data-op="${op || ""}" role="button" tabindex="0" title="${esc(label)} kholo">
    <div class="dash-stat-ic">${icon}</div>
    <div class="dash-stat-num">${value}</div>
    <div class="dash-stat-label">${esc(label)}</div>
    <div class="dash-stat-sub">${esc(sub)}</div>
  </div>`;
}

function renderDashboardView() {
  const view = document.getElementById("dashboard-view");
  const d = dashData;
  if (!d) {
    view.innerHTML = `<p class="hint">Loading…</p>`;
    return;
  }
  const series = chartMode === "last12" ? d.monthly_last12 : d.monthly_year;
  const seriesTotal = series.reduce((n, s) => n + s.count, 0);
  const todayPending = d.todays_list.filter((a) => a.status === "Pending").length;
  const deptCount = new Set((state ? state.doctors : []).map((x) => x.spec)).size;
  const user = currentUser ? currentUser.username : "Admin";
  const now = new Date();
  const dateStr = now.toLocaleDateString("en-GB", { weekday: "long", day: "2-digit", month: "short", year: "numeric" });
  const timeStr = now.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
  view.innerHTML = `
    <div class="dash-head">
      <div>
        <h1>Dashboard</h1>
        <p>Welcome back, ${esc(user)}! Here's what's happening at City Care Hospital today.</p>
      </div>
      <div class="dash-chips">
        <span class="chip">${esc(dateStr)}</span>
        <span class="chip chip-blue"><span id="dash-clock">${esc(timeStr)}</span></span>
      </div>
    </div>

    <div class="dash-stats">
      ${dashStatCard("patients", navIcon("users"), d.total_patients, "Total Patients", "Registered in the hospital", "view-sorted")}
      ${dashStatCard("doctors", navIcon("steth"), d.total_doctors, "Total Doctors", `${deptCount} department${deptCount === 1 ? "" : "s"}`, "doctors")}
      ${dashStatCard("appointments", navIcon("cal"), d.todays_appointments, "Today's Appointments", `${todayPending} pending today`, "schedule")}
      ${dashStatCard("beds", navIcon("bed"), d.available_beds, "Available Beds", `of ${d.total_beds} beds`, "beds")}
      ${dashStatCard("revenue", navIcon("money"), `Rs. ${d.total_revenue.toLocaleString("en-PK")}`, "Total Revenue", "Paid bills, all time", "billing")}
      ${dashStatCard("pending", navIcon("bill"), d.pending_bills, "Pending Bills", `Rs. ${d.pending_bills_total.toLocaleString("en-PK")} owed`, "billing")}
    </div>

    <div class="dash-grid">
      <div class="dash-card">
        <div class="dash-card-head">
          <h3>Monthly Appointments Overview</h3>
          <select id="chart-mode" aria-label="Chart range">
            <option value="year" ${chartMode === "year" ? "selected" : ""}>This Year</option>
            <option value="last12" ${chartMode === "last12" ? "selected" : ""}>Last 12 Months</option>
          </select>
        </div>
        <div class="chart-wrap" id="chart-wrap">
          ${seriesTotal === 0 ? `<p class="chart-empty">No appointments yet — book an appointment and the analysis will appear here.</p>` : ""}
          ${lineChartSVG(series)}
          <div id="chart-tip" class="chart-tip hidden"></div>
        </div>
      </div>

      <div class="dash-card">
        <div class="dash-card-head">
          <h3>Today's Appointments</h3>
          <button type="button" class="link-btn" id="today-view-all">View All →</button>
        </div>
        ${d.todays_list.length ? `<table class="tbl"><thead><tr>
            <th>Time</th><th>Patient Name</th><th>Doctor</th><th>Department</th><th>Status</th>
          </tr></thead><tbody>
          ${d.todays_list.map((a) => `<tr>
            <td>${a.appt_time ? esc(fmtTime12(a.appt_time)) : "—"}</td>
            <td>${esc(a.patient_name)}</td>
            <td>${esc(a.doctor_name)}</td>
            <td>${esc(a.department || "—")}</td>
            <td><span class="tag ${statusTagClass(a.status || "Pending")}">${esc(a.status || "Pending")}</span></td>
          </tr>`).join("")}</tbody></table>`
          : `<p class="muted">No appointments today yet.</p>`}
      </div>
    </div>

    <div class="dash-grid-3">
      <div class="dash-card">
        <div class="dash-card-head"><h3>Department Statistics</h3></div>
        ${d.departments.length ? `<div class="donut-wrap">${donutSVG(d.departments)}
          <ul class="donut-legend">
            ${d.departments.map((x, i) => `<li><span class="dot" style="background:${CHART_COLORS[i % CHART_COLORS.length]}"></span>${esc(x.name)}<b>${x.percent}%</b></li>`).join("")}
          </ul></div>`
          : `<p class="muted">No appointments yet — book an appointment to see the department split here.</p>`}
      </div>

      <div class="dash-card">
        <div class="dash-card-head">
          <h3>Recent Activities</h3>
          <button type="button" class="link-btn" id="activities-view-all">View All →</button>
        </div>
        ${d.activities.length ? `<ul class="activity-list">
          ${d.activities.map((a) => `<li><span class="act-dot act-${a.kind}"></span>
            <span class="act-text">${esc(a.text)}</span>
            <span class="act-when">${esc(relTime(a.when))}</span></li>`).join("")}
        </ul>` : `<p class="muted">No activity yet.</p>`}
      </div>

      <div class="dash-col">
        <div class="dash-card">
          <div class="dash-card-head"><h3>Quick Actions</h3></div>
          <div class="quick-grid">
            <button type="button" class="quick-btn" data-op="book-appointment">${navIcon("cal")}<span>Book Appointment</span></button>
            <button type="button" class="quick-btn" data-op="add-patient">${navIcon("users")}<span>Add Patient</span></button>
            <button type="button" class="quick-btn" data-op="doctors">${navIcon("steth")}<span>Add Doctor</span></button>
            <button type="button" class="quick-btn" data-op="billing">${navIcon("bill")}<span>Generate Bill</span></button>
            <button type="button" class="quick-btn" data-op="reports">${navIcon("chart")}<span>View Reports</span></button>
            <button type="button" class="quick-btn" data-op="beds">${navIcon("bed")}<span>Manage Beds</span></button>
          </div>
        </div>
        <div class="promo-card">
          <div class="promo-cross" aria-hidden="true">✚</div>
          <div class="promo-text"><b>Your Health<br>Our Priority</b></div>
          <button type="button" class="promo-btn" id="promo-learn">Learn More →</button>
        </div>
      </div>
    </div>`;

  /* Stat cards are shortcuts, not decoration. */
  view.querySelectorAll(".dash-stat[data-op]").forEach((c) => {
    const go = () => { const op = c.dataset.op; if (op && canUse(op)) openOp(op); };
    c.addEventListener("click", go);
    c.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
    });
  });
  document.getElementById("chart-mode").addEventListener("change", (e) => {
    chartMode = e.target.value;
    renderDashboardView();
  });
  /* Chart dots: hover shows the real count for that month. */
  const tip = document.getElementById("chart-tip");
  view.querySelectorAll(".chart-dot").forEach((dot) => {
    dot.addEventListener("mouseenter", () => {
      const s = series[Number(dot.dataset.i)];
      tip.textContent = `${s.count} Appointments — ${s.label}`;
      tip.style.left = `${(Number(dot.getAttribute("cx")) / 640) * 100}%`;
      tip.style.top = `${(Number(dot.getAttribute("cy")) / 260) * 100}%`;
      tip.classList.remove("hidden");
    });
    dot.addEventListener("mouseleave", () => tip.classList.add("hidden"));
  });
  document.getElementById("today-view-all").addEventListener("click", () => openOp("all-appointments"));
  document.getElementById("activities-view-all").addEventListener("click", renderActivitiesView);
  document.getElementById("promo-learn").addEventListener("click", renderSettings);
  view.querySelectorAll(".quick-btn[data-op]").forEach((b) => {
    b.addEventListener("click", () => { if (canUse(b.dataset.op)) openOp(b.dataset.op); });
  });
  renderBell();
}

async function showDashboard() {
  currentOp = "dashboard";
  currentView = "dashboard";
  setActiveNav("dashboard");
  showDashboardSection();
  const view = document.getElementById("dashboard-view");
  const res = await api("/api/dashboard");
  if (res && res._status === 401) {
    try { localStorage.removeItem("hms_token"); } catch (e) {}
    try { document.cookie = "hms_session=; path=/; max-age=0"; } catch (e) {}
    window.location.replace("/login.html?logout=1");
    return;
  }
  if (res && res.ok) {
    dashData = res.stats;
    renderDashboardView();
  } else {
    view.innerHTML = `${offlineBanner()}<p class="muted">Dashboard data could not load — ${esc(errOf(res))}</p>`;
  }
}

function updateClockChips() {
  const el = document.getElementById("dash-clock");
  if (el) el.textContent = new Date().toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
}

/* ---------------- Step 7: Departments / Settings / Soon / Activities */

function renderDepartments() {
  currentOp = "departments";
  currentView = "module";
  showModuleView();
  setModuleHead("Departments", "Departments come from doctor specializations");
  setActiveNav("departments");
  const specs = {};
  (state ? state.doctors : []).forEach((doc) => {
    const s = specs[doc.spec] || (specs[doc.spec] = { doctors: 0, waiting: 0 });
    s.doctors += 1;
    s.waiting += doc.queue.length;
  });
  const names = Object.keys(specs).sort();
  const totalAppts = names.reduce((n, k) => n + specs[k].waiting, 0);
  body.innerHTML = `
    ${state ? "" : offlineBanner()}
    ${names.length ? `<table class="tbl"><thead><tr>
        <th>Department</th><th>Doctors</th><th>Appointments Waiting</th><th>Share</th>
      </tr></thead><tbody>
      ${names.map((k) => `<tr><td>${esc(k)}</td><td>${specs[k].doctors}</td>
        <td>${specs[k].waiting}</td>
        <td>${totalAppts ? Math.round((100 * specs[k].waiting) / totalAppts) : 0}%</td></tr>`).join("")}
    </tbody></table>` : `<p class="muted">No doctors yet — add a doctor first and their specialization becomes a department.</p>`}
    <p class="muted" style="margin-top:12px">A separate Departments module (with its own list to choose from when booking) is a later step — for now, an appointment's department is its doctor's specialization.</p>`;
}

function renderSettings() {
  currentOp = "settings";
  currentView = "module";
  showModuleView();
  setModuleHead("Settings", "Hospital, appearance and account");
  setActiveNav("settings");
  const isAdmin = currentUser && currentUser.role === "Admin";
  body.innerHTML = `
    <div class="card"><h3>City Care Hospital</h3>
      <p>Management System — Version 1.0.0</p>
      <p class="muted">Patients: <b>${state ? state.patients.length : 0}</b> • Doctors: <b>${state ? state.doctors.length : 0}</b> • Data is stored in hospital.db on this computer.</p>
    </div>
    <div class="card"><h3>Appearance</h3>
      <div class="btn-row"><button type="button" id="set-theme" class="btn btn-primary">Switch to Dark Mode</button></div>
    </div>
    <div class="card"><h3>Account — ${esc(currentUser ? currentUser.username : "—")} (${esc(currentUser ? currentUser.role : "—")})</h3>
      <div class="btn-row">
        <button type="button" id="set-pw" class="btn btn-ghost">Change Password</button>
        ${isAdmin ? `<button type="button" id="set-users" class="btn btn-ghost">Manage Users</button>` : ""}
      </div>
    </div>`;
  document.getElementById("set-theme").addEventListener("click", () =>
    applyTheme(!document.body.classList.contains("dark")));
  document.getElementById("set-pw").addEventListener("click", () => openOp("change-password"));
  const usersBtn = document.getElementById("set-users");
  if (usersBtn) usersBtn.addEventListener("click", () => openOp("users"));
}

function openSoon(item) {
  currentOp = "soon";
  currentView = "module";
  showModuleView();
  setModuleHead(item.label, "Coming in a later step");
  setActiveNav(item.key);
  body.innerHTML = `
    <div class="card"><h3>${esc(item.label)}</h3><p>${esc(item.note || "This module is part of the plan and comes in a later step.")}</p></div>
    <div class="btn-row"><button type="button" id="soon-back" class="btn btn-primary">Back to Dashboard</button></div>`;
  document.getElementById("soon-back").addEventListener("click", showDashboard);
}

function renderActivitiesView() {
  currentOp = "activities";
  currentView = "module";
  showModuleView();
  setModuleHead("Recent Activities", "Latest status changes, registrations, bills and bookings");
  setActiveNav("dashboard");
  const acts = dashData ? dashData.activities : [];
  body.innerHTML = `
    ${acts.length ? `<ul class="activity-list">
      ${acts.map((a) => `<li><span class="act-dot act-${a.kind}"></span>
        <span class="act-text">${esc(a.text)}</span>
        <span class="act-when">${esc(a.when && a.when.length > 10 ? a.when : relTime(a.when))}</span></li>`).join("")}
    </ul>` : `<p class="muted">No activity yet.</p>`}
    <p class="muted">The feed shows the latest 8 events: appointment status changes (with who and when), patient registrations, bills and bookings — all from the live data.</p>`;
}

function describeUndo(last) {
  if (!last) return "Nothing to undo.";
  const kind = last[0];
  if (kind === "ADD_PATIENT") return `Add Patient — patient ID ${last[1]}`;
  if (kind === "ADD_NOTE") return `Add Note — patient ID ${last[1]}`;
  if (kind === "BOOK_APPT") return `Book Appointment — token ${last[2]} (doctor ${last[1]})`;
  if (kind === "BED") return `Bed ${last[1] + 1} — ${last[2] === 0 ? "was assigned" : "was freed (patient " + last[2] + ")"}`;
  return kind;
}

function renderUndo(msg = "", ok = true) {
  const last = state ? state.undo_last : null;
  body.innerHTML = `
    ${msgHtml(msg, ok)}
    ${state ? "" : offlineBanner()}
    <div class="card">
      <h3>Last action</h3>
      <p>${esc(describeUndo(last))}</p>
    </div>
    <div class="btn-row">
      <button type="button" id="btn-undo" class="btn btn-danger" ${last ? "" : "disabled"}>Undo Last Action ↩</button>
    </div>
    <p class="muted">Undo reverses the last add-patient, add-note, booking or bed change.</p>`;
  document.getElementById("btn-undo").addEventListener("click", doUndo);
}

async function doUndo() {
  const res = await api("/api/undo", "POST", {});
  await loadState();
  toast(res.ok ? res.message : errOf(res), !res.ok);
  if (currentOp === "undo") renderUndo(res.ok ? res.message : errOf(res), res.ok);
  else if (currentView === "dashboard") showDashboard();
  else if (OPS[currentOp]) openOp(currentOp);
  else showDashboard();
}

/* ------------------------------------------------------------------ Step 7: top search, bell, profile */

function showSearchPatient(pid) {
  const p = (state ? state.patients : []).find((x) => x.id === pid);
  currentOp = "find-patient";
  currentView = "module";
  showModuleView();
  setModuleHead("Search Result", "Patient found by search");
  setActiveNav("patients");
  if (!p) {
    body.innerHTML = `<div class="msg msg-err">Patient not found.</div>`;
    return;
  }
  body.innerHTML = patientCard(p);
  wirePatientCard(body, p);
}

function runSearch(q) {
  const box = document.getElementById("search-results");
  const input = document.getElementById("global-search");
  if (!q) { box.classList.add("hidden"); return; }
  const ql = q.toLowerCase();
  const patients = (state ? state.patients : []).filter((p) =>
    p.name.toLowerCase().includes(ql) || String(p.id) === q).slice(0, 5);
  const doctors = (state ? state.doctors : []).filter((d) =>
    d.name.toLowerCase().includes(ql) || d.id.toLowerCase() === ql ||
    (d.spec || "").toLowerCase().includes(ql)).slice(0, 3);
  const appts = [];
  (state ? state.doctors : []).forEach((d) => d.queue.forEach((a) => {
    if (a.token.toLowerCase() === ql || a.patient_name.toLowerCase().includes(ql)) {
      appts.push({ ...a, doctor_id: d.id, doctor_name: d.name, department: d.spec });
    }
  }));
  const found = appts.slice(0, 5);
  if (!patients.length && !doctors.length && !found.length) {
    box.innerHTML = `<div class="search-empty">No matches for “${esc(q)}”.</div>`;
  } else {
    box.innerHTML = `
      ${patients.length ? `<div class="search-sec">Patients</div>` + patients.map((p) =>
        `<button type="button" class="search-row" data-kind="patient" data-id="${p.id}">${esc(p.name)} <span class="muted">#${p.id} • ${esc(p.problem)}</span></button>`).join("") : ""}
      ${doctors.length ? `<div class="search-sec">Doctors</div>` + doctors.map((d) =>
        `<button type="button" class="search-row" data-kind="doctor" data-id="${esc(d.id)}">${esc(d.name)} <span class="muted">${esc(d.spec || "—")} • Rs ${d.fee ?? "—"}</span></button>`).join("") : ""}
      ${found.length ? `<div class="search-sec">Appointments</div>` + found.map((a) =>
        `<button type="button" class="search-row" data-kind="appt" data-doc="${esc(a.doctor_id || "")}" data-token="${esc(a.token)}" data-date="${esc(a.appt_date || "")}">${esc(a.token)} • ${esc(a.patient_name)} <span class="muted">${esc(a.doctor_name)}${a.appt_date ? " • " + esc(a.appt_date) : ""}</span></button>`).join("") : ""}`;
  }
  box.classList.remove("hidden");
  box.querySelectorAll(".search-row").forEach((row) => {
    row.addEventListener("click", () => {
      box.classList.add("hidden");
      input.value = "";
      if (row.dataset.kind === "patient") showSearchPatient(Number(row.dataset.id));
      else if (row.dataset.kind === "doctor") openOp("doctors");
      else {
        openOp("schedule");
        if (row.dataset.date) renderSchedule(row.dataset.date, row.dataset.doc || null);
      }
    });
  });
}

/* Step 12: the bell shows the REAL notifications — the badge is the
   unread count for the logged-in user, and the panel lists the
   newest first. Clicking one marks it read and opens its
   appointment's details page (when it has one). */
async function refreshBell() {
  const badge = document.getElementById("bell-badge");
  if (!badge) return;
  const res = await api("/api/notifications?limit=1");
  const n = res && res.ok ? res.unread_count
    : (dashData ? dashData.notification_count : 0);
  badge.textContent = n;
  badge.classList.toggle("hidden", !n);
}

function renderBell() {
  refreshBell();
}

const NOTIF_TYPE_LABELS = {
  booking: "Booking", cancellation: "Cancellation",
  reschedule: "Reschedule", status: "Status",
  reminder: "Reminder", schedule: "Schedule change",
};

function notifRowHtml(n) {
  const label = NOTIF_TYPE_LABELS[n.type] || n.type;
  return `<button type="button" class="notif-row${n.is_read ? "" : " notif-unread"}" data-notif-id="${n.id}" data-notif-apt="${esc(n.apt_id || "")}">
    <b>${esc(n.title)}</b> <span class="tag tag-nor">${esc(label)}</span>
    <span class="muted">${esc(n.message)}</span>
    <span class="muted">${esc(n.created_at || "")}</span></button>`;
}

async function openNotifRow(row) {
  const id = row.dataset.notifId;
  const apt = row.dataset.notifApt;
  await api(`/api/notifications/${id}/read`, "POST", {});
  document.getElementById("bell-panel").classList.add("hidden");
  refreshBell();
  if (apt) renderAppointmentDetail(apt);
}

async function fillBellPanel() {
  const panel = document.getElementById("bell-panel");
  const res = await api("/api/notifications?limit=15");
  if (!res || !res.ok) {
    panel.innerHTML = `<div class="pop-head">Notifications</div><p class="muted" style="padding:4px 14px 14px">${esc(errOf(res))}</p>`;
    return;
  }
  const rows = res.notifications || [];
  panel.innerHTML = `
    <div class="pop-head">Notifications (${res.unread_count} unread)</div>
    ${rows.length ? rows.map(notifRowHtml).join("") : `<p class="muted" style="padding:4px 14px 14px">All caught up — no notifications yet.</p>`}
    <div class="pop-foot">
      <button type="button" class="btn btn-ghost" id="bell-view-all">View all</button>
      <button type="button" class="btn btn-ghost" id="bell-read-all">Mark all read</button>
    </div>`;
  panel.querySelectorAll("[data-notif-id]").forEach((row) => row.addEventListener("click", () => openNotifRow(row)));
  document.getElementById("bell-view-all").addEventListener("click", () => {
    panel.classList.add("hidden");
    openOp("notifications");
  });
  document.getElementById("bell-read-all").addEventListener("click", async () => {
    await api("/api/notifications/read-all", "POST", {});
    refreshBell();
    fillBellPanel();
  });
}

async function toggleBellPanel() {
  const panel = document.getElementById("bell-panel");
  const wasHidden = panel.classList.contains("hidden");
  document.getElementById("profile-menu").classList.add("hidden");
  if (!wasHidden) { panel.classList.add("hidden"); return; }
  panel.innerHTML = `<div class="pop-head">Notifications</div><p class="muted" style="padding:4px 14px 14px">Loading…</p>`;
  panel.classList.remove("hidden");
  fillBellPanel();
}

/* Step 12: the Notifications view — filter by type, All / Unread,
   click one to mark it read (and open its appointment, when it has
   one), mark all read. */
async function renderNotifications(ntype, unreadOnly) {
  const type = ntype || "all";
  const unread = !!unreadOnly;
  const params = new URLSearchParams({ limit: 100 });
  if (type !== "all") params.set("type", type);
  if (unread) params.set("unread_only", "1");
  const res = await api(`/api/notifications?${params.toString()}`);
  if (!res || !res.ok) {
    body.innerHTML = `<div class="msg msg-err">${esc(errOf(res))}</div>`;
    return;
  }
  const rows = res.notifications || [];
  body.innerHTML = `
    <div class="field" style="margin-bottom:12px"><label>Filter</label>
      <div class="field-row">
        <select id="notif-type" style="flex:1">
          <option value="all" ${type === "all" ? "selected" : ""}>All types</option>
          ${["booking", "cancellation", "reschedule", "status", "reminder", "schedule"].map((t) => `<option value="${t}" ${type === t ? "selected" : ""}>${NOTIF_TYPE_LABELS[t]}</option>`).join("")}
        </select>
        <select id="notif-unread" style="flex:1">
          <option value="0" ${!unread ? "selected" : ""}>All</option>
          <option value="1" ${unread ? "selected" : ""}>Unread only</option>
        </select>
        <button type="button" id="notif-read-all" class="btn btn-ghost">Mark all read</button>
      </div>
    </div>
    <h3 style="margin:6px 0 8px">Notifications (${res.unread_count} unread)</h3>
    ${rows.length ? `<ul class="activity-list">
      ${rows.map((n) => `<li>${notifRowHtml(n)}</li>`).join("")}
    </ul>` : `<p class="muted">No notifications match this filter.</p>`}
    <p class="muted">Click a notification to mark it read — one with an appointment opens its details page.</p>`;
  document.getElementById("notif-type").addEventListener("change", (e) => {
    renderNotifications(e.target.value, document.getElementById("notif-unread").value === "1");
  });
  document.getElementById("notif-unread").addEventListener("change", (e) => {
    renderNotifications(document.getElementById("notif-type").value, e.target.value === "1");
  });
  document.getElementById("notif-read-all").addEventListener("click", async () => {
    await api("/api/notifications/read-all", "POST", {});
    refreshBell();
    renderNotifications(type, unread);
  });
  body.querySelectorAll("[data-notif-id]").forEach((row) => row.addEventListener("click", async () => {
    const id = row.dataset.notifId;
    const apt = row.dataset.notifApt;
    await api(`/api/notifications/${id}/read`, "POST", {});
    refreshBell();
    if (apt) renderAppointmentDetail(apt);
    else renderNotifications(type, unread);
  }));
}

async function doLogout() {
  await api("/api/logout", "POST", {});
  try { localStorage.removeItem("hms_token"); } catch (e) {}
  try { document.cookie = "hms_session=; path=/; max-age=0"; } catch (e) {}
  window.location.replace("/login.html?logout=1");
}

/* ------------------------------------------------------------------ users */

async function renderUsers(msg = "", ok = true) {
  const res = await api("/api/users");
  const users = res.ok ? res.users : [];
  body.innerHTML = `
    ${msgHtml(msg, ok)}
    ${res.ok ? "" : `<div class="msg msg-err">${esc(errOf(res))}</div>`}
    <h3 style="margin:0 0 8px">All Users (${users.length})</h3>
    ${users.length ? `<table class="tbl"><thead><tr>
        <th>Username</th><th>Role</th><th>Linked Doctor</th><th></th>
      </tr></thead><tbody>
      ${users.map((u) => `<tr><td>${esc(u.username)}</td><td>${esc(u.role)}</td>
        <td>${esc(u.doctor_id || "—")}</td>
        <td><button type="button" class="btn btn-ghost" data-reset="${esc(u.username)}" style="padding:6px 12px;font-size:13px">Reset Password</button></td>
      </tr>`).join("")}</tbody></table>` : `<p class="muted">No users.</p>`}
    <div id="reset-result" style="margin-top:10px"></div>
    <h3 style="margin:16px 0 8px">Add User</h3>
    <form id="f-user" class="form-grid">
      <div class="field-row">
        <div class="field"><label>Username</label><input type="text" name="username" placeholder="e.g. reception1" required></div>
        <div class="field"><label>Password</label><input type="password" name="password" placeholder="Password" required></div>
      </div>
      <div class="field-row">
        <div class="field"><label>Role</label>
          <select name="role" required>
            <option value="Receptionist">Receptionist</option>
            <option value="Doctor">Doctor</option>
            <option value="Admin">Admin</option>
          </select></div>
        <div class="field"><label>Linked Doctor ID (Doctors only)</label>
          <input type="text" name="doctor_id" placeholder="e.g. D1 (empty = all queues)"></div>
      </div>
      <div class="btn-row"><button type="submit" class="btn btn-primary">Add User +</button></div>
    </form>`;
  document.querySelectorAll("[data-reset]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const username = btn.dataset.reset;
      const np = prompt(`New password for ${username}:`);
      if (!np) return;
      const r = await api(`/api/users/${encodeURIComponent(username)}/reset`, "POST", { new_password: np });
      document.getElementById("reset-result").innerHTML =
        `<div class="msg ${r.ok ? "msg-ok" : "msg-err"}">${esc(r.ok ? r.message : errOf(r))}</div>`;
    });
  });
  document.getElementById("f-user").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const res2 = await api("/api/users", "POST", {
      username: f.get("username"), password: f.get("password"),
      role: f.get("role"), doctor_id: f.get("doctor_id"),
    });
    toast(res2.ok ? res2.message : errOf(res2), !res2.ok);
    renderUsers(res2.ok ? res2.message : errOf(res2), res2.ok);
  });
}

function renderChangePassword(msg = "", ok = true) {
  body.innerHTML = `
    ${msgHtml(msg, ok)}
    <form id="f-pw" class="form-grid">
      <div class="field"><label>Current Password</label>
        <input type="password" name="current_password" required></div>
      <div class="field"><label>New Password</label>
        <input type="password" name="new_password" required></div>
      <div class="field"><label>New Password Again</label>
        <input type="password" name="new_password_again" required></div>
      <div class="btn-row"><button type="submit" class="btn btn-primary">Change Password</button></div>
    </form>`;
  document.getElementById("f-pw").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const res = await api("/api/change-password", "POST", {
      current_password: f.get("current_password"),
      new_password: f.get("new_password"),
      new_password_again: f.get("new_password_again"),
    });
    toast(res.ok ? res.message : errOf(res), !res.ok);
    renderChangePassword(res.ok ? res.message : errOf(res), res.ok);
  });
}

/* ------------------------------------------------------------------ billing */

function billRow(b) {
  return `<tr><td>#${b.id}</td><td>${esc(b.date)}</td>
    <td>${esc(b.patient_name)} (#${b.patient_id})</td>
    <td>${esc(b.doctor_name)} ${esc(b.token)}</td>
    <td>Rs ${b.total}</td>
    <td>${b.status === "Paid"
      ? '<span class="tag tag-free">Paid</span>'
      : '<span class="tag tag-emg">Unpaid</span>'}</td>
    <td><button type="button" class="btn btn-ghost" data-view-bill="${b.id}"
      style="padding:6px 12px;font-size:13px">View</button></td></tr>`;
}

function billDetailHtml(b, canEdit) {
  return `<div class="card">
    <h3>Bill #${b.id} <span class="muted">${esc(b.date)} • ${b.status}</span></h3>
    <p>Patient: <b>${esc(b.patient_name)} (#${b.patient_id})</b> &nbsp;•&nbsp;
       Doctor: <b>${esc(b.doctor_name)}</b> &nbsp;•&nbsp; Token <b>${esc(b.token)}</b></p>
    <table class="tbl"><thead><tr><th>Charge</th><th>Amount (Rs)</th></tr></thead><tbody>
      ${b.items.map((it) => `<tr><td>${esc(it.label)}</td><td>${it.amount}</td></tr>`).join("")}
      <tr><td><b>Total</b></td><td><b>Rs ${b.total}</b></td></tr>
    </tbody></table>
    ${canEdit ? `
    <form id="f-charge" class="form-grid" style="margin-top:12px">
      <div class="field-row">
        <div class="field"><label>Extra charge</label>
          <input type="text" name="label" placeholder="e.g. Medicine — Paracetamol" required></div>
        <div class="field"><label>Amount (Rs)</label>
          <input type="number" name="amount" min="1" placeholder="e.g. 350" required></div>
      </div>
      <div class="btn-row"><button type="submit" class="btn btn-primary">Add Charge +</button></div>
    </form>
    <div class="btn-row" style="margin-top:10px">
      <button type="button" id="btn-pay" class="btn btn-green">Mark Paid</button>
      <button type="button" id="btn-unpay" class="btn btn-ghost">Mark Unpaid</button>
    </div>` : `<p class="muted">Doctors can view bills only — only Admin / Receptionist can change them.</p>`}
  </div>`;
}

async function renderBilling(msg = "", ok = true) {
  const canEdit = currentUser && ["Admin", "Receptionist"].includes(currentUser.role);
  const res = await api("/api/bills");
  const bills = res.ok ? res.bills : [];
  const docs = state ? state.doctors : [];
  body.innerHTML = `
    ${msgHtml(msg, ok)}
    ${res.ok ? "" : `<div class="msg msg-err">${esc(errOf(res))}</div>`}
    ${canEdit ? `
    <h3 style="margin:0 0 8px">Create Bill from Appointment</h3>
    <form id="f-bill" class="form-grid">
      <div class="field-row">
        <div class="field"><label>Patient ID</label>
          <input type="number" name="patient_id" min="1" placeholder="e.g. 101" required></div>
        <div class="field"><label>Doctor</label>
          <select name="doctor_id" required>
            ${docs.map((d) => `<option value="${esc(d.id)}">${esc(d.name)} — ${esc(d.spec || "—")}</option>`).join("")}
          </select></div>
        <div class="field"><label>Token</label>
          <input type="text" name="token" placeholder="e.g. N-1" required></div>
      </div>
      <div class="btn-row"><button type="submit" class="btn btn-primary">Create Bill +</button></div>
    </form>` : `<p class="muted">Only Admin / Receptionist can create bills — Doctors view them here.</p>`}
    <h3 style="margin:16px 0 8px">All Bills (${bills.length})</h3>
    ${bills.length ? `<table class="tbl"><thead><tr>
        <th>Bill</th><th>Date</th><th>Patient</th><th>Doctor</th><th>Total</th><th>Status</th><th></th>
      </tr></thead><tbody>${bills.map(billRow).join("")}</tbody></table>`
      : `<p class="muted">No bills yet. Book an appointment first, then create a bill from its token.</p>`}
    <div class="btn-row" style="margin-top:12px">
      <a class="btn btn-ghost" href="/api/bills/export" style="text-decoration:none">Download Bills CSV ↓</a>
    </div>
    <div id="bill-detail" style="margin-top:14px"></div>`;
  const billForm = document.getElementById("f-bill");
  if (billForm) {
    billForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      const r = await api("/api/bills", "POST", {
        patient_id: Number(f.get("patient_id")),
        doctor_id: f.get("doctor_id"), token: f.get("token"),
      });
      await loadState();
      toast(r.ok ? r.message : errOf(r), !r.ok);
      renderBilling(r.ok ? r.message : errOf(r), r.ok);
    });
  }
  document.querySelectorAll("[data-view-bill]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const r = await api(`/api/bills/${btn.dataset.viewBill}`);
      const detail = document.getElementById("bill-detail");
      if (!r.ok) {
        detail.innerHTML = `<div class="msg msg-err">${esc(errOf(r))}</div>`;
        return;
      }
      detail.innerHTML = billDetailHtml(r.bill, canEdit);
      const chargeForm = document.getElementById("f-charge");
      if (chargeForm) {
        chargeForm.addEventListener("submit", async (e) => {
          e.preventDefault();
          const f = new FormData(e.target);
          const r2 = await api(`/api/bills/${r.bill.id}/charges`, "POST", {
            label: f.get("label"), amount: Number(f.get("amount")),
          });
          toast(r2.ok ? r2.message : errOf(r2), !r2.ok);
          renderBilling(r2.ok ? r2.message : errOf(r2), r2.ok);
        });
      }
      const payBtn = document.getElementById("btn-pay");
      if (payBtn) {
        payBtn.addEventListener("click", async () => {
          const r2 = await api(`/api/bills/${r.bill.id}/pay`, "POST", {});
          toast(r2.ok ? r2.message : errOf(r2), !r2.ok);
          renderBilling(r2.ok ? r2.message : errOf(r2), r2.ok);
        });
      }
      const unpayBtn = document.getElementById("btn-unpay");
      if (unpayBtn) {
        unpayBtn.addEventListener("click", async () => {
          const r2 = await api(`/api/bills/${r.bill.id}/unpay`, "POST", {});
          toast(r2.ok ? r2.message : errOf(r2), !r2.ok);
          renderBilling(r2.ok ? r2.message : errOf(r2), r2.ok);
        });
      }
    });
  });
}

/* ------------------------------------------------------------------ reports */

/* Step 12: Reports & Analytics — daily / weekly / monthly figures
   from the server, with doctor + department filters, charts and
   CSV/PDF export of the current report. (The old Step 3 daily
   summary still lives on GET /api/reports and in the CLI.) */
function barChartSVG(perDay) {
  const rowH = 24, PT = 8, labelW = 90, maxW = 420;
  const H = PT + perDay.length * rowH + 8;
  const maxV = Math.max(1, ...perDay.map((d) => d.count));
  const rows = perDay.map((d, i) => {
    const y = PT + i * rowH;
    const w = (d.count / maxV) * maxW;
    return `<text x="0" y="${y + 15}" font-size="11" fill="#64748b">${esc(d.date.slice(5))}</text>
      <rect x="${labelW}" y="${y + 4}" width="${w}" height="14" rx="3" fill="#1769AA"/>
      <text x="${labelW + w + 8}" y="${y + 15}" font-size="11" fill="#172033">${d.count}</text>`;
  }).join("");
  return `<svg viewBox="0 0 ${labelW + maxW + 40} ${H}" class="chart-svg" role="img" aria-label="Appointments per day bar chart">${rows}</svg>`;
}

async function renderReports() {
  const docs = state ? state.doctors : [];
  const depts = [...new Set(docs.map((d) => d.spec).filter(Boolean))];
  const now = new Date();
  const todayLocal = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  body.innerHTML = `
    <div class="field" style="margin-bottom:12px"><label>Report</label>
      <div class="field-row">
        <select id="rep-period" style="flex:1">
          <option value="daily">Daily</option>
          <option value="weekly">Weekly</option>
          <option value="monthly">Monthly</option>
        </select>
        <input type="date" id="rep-date" value="${todayLocal}" style="flex:1">
        <select id="rep-doctor" style="flex:1">
          <option value="">All doctors</option>
          ${docs.map((d) => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join("")}
        </select>
        <select id="rep-dept" style="flex:1">
          <option value="">All departments</option>
          ${depts.map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join("")}
        </select>
        <button type="button" id="rep-load" class="btn btn-primary">Load Report</button>
      </div>
    </div>
    <div id="rep-body"><p class="muted">Loading report…</p></div>`;
  const load = async () => {
    const period = document.getElementById("rep-period").value;
    const date = document.getElementById("rep-date").value;
    const doctorId = document.getElementById("rep-doctor").value;
    const dept = document.getElementById("rep-dept").value;
    const params = new URLSearchParams({ period });
    if (date) params.set("date", date);
    if (doctorId) params.set("doctor_id", doctorId);
    if (dept) params.set("department", dept);
    const res = await api(`/api/reports/analytics?${params.toString()}`);
    const box = document.getElementById("rep-body");
    if (!box) return;
    if (!res || !res.ok) {
      box.innerHTML = `<div class="msg msg-err">${esc(errOf(res))}</div>`;
      return;
    }
    const r = res.report;
    const qs = params.toString();
    box.innerHTML = `
    <h3 style="margin:6px 0 8px">Appointments Report — ${esc(r.period[0].toUpperCase() + r.period.slice(1))} ${esc(r.from)} to ${esc(r.to)}</h3>
    <div class="stats" style="grid-template-columns: repeat(2, 1fr); margin-bottom: 14px">
      <div class="stat-card stat-blue"><div class="stat-text"><span class="stat-label">Total Appointments</span><span class="stat-num">${r.total}</span></div></div>
      <div class="stat-card stat-teal"><div class="stat-text"><span class="stat-label">Completed</span><span class="stat-num">${r.completed} (${r.completion_rate}%)</span></div></div>
      <div class="stat-card stat-blue"><div class="stat-text"><span class="stat-label">Cancelled</span><span class="stat-num">${r.cancelled} (${r.cancellation_rate}%)</span></div></div>
      <div class="stat-card stat-teal"><div class="stat-text"><span class="stat-label">No Show</span><span class="stat-num">${r.no_show} (${r.no_show_rate}%)</span></div></div>
      <div class="stat-card stat-blue"><div class="stat-text"><span class="stat-label">Revenue (Paid Bills)</span><span class="stat-num">Rs ${r.revenue}</span></div></div>
      <div class="stat-card stat-teal"><div class="stat-text"><span class="stat-label">Unpaid in Period</span><span class="stat-num">Rs ${r.unpaid_total}</span></div></div>
    </div>
    <div class="btn-row" style="margin-bottom:14px">
      <a class="btn btn-ghost" href="/api/reports/analytics.csv?${qs}" style="text-decoration:none">Export CSV ↓</a>
      <a class="btn btn-ghost" href="/api/reports/analytics.pdf?${qs}" style="text-decoration:none">Export PDF ↓</a>
    </div>
    <h3 style="margin:14px 0 8px">By Status</h3>
    <table class="tbl"><thead><tr><th>Status</th><th>Count</th></tr></thead><tbody>
      ${["Pending", "Confirmed", "Checked In", "In Progress", "Completed", "Cancelled", "No Show"].map((s) => `<tr><td><span class="tag ${statusTagClass(s)}">${esc(s)}</span></td><td>${r.by_status[s]}</td></tr>`).join("")}
    </tbody></table>
    <h3 style="margin:14px 0 8px">Appointments per Day</h3>
    ${r.per_day.length ? barChartSVG(r.per_day) : `<p class="muted">No days in this period.</p>`}
    <h3 style="margin:14px 0 8px">Department Statistics</h3>
    ${r.per_department.length ? `<div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
      ${donutSVG(r.per_department.map((d) => ({ name: d.name, count: d.appointments, percent: r.total ? Math.round(100 * d.appointments / r.total) : 0 })))}
      <ul class="dept-list">${r.per_department.map((d, i) => `<li><span class="dot" style="background:${CHART_COLORS[i % CHART_COLORS.length]}"></span>${esc(d.name)}<b>${d.appointments}</b></li>`).join("")}</ul>
    </div>` : `<p class="muted">No appointments in this period.</p>`}
    <h3 style="margin:14px 0 8px">Per Doctor</h3>
    <table class="tbl"><thead><tr><th>Doctor</th><th>Department</th><th>Appointments</th><th>Completed</th><th>Cancelled</th><th>No Show</th></tr></thead><tbody>
      ${r.per_doctor.map((d) => `<tr><td>${esc(d.name)} (${esc(d.doctor_id)})</td><td>${esc(d.department)}</td><td>${d.appointments}</td><td>${d.completed}</td><td>${d.cancelled}</td><td>${d.no_show}</td></tr>`).join("")}
    </tbody></table>
    <h3 style="margin:14px 0 8px">Per Department</h3>
    <table class="tbl"><thead><tr><th>Department</th><th>Appointments</th><th>Completed</th><th>Cancelled</th><th>No Show</th></tr></thead><tbody>
      ${r.per_department.map((d) => `<tr><td>${esc(d.name)}</td><td>${d.appointments}</td><td>${d.completed}</td><td>${d.cancelled}</td><td>${d.no_show}</td></tr>`).join("")}
    </tbody></table>
    <h3 style="margin:14px 0 8px">Cancelled Appointments (${r.cancelled_list.length})</h3>
    ${r.cancelled_list.length ? `<table class="tbl"><thead><tr><th>Appt ID</th><th>Patient</th><th>Doctor</th><th>Date</th><th>Time</th></tr></thead><tbody>
      ${r.cancelled_list.map((a) => `<tr><td>${esc(a.apt_id || "—")}</td><td>${esc(a.patient_name)}</td><td>${esc(a.doctor_name)}</td><td>${esc(a.appt_date || "—")}</td><td>${esc(a.appt_time || "—")}</td></tr>`).join("")}
    </tbody></table>` : `<p class="muted">None in this period.</p>`}
    <h3 style="margin:14px 0 8px">No-Show Appointments (${r.no_show_list.length})</h3>
    ${r.no_show_list.length ? `<table class="tbl"><thead><tr><th>Appt ID</th><th>Patient</th><th>Doctor</th><th>Date</th><th>Time</th></tr></thead><tbody>
      ${r.no_show_list.map((a) => `<tr><td>${esc(a.apt_id || "—")}</td><td>${esc(a.patient_name)}</td><td>${esc(a.doctor_name)}</td><td>${esc(a.appt_date || "—")}</td><td>${esc(a.appt_time || "—")}</td></tr>`).join("")}
    </tbody></table>` : `<p class="muted">None in this period.</p>`}`;
  };
  document.getElementById("rep-load").addEventListener("click", load);
  load();
}

/* Step 4 (Schedule): appointments for one date, one doctor or all.
   Sorted by time, then Emergency-first — the server already sorts. */
async function renderSchedule(date, doctorId) {
  const now = new Date();
  const todayLocal = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  const day = date || todayLocal;
  const docs = state ? state.doctors : [];
  const params = new URLSearchParams({ date: day });
  if (doctorId) params.set("doctor_id", doctorId);
  const res = await api(`/api/appointments?${params.toString()}`);
  if (!res.ok) {
    body.innerHTML = `<div class="msg msg-err">${esc(errOf(res))}</div>`;
    return;
  }
  const rows = res.appointments || [];
  body.innerHTML = `
    <div class="field" style="margin-bottom:12px"><label>Date</label>
      <div class="field-row">
        <input type="date" id="sch-date" value="${esc(day)}" style="flex:1">
        <select id="sch-doctor" style="flex:1">
          <option value="">All doctors</option>
          ${docs.map((d) => `<option value="${esc(d.id)}" ${doctorId === d.id ? "selected" : ""}>${esc(d.name)}</option>`).join("")}
        </select>
        <button type="button" id="sch-load" class="btn btn-primary">Show</button>
      </div>
    </div>
    <h3 style="margin:6px 0 8px">Schedule — ${esc(res.date)} (${rows.length} appointment${rows.length === 1 ? "" : "s"})</h3>
    ${rows.length ? `<table class="tbl"><thead><tr>
        <th>Time</th><th>Token</th><th>Patient</th><th>Doctor</th><th>Status</th><th>Fee</th>
      </tr></thead><tbody>
      ${rows.map((a) => `<tr><td>${a.appt_time ? esc(a.appt_time) : "—"}</td>
        <td>${esc(a.token)} ${a.emergency ? '<span class="tag tag-emg">EMERGENCY</span>' : ""}</td>
        <td>${esc(a.patient_name)} (#${a.patient_id})</td>
        <td>${esc(a.doctor_name)}</td><td><span class="tag ${statusTagClass(a.status || "Pending")}">${esc(a.status || "Pending")}</span></td><td>Rs ${a.fee}</td></tr>`).join("")}
    </tbody></table>`
      : `<p class="muted">No appointments on this date.</p>`}`;
  document.getElementById("sch-load").addEventListener("click", () => {
    const picked = document.getElementById("sch-date").value;
    if (!picked) { toast("Pick a date first.", true); return; }
    renderSchedule(picked, document.getElementById("sch-doctor").value || null);
  });
}

/* ---------------- Step 10: appointment calendar ---------------- */

/* Calendar state — which view, which day the view is anchored on
   (a date inside the shown day / week / month), and the
   doctor/department filters. The server returns the appointments
   for the shown range; this only groups and draws them. */
let calState = { view: "month", anchor: null, doctor_id: "", department: "" };

const CAL_MONTHS = ["January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"];
const CAL_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
  "Saturday", "Sunday"];
function calTodayLocal() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}
function calParseISO(s) {
  const [y, m, d] = String(s).split("-").map(Number);
  return new Date(y, (m || 1) - 1, d || 1);
}
function calISO(dt) {
  return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
}
function calAddDays(dt, n) {
  const d = new Date(dt);
  d.setDate(d.getDate() + n);
  return d;
}
/* The date range the current view shows, plus its title. Weeks start
   on Monday (the hospital's week), months run 1st to last day. */
function calRange() {
  const anchor = calParseISO(calState.anchor || calTodayLocal());
  if (calState.view === "day") {
    return { from: calState.anchor, to: calState.anchor,
      title: `${CAL_DAYS[(anchor.getDay() + 6) % 7]}, ${anchor.getDate()} ${CAL_MONTHS[anchor.getMonth()]} ${anchor.getFullYear()}` };
  }
  if (calState.view === "week") {
    const from = calAddDays(anchor, -((anchor.getDay() + 6) % 7));
    const to = calAddDays(from, 6);
    return { from: calISO(from), to: calISO(to),
      title: `${from.getDate()} – ${to.getDate()} ${CAL_MONTHS[to.getMonth()]} ${to.getFullYear()}` };
  }
  const from = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
  const to = new Date(anchor.getFullYear(), anchor.getMonth() + 1, 0);
  return { from: calISO(from), to: calISO(to),
    title: `${CAL_MONTHS[anchor.getMonth()]} ${anchor.getFullYear()}` };
}
function calShift(dir) {
  const anchor = calParseISO(calState.anchor || calTodayLocal());
  let next = anchor;
  if (calState.view === "day") next = calAddDays(anchor, dir);
  else if (calState.view === "week") next = calAddDays(anchor, 7 * dir);
  else next = new Date(anchor.getFullYear(), anchor.getMonth() + dir, 1);
  calState.anchor = calISO(next);
  renderCalendar();
}
/* An empty calendar day opens the Step 8 booking form with that
   date already filled in (a Doctor sees the role message instead —
   booking is Admin/Receptionist only). */
function calBookOn(dateStr) {
  bookPrefillDate = dateStr;
  openOp("book-appointment");
}
/* One appointment as a clickable chip — the status pill uses the
   same colours as the dashboard chips. Cancelled / No Show entries
   are dimmed so the day's real picture stays visible. */
function calChip(a) {
  const dim = ["Cancelled", "No Show"].includes(a.status) ? " cal-dim" : "";
  return `<button type="button" class="cal-chip${dim}" data-ref="${esc(apptRef(a))}"
      title="${esc(a.patient_name)} — ${esc(a.doctor_name)} (${esc(a.status)})">
    <span class="cal-chip-time">${a.appt_time ? esc(a.appt_time) : "No time"}</span>
    <span class="cal-chip-name">${esc(a.patient_name)}</span>
    <span class="tag ${statusTagClass(a.status || "Pending")}">${esc(a.status || "Pending")}</span>
  </button>`;
}
function calWireChips(root) {
  root.querySelectorAll(".cal-chip").forEach((el) =>
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      renderAppointmentDetail(el.dataset.ref, false);
    }));
}

async function renderCalendar() {
  if (!calState.anchor) calState.anchor = calTodayLocal();
  const docs = state ? state.doctors : [];
  const departments = [...new Set(docs.map((d) => d.spec).filter(Boolean))].sort();
  const range = calRange();
  const params = new URLSearchParams({ from: range.from, to: range.to });
  if (calState.doctor_id) params.set("doctor_id", calState.doctor_id);
  if (calState.department) params.set("department", calState.department);
  const res = await api(`/api/appointments/calendar?${params.toString()}`);
  if (!res.ok) {
    body.innerHTML = `<div class="msg msg-err">${esc(errOf(res))}</div>`;
    return;
  }
  const rows = res.appointments || [];
  const byDate = {};
  for (const a of rows) {
    (byDate[a.appt_date] = byDate[a.appt_date] || []).push(a);
  }
  const today = calTodayLocal();
  const viewBtn = (v, label) => `<button type="button" class="btn ${calState.view === v ? "btn-primary" : "btn-ghost"}" data-cal-view="${v}">${label}</button>`;
  let gridHtml = "";
  if (calState.view === "day") {
    const dayRows = byDate[range.from] || [];
    const timed = dayRows.filter((a) => a.appt_time);
    const untimed = dayRows.filter((a) => !a.appt_time);
    const rowHtml = (a) => `<div class="cal-day-row">
        <span class="cal-day-time">${a.appt_time ? esc(a.appt_time) : "No fixed time"}</span>
        <span>${esc(a.patient_name)} (#${a.patient_id}) — ${esc(a.doctor_name)} (${esc(a.department)})</span>
        <span>${esc(a.appt_type || "General")}${a.emergency ? ' <span class="tag tag-emg">EMERGENCY</span>' : ""}</span>
        <span class="tag ${statusTagClass(a.status || "Pending")}">${esc(a.status || "Pending")}</span>
        <button type="button" class="btn btn-ghost" data-ref="${esc(apptRef(a))}" data-cal-open="1">View</button>
      </div>`;
    gridHtml = `
      ${timed.length ? timed.map(rowHtml).join("") : `<p class="muted">No timed appointments on this day.</p>`}
      ${untimed.length ? `<h4 style="margin:14px 0 6px">No fixed time</h4>${untimed.map(rowHtml).join("")}` : ""}
      <div class="btn-row" style="margin-top:14px">
        <button type="button" class="btn btn-primary" id="cal-book-day">+ Book Appointment on ${esc(range.from)}</button>
      </div>`;
  } else if (calState.view === "week") {
    const from = calParseISO(range.from);
    let cols = "";
    for (let i = 0; i < 7; i++) {
      const d = calAddDays(from, i);
      const key = calISO(d);
      const dayRows = byDate[key] || [];
      cols += `<div class="cal-col ${key === today ? "cal-today" : ""}">
        <div class="cal-col-head"><b>${CAL_DAYS[i].slice(0, 3)}</b> ${d.getDate()} ${CAL_MONTHS[d.getMonth()].slice(0, 3)}</div>
        ${dayRows.map(calChip).join("") || `<p class="muted">—</p>`}
        <button type="button" class="btn btn-ghost cal-book-col" data-date="${key}">+ Book</button>
      </div>`;
    }
    gridHtml = `<div class="cal-week">${cols}</div>`;
  } else {
    const anchor = calParseISO(calState.anchor);
    const first = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
    const lastDay = new Date(anchor.getFullYear(), anchor.getMonth() + 1, 0).getDate();
    const lead = (first.getDay() + 6) % 7; /* Monday-first grid */
    let cells = "";
    for (let i = 0; i < lead; i++) cells += `<div class="cal-cell cal-blank"></div>`;
    for (let day = 1; day <= lastDay; day++) {
      const key = `${anchor.getFullYear()}-${String(anchor.getMonth() + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
      const dayRows = byDate[key] || [];
      const shown = dayRows.slice(0, 3);
      const more = dayRows.length - shown.length;
      cells += `<div class="cal-cell ${key === today ? "cal-today" : ""}" data-date="${key}" title="Book on ${key}">
        <div class="cal-daynum">${day}</div>
        ${shown.map(calChip).join("")}
        ${more > 0 ? `<button type="button" class="cal-more" data-day="${key}">+${more} more</button>` : ""}
      </div>`;
    }
    gridHtml = `<div class="cal-grid">
      ${["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => `<div class="cal-head">${d}</div>`).join("")}
      ${cells}
    </div>`;
  }
  body.innerHTML = `
    ${state ? "" : offlineBanner()}
    <div class="cal-toolbar">
      <div class="btn-row" style="gap:6px">
        ${viewBtn("day", "Day")}${viewBtn("week", "Week")}${viewBtn("month", "Month")}
      </div>
      <div class="btn-row" style="gap:6px">
        <button type="button" class="btn btn-ghost" id="cal-prev">← Prev</button>
        <button type="button" class="btn btn-ghost" id="cal-today">Today</button>
        <button type="button" class="btn btn-ghost" id="cal-next">Next →</button>
      </div>
      <div class="field-row" style="flex:1;min-width:220px">
        <div class="field"><label>Jump to date</label>
          <input type="date" id="cal-jump" value="${esc(calState.anchor)}"></div>
        <div class="field"><label>Doctor</label>
          <select id="cal-doctor">
            <option value="">All doctors</option>
            ${docs.map((d) => `<option value="${esc(d.id)}" ${calState.doctor_id === d.id ? "selected" : ""}>${esc(d.name)}</option>`).join("")}
          </select></div>
        <div class="field"><label>Department</label>
          <select id="cal-dept">
            <option value="">All departments</option>
            ${departments.map((d) => `<option ${calState.department === d ? "selected" : ""}>${esc(d)}</option>`).join("")}
          </select></div>
      </div>
    </div>
    <h3 style="margin:10px 0 4px">${esc(range.title)} — ${rows.length} appointment${rows.length === 1 ? "" : "s"}</h3>
    <div class="cal-legend">${APPOINTMENT_STATUSES.map((s) => `<span class="tag ${statusTagClass(s)}">${s}</span>`).join("")}
      <span class="muted">Click an appointment to open its details. Click an empty day to book on that date.</span></div>
    <div style="margin-top:10px">${gridHtml}</div>`;
  document.querySelectorAll("[data-cal-view]").forEach((btn) =>
    btn.addEventListener("click", () => { calState.view = btn.dataset.calView; renderCalendar(); }));
  document.getElementById("cal-prev").addEventListener("click", () => calShift(-1));
  document.getElementById("cal-next").addEventListener("click", () => calShift(1));
  document.getElementById("cal-today").addEventListener("click", () => { calState.anchor = calTodayLocal(); renderCalendar(); });
  document.getElementById("cal-jump").addEventListener("change", (e) => {
    if (e.target.value) { calState.anchor = e.target.value; renderCalendar(); }
  });
  document.getElementById("cal-doctor").addEventListener("change", (e) => { calState.doctor_id = e.target.value; renderCalendar(); });
  document.getElementById("cal-dept").addEventListener("change", (e) => { calState.department = e.target.value; renderCalendar(); });
  calWireChips(body);
  body.querySelectorAll("[data-cal-open]").forEach((btn) =>
    btn.addEventListener("click", () => renderAppointmentDetail(btn.dataset.ref, false)));
  body.querySelectorAll(".cal-book-col").forEach((btn) =>
    btn.addEventListener("click", () => calBookOn(btn.dataset.date)));
  body.querySelectorAll(".cal-more").forEach((btn) =>
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      calState.view = "day";
      calState.anchor = btn.dataset.day;
      renderCalendar();
    }));
  body.querySelectorAll(".cal-cell[data-date]").forEach((cell) =>
    cell.addEventListener("click", (e) => {
      if (e.target.closest(".cal-chip") || e.target.closest(".cal-more")) return;
      calBookOn(cell.dataset.date);
    }));
  const bookDay = document.getElementById("cal-book-day");
  if (bookDay) bookDay.addEventListener("click", () => calBookOn(range.from));
}

/* ---------------- Step 9: appointments table + details ---------------- */

/* Table state — the filters, sort and page the table is showing.
   Filtering/sorting/pagination all happen on the server
   (GET /api/appointments/list); this only remembers what to ask for. */
let apptFilters = { search: "", date: "", doctor_id: "", department: "",
                    status: "", sort: "date", order: "asc", page: 1 };
const APPT_PER_PAGE = 10;
const APPT_SORTS = ["date", "time", "patient_name", "doctor_name", "status"];

/* The reference the details page opens by — the APT ID, or
   "D1:N-1" (doctor + token) for appointments booked before Step 8
   that never got an APT ID. */
function apptRef(a) {
  return a.apt_id || `${a.doctor_id}:${a.token}`;
}

function apptListParams(includePage) {
  const p = new URLSearchParams();
  for (const key of ["search", "date", "doctor_id", "department", "status", "sort", "order"]) {
    if (apptFilters[key]) p.set(key, apptFilters[key]);
  }
  if (includePage) {
    p.set("page", String(apptFilters.page));
    p.set("per_page", String(APPT_PER_PAGE));
  }
  return p;
}

async function renderAllAppointments() {
  const docs = state ? state.doctors : [];
  const departments = [...new Set(docs.map((d) => d.spec).filter(Boolean))].sort();
  const res = await api(`/api/appointments/list?${apptListParams(true).toString()}`);
  if (!res.ok) {
    body.innerHTML = `<div class="msg msg-err">${esc(errOf(res))}</div>`;
    return;
  }
  const rows = res.appointments || [];
  const total = res.total || 0;
  const page = res.page || 1;
  const pages = res.pages || 1;
  const canEdit = currentUser && ["Admin", "Receptionist"].includes(currentUser.role);
  const exportQs = apptListParams(false).toString();
  const sortArrow = (col) => apptFilters.sort === col
    ? (apptFilters.order === "asc" ? " ▲" : " ▼") : "";
  const th = (col, label) => `<th data-sort="${col}" style="cursor:pointer;white-space:nowrap">${label}${sortArrow(col)}</th>`;
  body.innerHTML = `
    ${state ? "" : offlineBanner()}
    <div class="form-grid" style="margin-bottom:12px">
      <div class="field-row">
        <div class="field"><label>Search (patient, APT ID, token)</label>
          <input type="text" id="appt-search" placeholder="e.g. Ahmed, APT-0001, N-1" value="${esc(apptFilters.search)}"></div>
        <div class="field"><label>Date</label>
          <input type="date" id="appt-date" value="${esc(apptFilters.date)}"></div>
      </div>
      <div class="field-row">
        <div class="field"><label>Doctor</label>
          <select id="appt-doctor">
            <option value="">All doctors</option>
            ${docs.map((d) => `<option value="${esc(d.id)}" ${apptFilters.doctor_id === d.id ? "selected" : ""}>${esc(d.name)}</option>`).join("")}
          </select></div>
        <div class="field"><label>Department</label>
          <select id="appt-dept">
            <option value="">All departments</option>
            ${departments.map((d) => `<option ${apptFilters.department === d ? "selected" : ""}>${esc(d)}</option>`).join("")}
          </select></div>
        <div class="field"><label>Status</label>
          <select id="appt-status">
            <option value="">All statuses</option>
            ${APPOINTMENT_STATUSES.map((s) => `<option ${apptFilters.status === s ? "selected" : ""}>${s}</option>`).join("")}
          </select></div>
      </div>
      <div class="btn-row">
        <button type="button" class="btn btn-primary" id="appt-apply">Apply Filters</button>
        <button type="button" class="btn btn-ghost" id="appt-clear">Clear</button>
        <span style="flex:1"></span>
        <a class="btn btn-ghost" href="/api/appointments/export.csv?${exportQs}" style="text-decoration:none">Export CSV ↓</a>
        <a class="btn btn-ghost" href="/api/appointments/export.pdf?${exportQs}" style="text-decoration:none">Export PDF ↓</a>
      </div>
    </div>
    <h3 style="margin:6px 0 8px">Appointments — ${total} found</h3>
    ${rows.length ? `<table class="tbl"><thead><tr>
        <th>Appt ID</th>
        ${th("patient_name", "Patient")}${th("doctor_name", "Doctor")}<th>Department</th>
        ${th("date", "Date")}${th("time", "Time")}<th>Type</th>
        ${th("status", "Status")}<th>Actions</th>
      </tr></thead><tbody>
      ${rows.map((a) => `<tr>
        <td>${esc(a.apt_id || "—")}</td>
        <td>${esc(a.patient_name)} (#${a.patient_id})</td>
        <td>${esc(a.doctor_name)}</td>
        <td>${esc(a.department)}</td>
        <td>${esc(a.appt_date || "—")}</td>
        <td>${esc(a.appt_time || "—")}</td>
        <td>${esc(a.appt_type || "General")}${a.emergency ? ' <span class="tag tag-emg">EMERGENCY</span>' : ""}</td>
        <td><span class="tag ${statusTagClass(a.status || "Pending")}">${esc(a.status || "Pending")}</span></td>
        <td style="white-space:nowrap">
          <button type="button" class="btn btn-ghost" data-act="view" data-ref="${esc(apptRef(a))}">View</button>
          ${canEdit ? `<button type="button" class="btn btn-ghost" data-act="edit" data-ref="${esc(apptRef(a))}">Edit</button>
          <button type="button" class="btn btn-danger" data-act="cancel" data-ref="${esc(apptRef(a))}" data-doc="${esc(a.doctor_id)}" data-token="${esc(a.token)}">Cancel</button>` : ""}
        </td></tr>`).join("")}
    </tbody></table>`
      : `<p class="muted">No appointments match these filters.</p>`}
    <div class="btn-row" style="margin-top:12px;align-items:center">
      <button type="button" class="btn btn-ghost" id="appt-prev" ${page <= 1 ? "disabled" : ""}>← Previous</button>
      <span class="muted">Page ${page} of ${pages} — ${total} appointment${total === 1 ? "" : "s"}</span>
      <button type="button" class="btn btn-ghost" id="appt-next" ${page >= pages ? "disabled" : ""}>Next →</button>
    </div>`;
  document.getElementById("appt-apply").addEventListener("click", () => {
    apptFilters.search = document.getElementById("appt-search").value.trim();
    apptFilters.date = document.getElementById("appt-date").value;
    apptFilters.doctor_id = document.getElementById("appt-doctor").value;
    apptFilters.department = document.getElementById("appt-dept").value;
    apptFilters.status = document.getElementById("appt-status").value;
    apptFilters.page = 1;
    renderAllAppointments();
  });
  document.getElementById("appt-clear").addEventListener("click", () => {
    apptFilters = { search: "", date: "", doctor_id: "", department: "",
                    status: "", sort: "date", order: "asc", page: 1 };
    renderAllAppointments();
  });
  document.getElementById("appt-prev").addEventListener("click", () => {
    if (apptFilters.page > 1) { apptFilters.page -= 1; renderAllAppointments(); }
  });
  document.getElementById("appt-next").addEventListener("click", () => {
    if (apptFilters.page < pages) { apptFilters.page += 1; renderAllAppointments(); }
  });
  document.querySelectorAll("th[data-sort]").forEach((el) => {
    el.addEventListener("click", () => {
      const col = el.dataset.sort;
      if (apptFilters.sort === col) {
        apptFilters.order = apptFilters.order === "asc" ? "desc" : "asc";
      } else {
        apptFilters.sort = col;
        apptFilters.order = "asc";
      }
      apptFilters.page = 1;
      renderAllAppointments();
    });
  });
  document.querySelectorAll('[data-act="view"]').forEach((btn) =>
    btn.addEventListener("click", () => renderAppointmentDetail(btn.dataset.ref, false)));
  document.querySelectorAll('[data-act="edit"]').forEach((btn) =>
    btn.addEventListener("click", () => renderAppointmentDetail(btn.dataset.ref, true)));
  document.querySelectorAll('[data-act="cancel"]').forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm(`Cancel appointment ${btn.dataset.ref}?\n\nIts status becomes Cancelled (the slot frees up again).`)) return;
      const res = await api(`/api/appointments/${btn.dataset.doc}/${btn.dataset.token}/status`, "POST", { status: "Cancelled" });
      await loadState();
      toast(res.ok ? res.message : errOf(res), !res.ok);
      renderAllAppointments();
    }));
}

/* Step 9 (details page): everything about one appointment — patient,
   doctor, date/time, type, reason, notes, fee, payment status,
   booking date, status + its history — with Edit / Reschedule,
   Cancel and Print. */
async function renderAppointmentDetail(ref, editMode) {
  const res = await api(`/api/appointments/by-id/${encodeURIComponent(ref)}`);
  if (!res.ok) {
    body.innerHTML = `<div class="msg msg-err">${esc(errOf(res))}</div>
      <div class="btn-row"><button type="button" class="btn btn-ghost" onclick="openOp('all-appointments')">← Back to All Appointments</button></div>`;
    return;
  }
  const a = res.appointment;
  const p = res.patient || {};
  const d = res.doctor || {};
  const pay = res.payment || {};
  const history = res.history || [];
  const canEdit = currentUser && ["Admin", "Receptionist"].includes(currentUser.role);
  const when = [a.appt_date, a.appt_time].filter(Boolean).join(" ") || "No date set";
  body.innerHTML = `
    <div class="btn-row no-print" style="margin-bottom:10px">
      <button type="button" class="btn btn-ghost" onclick="openOp('all-appointments')">← All Appointments</button>
    </div>
    <div class="card">
      <h3>Appointment ${esc(a.apt_id || a.token)} <span class="tag ${statusTagClass(a.status || "Pending")}">${esc(a.status || "Pending")}</span></h3>
      <div class="kv">
        <div><b>Patient</b></div><div>${esc(p.name || a.patient_name)} (#${a.patient_id}) — Age ${p.age ?? "—"} — ${esc(p.problem || "")}</div>
        <div><b>Father Name</b></div><div>${esc(p.father_name || "—")}</div>
        <div><b>Phone</b></div><div>${esc(p.phone || "—")}</div>
        <div><b>Gender</b></div><div>${esc(p.gender || "—")}</div>
        <div><b>Doctor</b></div><div>${esc(d.name || a.doctor_name)} (${esc(d.spec || a.department)}) — Fee Rs ${d.fee ?? a.fee}</div>
        <div><b>When</b></div><div>${esc(when)}</div>
        <div><b>Type</b></div><div>${esc(a.appt_type || "General")}${a.emergency ? ' <span class="tag tag-emg">EMERGENCY</span>' : ""}</div>
        <div><b>Reason</b></div><div>${esc(a.reason || "—")}</div>
        <div><b>Notes</b></div><div>${esc(a.notes || "—")}</div>
        <div><b>Booked</b></div><div>${esc(a.booked_date || "—")} — Token ${esc(a.token)}</div>
        <div><b>Payment</b></div><div>${pay.billed
          ? `Bill #${pay.bill_id} — Rs ${pay.total} (${esc(pay.status)})`
          : "Not billed yet."}</div>
      </div>
    </div>
    <div class="card">
      <h3>Status History</h3>
      ${history.length
        ? `<div class="timeline">${history.map((h) => `<p>${esc(h.changed_at)} | ${esc(h.old_status || "—")} → ${esc(h.new_status)} by ${esc(h.changed_by)}</p>`).join("")}</div>`
        : `<p class="muted">No status changes yet.</p>`}
      <div class="field-row no-print" style="margin-top:10px">
        <div class="field"><label>Change status</label>
          <select id="det-status">${statusOptions().map((s) => `<option ${s === a.status ? "selected" : ""}>${s}</option>`).join("")}</select></div>
        <div class="btn-row" style="align-items:flex-end">
          <button type="button" class="btn btn-primary" id="det-set-status">Update Status</button>
        </div>
      </div>
    </div>
    <div class="btn-row no-print">
      ${canEdit ? `<button type="button" class="btn btn-primary" id="det-edit">Edit</button>
      <button type="button" class="btn btn-primary" id="det-reschedule">Reschedule</button>
      <button type="button" class="btn btn-danger" id="det-cancel">Cancel Appointment</button>` : ""}
      ${(!pay.billed && canEdit) ? `<button type="button" class="btn btn-ghost" id="det-bill">Generate Bill</button>` : ""}
      <button type="button" class="btn btn-ghost" id="det-print">Print</button>
    </div>
    <div id="det-edit-slot"></div>`;
  document.getElementById("det-set-status").addEventListener("click", async () => {
    const status = document.getElementById("det-status").value;
    const r = await api(`/api/appointments/${a.doctor_id}/${a.token}/status`, "POST", { status });
    await loadState();
    toast(r.ok ? r.message : errOf(r), !r.ok);
    renderAppointmentDetail(ref, false);
  });
  const billBtn = document.getElementById("det-bill");
  if (billBtn) {
    billBtn.addEventListener("click", async () => {
      const r = await api("/api/bills", "POST", { patient_id: a.patient_id, doctor_id: a.doctor_id, token: a.token });
      await loadState();
      toast(r.ok ? r.message : errOf(r), !r.ok);
      renderAppointmentDetail(ref, false);
    });
  }
  document.getElementById("det-print").addEventListener("click", () => window.print());
  const cancelBtn = document.getElementById("det-cancel");
  if (cancelBtn) {
    cancelBtn.addEventListener("click", async () => {
      if (!confirm(`Cancel appointment ${a.apt_id || a.token}?\n\nIts status becomes Cancelled (the slot frees up again).`)) return;
      const r = await api(`/api/appointments/${a.doctor_id}/${a.token}/status`, "POST", { status: "Cancelled" });
      await loadState();
      toast(r.ok ? r.message : errOf(r), !r.ok);
      renderAppointmentDetail(ref, false);
    });
  }
  const openEdit = () => {
    const slot = document.getElementById("det-edit-slot");
    slot.innerHTML = `
      <form id="f-det-edit" class="form-grid card">
        <h3>Edit Appointment</h3>
        <div class="field-row">
          <div class="field"><label>Date</label>
            <input type="date" name="date" value="${esc(a.appt_date || "")}" required></div>
          <div class="field"><label>Time (empty = no fixed time)</label>
            <input type="time" name="time" value="${esc(a.appt_time || "")}"></div>
        </div>
        <div class="field"><label>Type</label>
          <select name="type">
            ${["General", "Follow-up", "Emergency"].map((t) => `<option ${t === a.appt_type ? "selected" : ""}>${t}</option>`).join("")}
          </select></div>
        <div class="field"><label>Reason for Visit</label>
          <input type="text" name="reason" value="${esc(a.reason || "")}"></div>
        <div class="field"><label>Additional Notes</label>
          <input type="text" name="notes" value="${esc(a.notes || "")}"></div>
        <p class="muted">Changing the type between Emergency and General / Follow-up moves the token (E- ↔ N-) but keeps the Appointment ID, booked date and status. A reschedule onto a taken slot is refused.</p>
        <div class="btn-row">
          <button type="submit" class="btn btn-primary">Save Changes</button>
          <button type="button" class="btn btn-ghost" id="det-edit-cancel">Cancel</button>
        </div>
      </form>`;
    document.getElementById("det-edit-cancel").addEventListener("click", () => renderAppointmentDetail(ref, false));
    document.getElementById("f-det-edit").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      const r = await api(`/api/appointments/by-id/${encodeURIComponent(ref)}/edit`, "POST", {
        date: f.get("date"), time: f.get("time") || "",
        type: f.get("type"), reason: f.get("reason"), notes: f.get("notes"),
      });
      await loadState();
      if (r.ok) {
        toast(r.message);
        renderAppointmentDetail(r.appointment.apt_id || ref, false);
      } else {
        toast(errOf(r), true);
      }
    });
  };
  const editBtn = document.getElementById("det-edit");
  if (editBtn) editBtn.addEventListener("click", openEdit);
  const reschedBtn = document.getElementById("det-reschedule");
  if (reschedBtn) reschedBtn.addEventListener("click", openEdit);
  if (editMode) openEdit();
}

/* ------------------------------------------------------------------ boot */

buildSideNav();

document.getElementById("nav-toggle").addEventListener("click", () => {
  if (window.matchMedia("(max-width: 980px)").matches) {
    document.body.classList.toggle("sidebar-open");
    document.getElementById("side-backdrop").classList.toggle(
      "hidden", !document.body.classList.contains("sidebar-open"));
  } else {
    document.body.classList.toggle("sidebar-collapsed");
  }
});
document.getElementById("side-backdrop").addEventListener("click", () => {
  document.body.classList.remove("sidebar-open");
  document.getElementById("side-backdrop").classList.add("hidden");
});

/* Top search: patients by name/ID, doctors by name/ID/specialty,
   appointments by token or patient name — all from the live state. */
const searchInput = document.getElementById("global-search");
let searchTimer = null;
searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => runSearch(searchInput.value.trim()), 200);
});
searchInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { clearTimeout(searchTimer); runSearch(searchInput.value.trim()); }
  if (e.key === "Escape") { document.getElementById("search-results").classList.add("hidden"); searchInput.blur(); }
});
searchInput.addEventListener("blur", () => setTimeout(() =>
  document.getElementById("search-results").classList.add("hidden"), 200));
searchInput.addEventListener("focus", () => {
  if (searchInput.value.trim()) runSearch(searchInput.value.trim());
});

/* Bell + profile panels; a click anywhere else closes them. */
document.getElementById("bell-btn").addEventListener("click", (e) => { e.stopPropagation(); toggleBellPanel(); });
document.getElementById("profile-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  document.getElementById("bell-panel").classList.add("hidden");
  document.getElementById("profile-menu").classList.toggle("hidden");
});
document.addEventListener("click", (e) => {
  if (!e.target.closest(".bell-wrap")) document.getElementById("bell-panel").classList.add("hidden");
  if (!e.target.closest(".profile-wrap")) document.getElementById("profile-menu").classList.add("hidden");
});
document.getElementById("menu-change-pw").addEventListener("click", () => {
  document.getElementById("profile-menu").classList.add("hidden");
  openOp("change-password");
});
document.getElementById("menu-theme").addEventListener("click", () =>
  applyTheme(!document.body.classList.contains("dark")));
document.getElementById("menu-undo").addEventListener("click", () => {
  document.getElementById("profile-menu").classList.add("hidden");
  doUndo();
});
document.getElementById("menu-logout").addEventListener("click", doLogout);

function applyTheme(dark) {
  document.body.classList.toggle("dark", dark);
  const btn = document.getElementById("menu-theme");
  if (btn) btn.textContent = dark ? "Light Mode" : "Dark Mode";
  const setBtn = document.getElementById("set-theme");
  if (setBtn) setBtn.textContent = dark ? "Switch to Light Mode" : "Switch to Dark Mode";
  try { localStorage.setItem("hms-theme", dark ? "dark" : "light"); } catch (e) { /* ignore */ }
}
try {
  applyTheme(localStorage.getItem("hms-theme") === "dark");
} catch (e) { applyTheme(false); }

setInterval(updateClockChips, 30000);

loadMe().then((ok) => {
  if (!ok) return; // loadMe already sent us to the login page.
  loadState().then(() => showDashboard());
});
