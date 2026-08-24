/* Waiver wire: who can take over a job, and who is quietly worth owning. */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let leagues = [];
let currentLeague = null;

async function init() {
  try {
    leagues = await (await fetch("/api/leagues")).json();
  } catch { leagues = []; }
  const sel = $("league-pick");
  sel.innerHTML = leagues.map(l =>
    `<option value="${l.id}">${esc(l.name)}</option>`).join("");
  sel.onchange = () => load(sel.value);
  currentLeague = localStorage.getItem("waiver-league") ||
                  (leagues[0] && leagues[0].id) || null;
  if (currentLeague) sel.value = currentLeague;
  load(currentLeague);
}

async function load(leagueId, refresh = false) {
  currentLeague = leagueId;
  if (leagueId) localStorage.setItem("waiver-league", leagueId);
  $("sections").innerHTML = `<div class="empty">Loading free agents…</div>`;
  const qs = new URLSearchParams();
  if (leagueId) qs.set("league", leagueId);
  if (refresh) qs.set("refresh", "true");
  const res = await fetch("/api/waivers?" + qs);
  if (!res.ok) {
    $("sections").innerHTML = `<div class="empty">Could not load: ${res.status}</div>`;
    return;
  }
  render(await res.json());
}

function render(d) {
  const league = leagues.find(l => String(l.id) === String(d.league_id));
  $("league-label").textContent = league ? league.name : d.league_id;
  $("fa-count").textContent = `${d.free_agent_count} free agents`;

  const ex = $("exposed");
  if (d.exposed.length) {
    ex.classList.remove("hidden");
    ex.innerHTML = "Your roster needs cover: " + d.exposed.map(p =>
      `<b>${esc(p.name)}</b> (${p.pos}, ${esc(p.injury.replace(/_/g, " ").toLowerCase())})`
    ).join(" · ");
  } else {
    ex.classList.add("hidden");
  }

  $("sections").innerHTML = d.sections.map(sec => {
    const urgent = sec.id === "handcuff" || sec.id === "takeover";
    const body = sec.players.length
      ? `<div class="wgrid">${sec.players.map(p => card(p, urgent)).join("")}</div>`
      : `<div class="empty">Nothing here right now.</div>`;
    // Be explicit when the pool is genuinely picked over, rather than letting
    // a list of negative-value players look like a recommendation.
    const caveat = (sec.id === "gem" && d.none_above_replacement)
      ? `<div class="caveat">No free agent currently projects above a
         replacement starter — normal in a shallow league. These are simply
         the best of what's left.</div>` : "";
    return `<section class="wsection ${urgent ? "urgent" : ""}">
      <h2>${esc(sec.title)}</h2>
      <div class="blurb">${esc(sec.blurb)}</div>
      ${caveat}${body}</section>`;
  }).join("");
}

function card(p, urgent) {
  const inj = p.injury && p.injury !== "ACTIVE" && p.injury !== "NORMAL"
    ? ` <span class="inj">${esc(p.injury.replace(/_/g, " "))}</span>` : "";
  const trend = p.trend >= 1 ? ` <span class="trend-up">+${p.trend}%</span>` : "";
  return `<div class="wcard ${urgent ? "urgent" : ""}">
    <div class="top">
      <span class="posbadge pos-${p.pos}">${p.pos}</span>
      <span class="nm">${esc(p.name)}</span>${inj}
    </div>
    <div class="own">${p.owned}% rostered${trend} · ${esc(p.team)}</div>
    <div class="head">${esc(p.headline)}</div>
    <div class="why">${esc((p.reasons || []).join(" · "))}</div>
  </div>`;
}

$("refresh").onclick = () => load(currentLeague, true);
init();
