/* Draft prep: team-by-team depth chart browser with hover scoring,
   injury history, and player tagging. */

let teams = [];          // [{team_id, abbrev}]
let current = 0;         // index into teams
let vocab = [];          // tag vocabulary
let playerTags = {};     // playerId -> [tagIds]

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const POS_ORDER = ["QB", "RB", "WR", "TE"];

async function init() {
  const [teamsRes, tagsRes] = await Promise.all([
    fetch("/api/prep/teams"), fetch("/api/tags"),
  ]);
  teams = await teamsRes.json();
  const tagData = await tagsRes.json();
  vocab = tagData.vocabulary;
  playerTags = Object.fromEntries(
    Object.entries(tagData.player_tags).map(([k, v]) => [Number(k), v]));
  renderStrip();
  const saved = localStorage.getItem("prep-team");
  const idx = teams.findIndex(t => t.abbrev === saved);
  loadTeam(idx >= 0 ? idx : 0);
}

function renderStrip() {
  $("teamstrip").innerHTML = teams.map((t, i) => {
    return `<div class="tchip ${i === current ? "active" : ""}" data-i="${i}">${t.abbrev}</div>`;
  }).join("");
  $("teamstrip").querySelectorAll(".tchip").forEach(el => {
    el.onclick = () => loadTeam(Number(el.dataset.i));
  });
}

async function loadTeam(idx) {
  current = (idx + teams.length) % teams.length;
  localStorage.setItem("prep-team", teams[current].abbrev);
  renderStrip();
  $("team-name").textContent = teams[current].abbrev;
  $("team-pos").textContent = `${current + 1} of ${teams.length}`;
  $("chart").innerHTML = `<div class="hint" style="grid-column:1/-1">Loading…</div>`;

  const res = await fetch(`/api/prep/team/${teams[current].team_id}`);
  const data = await res.json();
  renderChart(data);
  updateTaggedCount();
}

function renderChart(data) {
  const chart = $("chart");
  chart.innerHTML = "";
  for (const pos of POS_ORDER) {
    const col = document.createElement("div");
    col.className = "poscol";
    col.innerHTML = `<h2><span class="posbadge pos-${pos}">${pos}</span>depth</h2>`;
    for (const p of (data.positions[pos] || [])) {
      col.appendChild(playerCard(p, data.scoring));
    }
    chart.appendChild(col);
  }
}

function playerCard(p, scoring) {
  const node = $("player-card-t").content.firstElementChild.cloneNode(true);
  const tags = playerTags[p.id] || [];
  if (tags.length) node.classList.add("tagged");

  const depth = document.createElement("div");
  depth.className = "depth"; depth.textContent = `#${p.rank}`;
  node.appendChild(depth);

  const img = node.querySelector(".headshot");
  img.src = p.headshot;
  img.onerror = () => { img.style.opacity = .25; };

  node.querySelector(".pname").textContent = p.name;
  const meta = [];
  if (p.adp) meta.push(`ADP ${p.adp.toFixed(1)}`);
  if (p.proj) meta.push(`proj ${p.proj.toFixed(0)}`);
  const inj = p.injury && p.injury !== "ACTIVE" && p.injury !== "NORMAL"
    ? ` <span class="inj-bad">${esc(p.injury)}</span>` : "";
  node.querySelector(".pmeta-prep").innerHTML = (meta.join(" · ") || "undrafted range") + inj;

  wireHover(node, p, scoring);
  wireInjuryPanel(node, p);
  wireTags(node, p);
  return node;
}

/* ---- hover: last season's scoring ---- */
function wireHover(node, p, scoring) {
  const wrap = node.querySelector(".headwrap");
  const card = node.querySelector(".hovercard");
  let built = false;
  wrap.onmouseenter = () => {
    if (!built) { card.innerHTML = hoverHtml(p, scoring); built = true; }
    card.classList.remove("hidden");
  };
  wrap.onmouseleave = () => card.classList.add("hidden");
}

