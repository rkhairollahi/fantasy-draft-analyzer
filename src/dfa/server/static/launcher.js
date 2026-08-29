/* Launcher: sign in -> pick a league -> choose a mode. */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let leagues = [];
let league = null;      // selected league id
let facts = null;       // /api/modes payload for the selected league
let pollTimer = null;

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json();
}

/* ---- step 1: auth ---- */
async function refreshAuth() {
  const a = await api("/api/auth");
  const el = $("auth-state");
  $("login-btn").classList.toggle("hidden", a.signed_in);
  $("logout-btn").classList.toggle("hidden", !a.signed_in);
  $("step-auth").classList.toggle("done", a.signed_in);

  if (a.signed_in) {
    el.textContent = "Signed in to ESPN.";
    $("step-league").classList.remove("disabled");
    if (!leagues.length) loadLeagues(a.selected_league);
  } else if (a.status === "waiting" || a.status === "opening") {
    el.textContent = a.detail || "Waiting for sign-in…";
    $("login-btn").disabled = true;
  } else {
    el.textContent = a.detail || "Not signed in.";
    $("login-btn").disabled = false;
  }
  // Keep polling while a sign-in window is open.
  const busy = a.status === "waiting" || a.status === "opening";
  clearTimeout(pollTimer);
  if (busy) pollTimer = setTimeout(refreshAuth, 1500);
}

$("login-btn").onclick = async () => {
  $("login-btn").disabled = true;
  $("auth-state").textContent = "Opening a browser window…";
  await api("/api/auth/login", { method: "POST" });
  setTimeout(refreshAuth, 1200);
};

$("logout-btn").onclick = async () => {
  await api("/api/auth/logout", { method: "POST" });
  leagues = []; league = null; facts = null;
  $("league-list").textContent = "Sign in first.";
  $("step-league").classList.add("disabled");
  $("step-mode").classList.add("disabled");
  refreshAuth();
};

/* ---- step 2: leagues ---- */
async function loadLeagues(preselect) {
  $("league-list").textContent = "Loading your leagues…";
  try {
    leagues = await api("/api/leagues");
  } catch {
    $("league-list").textContent = "Could not load leagues.";
    return;
  }
  if (!leagues.length) {
    $("league-list").textContent = "No football leagues found on this account.";
    return;
  }
  $("league-list").innerHTML = leagues.map(l => `
    <div class="leaguerow" data-id="${esc(l.id)}">
      <span class="lname">${esc(l.name)}</span>
      <span class="lteam">${esc(l.team || "")}</span>
    </div>`).join("");
  $("league-list").querySelectorAll(".leaguerow").forEach(row => {
    row.onclick = () => selectLeague(row.dataset.id);
  });
  const saved = preselect && leagues.find(l => String(l.id) === String(preselect));
  if (saved) selectLeague(saved.id);
}

async function selectLeague(id) {
  league = String(id);
  document.querySelectorAll(".leaguerow").forEach(r =>
    r.classList.toggle("on", r.dataset.id === league));
  $("step-league").classList.add("done");
  $("step-mode").classList.remove("disabled");
  $("league-facts").textContent = "Reading league settings…";
  $("modes").innerHTML = "";
  try {
    facts = await api(`/api/modes?league=${encodeURIComponent(league)}`);
  } catch {
    $("league-facts").textContent = "Could not read that league.";
    return;
  }
  renderModes();
}

/* ---- step 3: modes ---- */
function renderModes() {
  const f = facts;
  $("league-facts").textContent =
    `${f.teams}-team ${f.scoring}, ${f.rounds} rounds` +
    (f.my_draft_slot ? ` · your slot ${f.my_draft_slot}` : "");

  const draftState = f.draft_complete ? "Draft complete."
    : f.draft_in_progress ? "Draft is live right now."
    : "Draft hasn't started yet.";

  $("modes").innerHTML = `
    <button class="modecard" id="m-practice">
      <div class="mt">Practice draft</div>
      <div class="md">A full mock against bots using this league's real
        settings. Choose your slot, pick from the board, and the app drafts
        for you if the clock runs out.</div>
    </button>

    <button class="modecard" id="m-live">
      <div class="mt">Draft</div>
      <div class="md">Follows your real draft. Start it here, then run the
        draft in your own ESPN tab — the board keeps up as picks land.
        ${esc(draftState)}</div>
    </button>

    <button class="modecard" id="m-fa" ${f.free_agency_ready ? "" : "disabled"}>
      <div class="mt">Free agency analysis</div>
      <div class="md">Waiver targets: who covers your injured players, who
        could take over a job, and who is quietly worth owning.</div>
      ${f.free_agency_ready ? "" :
        `<div class="lock">Available once this league's draft is complete.</div>`}
    </button>`;

  $("m-practice").onclick = openPractice;
  $("m-live").onclick = startLive;
  const fa = $("m-fa");
  if (fa && !fa.disabled) fa.onclick = () => { location.href = `/waivers?league=${league}`; };
}

/* ---- practice setup ---- */
function openPractice() {
  const teams = facts.teams;
  $("slot-picker").innerHTML = Array.from({ length: teams }, (_, i) =>
    `<div class="slot" data-slot="${i + 1}">${i + 1}</div>`).join("");
  const preferred = facts.my_draft_slot || 1;
  $("slot-picker").querySelectorAll(".slot").forEach(s => {
    s.classList.toggle("on", Number(s.dataset.slot) === preferred);
    s.onclick = () => {
      $("slot-picker").querySelectorAll(".slot").forEach(x => x.classList.remove("on"));
      s.classList.add("on");
    };
  });
  $("practice-modal").classList.remove("hidden");
}

$("practice-cancel").onclick = () => $("practice-modal").classList.add("hidden");

$("practice-go").onclick = async () => {
  const slot = Number(document.querySelector(".slot.on")?.dataset.slot || 1);
  const secs = Number($("pick-seconds").value);
  $("practice-go").disabled = true;
  try {
    await api(`/api/mode/practice?league=${encodeURIComponent(league)}` +
              `&slot=${slot}&pick_seconds=${secs}`, { method: "POST" });
    location.href = "/board";
  } catch (e) {
    $("practice-go").disabled = false;
    alert("Could not start the practice draft.");
  }
};

async function startLive() {
  $("m-live").disabled = true;
  try {
    await api(`/api/mode/live?league=${encodeURIComponent(league)}`, { method: "POST" });
    location.href = "/board";
  } catch {
    $("m-live").disabled = false;
    alert("Could not start the draft watcher.");
  }
}

/* ---- running mode banner ---- */
async function refreshRunning() {
  try {
    const m = await api("/api/mode");
    const box = $("running");
    if (m.mode === "idle") {
      box.classList.add("hidden");
      $("links").classList.add("hidden");
    } else {
      box.classList.remove("hidden");
      $("links").classList.remove("hidden");
      box.innerHTML = `<div class="rt">${esc(m.mode === "practice" ? "Practice draft running"
        : m.mode === "waiting" ? "Waiting for your draft" : "Following your draft")}</div>
        <div class="rd">${esc(m.detail || "")}</div>
        <button class="ghost" id="stop-mode">Stop</button>`;
      $("stop-mode").onclick = async () => {
        await api("/api/mode/stop", { method: "POST" });
        refreshRunning();
      };
    }
  } catch { /* server busy; try again next tick */ }
  setTimeout(refreshRunning, 3000);
}

refreshAuth();
refreshRunning();