function hoverHtml(p, scoring) {
  const h = p.history;
  if (!h || !h.weekly.length) {
    return `<div class="hc-head">${esc(p.name)}</div>
            <div class="nohist">No ${new Date().getFullYear() - 1} stats — rookie or missed season.</div>`;
  }
  const max = Math.max(...h.weekly.map(w => w.pts), 1);
  const weeks = [];
  const byWeek = Object.fromEntries(h.weekly.map(w => [w.week, w.pts]));
  const first = h.weekly[0].week, last = h.weekly[h.weekly.length - 1].week;
  for (let w = first; w <= last; w++) {
    if (byWeek[w] !== undefined) {
      const hpx = Math.max(2, Math.round((byWeek[w] / max) * 44));
      weeks.push(`<div class="bar-w" style="height:${hpx}px" title="W${w}: ${byWeek[w]}"></div>`);
    } else {
      weeks.push(`<div class="bar-w missed" title="W${w}: did not play"></div>`);
    }
  }
  return `<div class="hc-head">${esc(p.name)} — ${h.season}</div>
    <div class="hc-sub">${h.total} pts (${esc(scoring)}) · ${h.games} games · ${h.ppg} ppg · best ${h.best}</div>
    <div class="spark">${weeks.join("")}</div>
    <div class="weeklbl"><span>W${first}</span>${h.missed.length > 1 ? `<span>out: wk ${h.missed.join(", ")}</span>` : ""}<span>W${last}</span></div>`;
}

/* ---- injury & recovery history ---- */
function wireInjuryPanel(node, p) {
  const btn = node.querySelector(".injbtn");
  const panel = node.querySelector(".injpanel");
  let loaded = false;
  btn.onclick = async () => {
    if (!panel.classList.contains("hidden")) { panel.classList.add("hidden"); return; }
    if (!loaded) {
      panel.innerHTML = `<div class="none">Loading…</div>`;
      panel.classList.remove("hidden");
      const res = await fetch(`/api/prep/injuries/${p.id}`);
      const d = await res.json();
      const bits = [`<div class="ih">INJURY & RECOVERY</div>`];
      if (d.current_status && d.current_status !== "ACTIVE" && d.current_status !== "NORMAL")
        bits.push(`<div class="lv-high">Current: ${esc(d.current_status)}</div>`);
      if (d.games_last_season !== null) {
        const missed = d.missed_last_season;
        // One absent week in an otherwise full season is the bye, not an injury.
        const label = missed.length === 1 && d.games_last_season >= 16
          ? `wk ${missed[0]} off (likely bye)`
          : missed.length ? `no game wk ${missed.join(", ")} (incl. bye)` : `every week played`;
        bits.push(`<div>Last season: ${d.games_last_season} games, ${label}</div>`);
      }
      for (const n of d.injury_news)
        bits.push(`<div class="${n.level === "high" ? "lv-high" : ""}">${esc(n.age)}: ${esc(n.headline)}</div>`);
      for (const n of d.risk_notes) bits.push(`<div>${esc(n)}</div>`);
      if (bits.length === 1) bits.push(`<div class="none">Nothing on record.</div>`);
      panel.innerHTML = bits.join("");
      loaded = true;
    } else {
      panel.classList.remove("hidden");
    }
  };
}

/* ---- tags ---- */
function wireTags(node, p) {
  const row = node.querySelector(".tagrow");
  const paint = () => {
    const mine = playerTags[p.id] || [];
    row.innerHTML = vocab.map(t => {
      const on = mine.includes(t.id) ? ` on-${t.tone}` : "";
      return `<span class="tagchip${on}" data-tag="${t.id}">${esc(t.label)}</span>`;
    }).join("");
    node.classList.toggle("tagged", mine.length > 0);
    row.querySelectorAll(".tagchip").forEach(chip => {
      chip.onclick = async () => {
        const res = await fetch(`/api/tags/${p.id}/${chip.dataset.tag}`, { method: "POST" });
        const d = await res.json();
        playerTags[p.id] = d.tags;
        if (!d.tags.length) delete playerTags[p.id];
        paint();
        updateTaggedCount();
      };
    });
  };
  paint();
}

function updateTaggedCount() {
  const n = Object.keys(playerTags).length;
  $("tagged-count").textContent = n ? `${n} player${n === 1 ? "" : "s"} tagged` : "";
}

$("prev").onclick = () => loadTeam(current - 1);
$("next").onclick = () => loadTeam(current + 1);
document.addEventListener("keydown", (e) => {
  if (e.key === "ArrowLeft") loadTeam(current - 1);
  if (e.key === "ArrowRight") loadTeam(current + 1);
});

init();
