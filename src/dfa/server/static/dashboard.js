/* Draft dashboard: polls /api/state and repaints. */

let posFilter = "ALL";
let expanded = null;      // player id whose news row is open
let lastData = null;
let lastOk = 0;

const $ = (id) => document.getElementById(id);
const POS_COLORS = { QB: "--qb", RB: "--rb", WR: "--wr", TE: "--te", K: "--k", DST: "--dst" };

async function poll() {
  try {
    const res = await fetch("/api/state?top=30");
    if (!res.ok) throw new Error(res.status);
    lastData = await res.json();
    lastOk = Date.now();
    render(lastData);
  } catch (e) {
    /* keep showing the last good state rather than blanking mid-draft */
  }
  const stale = Date.now() - lastOk > 12000;
  $("status-dot").className = "dot " + (lastOk === 0 ? "" : stale ? "stale" : "live");
}

function syncStickyOffset() {
  // The run-alert bar appears and disappears, changing the header height.
  const h = $("clockbar").offsetHeight + ($("runbar").classList.contains("hidden") ? 0 : $("runbar").offsetHeight);
  document.documentElement.style.setProperty("--sticky-top", h + "px");
}

function render(d) {
  renderClock(d);
  renderRuns(d);
  syncStickyOffset();
  renderSlots(d);
  renderNeeds(d);
  renderVona(d);
  renderRecent(d);
  renderYourPick(d);
  renderBoard(d);
}

function renderYourPick(d) {
  const panel = $("yourpick");
  // Only surface the shortlist when the decision is actually yours.
  if (!d.on_clock.is_me || !(d.top3 || []).length) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  $("pickcards").innerHTML = d.top3.map(p => {
    const last = p.survival > 0 ? `${Math.round(p.survival * 100)}% to last` : "—";
    return `<div class="pcard" data-id="${p.id}">
      <div class="tag">${esc(p.tag)}</div>
      <div class="nm">${esc(p.name)}${tagBadges(p)}</div>
      <div class="sub"><span class="posbadge pos-${p.pos}">${p.pos}${p.pos_rank}</span>
        &nbsp;${p.team}${p.bye ? " · bye " + p.bye : ""} · tier ${p.tier}</div>
      <div class="stats">
        <span>VOR <b>${p.vor.toFixed(0)}</b></span>
        <span>ADP <b>${p.adp ? p.adp.toFixed(1) : "—"}</b></span>
        <span><b>${last}</b></span>
      </div>
      <div class="rz">${esc((p.reasons || []).join(" · ")) || "&nbsp;"}</div>
      ${(p.risk_notes || []).length
        ? `<div class="riskline">${riskBadges(p)} ${esc(p.risk_notes[0])}</div>` : ""}
    </div>`;
  }).join("");

  $("pickcards").querySelectorAll(".pcard").forEach(card => {
    card.onclick = () => {
      expanded = expanded === Number(card.dataset.id) ? null : Number(card.dataset.id);
      renderBoard(lastData);
      document.querySelector(`tr[data-id="${card.dataset.id}"]`)
        ?.scrollIntoView({ behavior: "smooth", block: "start" });
    };
  });
}

function renderClock(d) {
  const c = d.on_clock;
  $("clockbar").className = c.is_me ? "mine" : "";
  $("clock-team").textContent = c.is_me
    ? "YOU'RE UP"
    : (c.team_name || `Slot ${c.slot}`);
  $("clock-pick").textContent =
    `Round ${c.round}, Pick ${c.pick_in_round}  ·  #${c.overall} overall`;
  $("league-meta").textContent =
    `${d.league.teams}-team ${d.league.scoring} · ${d.counts.picked}/${d.counts.total} picked`;
  const nexts = (c.my_next_picks || []).slice(0, 3);
  $("next-picks").textContent = nexts.length ? `Your picks: ${nexts.map(n => "#" + n).join(", ")}` : "";
}

function renderRuns(d) {
  const bar = $("runbar");
  if (!d.runs || !d.runs.length) { bar.classList.add("hidden"); return; }
  bar.classList.remove("hidden");
  bar.textContent = "RUN ALERT — " + d.runs.map(r => r.label).join("  ·  ");
}

function renderSlots(d) {
  $("slots").innerHTML = d.roster.slots.map(([slot, filled, req]) => {
    const pips = Array.from({ length: req }, (_, i) =>
      `<div class="pip ${i < filled ? "on" : ""}"></div>`).join("");
    return `<div class="slot"><span class="name">${slot}</span><div class="pips">${pips}</div></div>`;
  }).join("");

  $("roster-list").innerHTML = d.roster.players.length
    ? d.roster.players.map(p =>
        `<div><span>R${p.round}</span><span class="rp">${esc(p.name)}</span><span>${p.pos}</span></div>`).join("")
    : `<div>No picks yet</div>`;
}

function renderNeeds(d) {
  $("needs").innerHTML = d.roster.needs.map(n => {
    const pct = Math.round(n.score * 100);
    return bar(n.pos, pct, pct + "%", `var(${POS_COLORS[n.pos] || "--accent"})`);
  }).join("") || `<div class="hint">Roster complete</div>`;
}

function renderVona(d) {
  const entries = Object.entries(d.vona || {})
    .filter(([p, v]) => v > 0 && ["QB", "RB", "WR", "TE"].includes(p))
    .sort((a, b) => b[1] - a[1]);
  const max = Math.max(...entries.map(e => e[1]), 1);
  $("vona").innerHTML = entries.length
    ? entries.map(([p, v]) =>
        bar(p, Math.round((v / max) * 100), `-${v.toFixed(0)}`, `var(${POS_COLORS[p]})`)).join("")
    : `<div class="hint">n/a until your slot is known</div>`;
}

function bar(label, pct, val, color) {
  return `<div class="bar"><span class="pos" style="color:${color}">${label}</span>
    <div class="track"><div class="fill" style="width:${pct}%;background:${color}"></div></div>
    <span class="val">${val}</span></div>`;
}

function renderRecent(d) {
  $("recent").innerHTML = d.recent_picks.map(p =>
    `<li class="${p.is_me ? "me" : ""}"><span class="ov">${p.round}.${String(p.pick).padStart(2, "0")}</span>
     <span>${esc(p.name)}</span><span class="hint">${p.pos}</span></li>`).join("")
    || `<li class="hint">Waiting for the draft to start…</li>`;
}

function renderBoard(d) {
  const rows = d.available.filter(p => posFilter === "ALL" || p.pos === posFilter);
  const body = $("rows");
  body.innerHTML = rows.map((p, i) => {
    const flags =
      (p.is_value ? `<span class="flag value">VALUE</span>` : "") +
      (p.injury_flag ? `<span class="flag inj">${esc(shortInj(p.injury))}</span>` : "") +
      riskBadges(p) + tagBadges(p);
    const last = p.survival > 0
      ? `<span class="${p.will_last ? "last-yes" : "last-no"}">${Math.round(p.survival * 100)}%</span>`
      : `<span class="hint">—</span>`;
    const risky = p.risk_level === "high" || p.risk_level === "medium";
    const main = `<tr data-id="${p.id}" class="${i < 3 ? "top" : ""}${risky ? " has-risk" : ""}">
      <td class="num">${i + 1}</td>
      <td><span class="pname">${esc(p.name)}</span>${flags}
          <span class="pmeta">${p.team}${p.bye ? " · bye " + p.bye : ""}</span></td>
      <td><span class="posbadge pos-${p.pos}">${p.pos}${p.pos_rank}</span></td>
      <td class="num">T${p.tier}</td>
      <td class="num">${p.adp ? p.adp.toFixed(1) : "—"}</td>
      <td class="num">${p.proj.toFixed(0)}</td>
      <td class="num">${p.vor.toFixed(0)}</td>
      <td class="num">${last}</td>
      <td class="why">${esc((p.reasons || []).join(" · "))}</td>
    </tr>`;
    return main + (expanded === p.id ? detailRow(p) : "");
  }).join("");

  body.querySelectorAll("tr[data-id]").forEach(tr => {
    tr.onclick = () => {
      const id = Number(tr.dataset.id);
      expanded = expanded === id ? null : id;
      renderBoard(lastData);
    };
  });
}

function tagBadges(p) {
  return (p.tags || []).map(t =>
    `<span class="flag tag-${t.tone}">${esc(t.label.toUpperCase())}</span>`).join("");
}

function riskBadges(p) {
  if (!p.risk_level || p.risk_level === "none" || p.risk_level === "low") return "";
  const cls = p.risk_level === "high" ? "risk-high" : "risk-medium";
  const labels = (p.risk_flags || []).length ? p.risk_flags : ["RISK"];
  return labels.map(f => `<span class="flag ${cls}">${esc(f)}</span>`).join("");
}

function riskBlock(p) {
  if (!(p.risk_notes || []).length) return "";
  return `<div class="risknote">
    <div class="rl">RISK — ${esc((p.risk_level || "").toUpperCase())}</div>
    ${p.risk_notes.map(n => `<div>${esc(n)}</div>`).join("")}
  </div>`;
}

function detailRow(p) {
  const news = (p.news || []).length
    ? p.news.map(n => `<div class="newsitem">
        <div class="ag">${esc(n.age)}</div>
        <div class="hl">${esc(n.headline)}</div>
        <div class="st">${esc(n.story)}</div></div>`).join("")
    : `<div class="nonews">No recent news items.</div>`;
  const outlook = p.outlook ? `<div class="outlook"><b>Outlook:</b> ${esc(p.outlook)}</div>` : "";
  return `<tr class="detail"><td colspan="9">${riskBlock(p)}${outlook}${news}</td></tr>`;
}

function shortInj(s) {
  return { QUESTIONABLE: "QUES", DOUBTFUL: "DOUBT", OUT: "OUT",
           INJURY_RESERVE: "IR", SUSPENSION: "SUSP", DAY_TO_DAY: "DTD" }[s] || s;
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ---- controls ---- */
document.querySelectorAll(".filt").forEach(b => {
  b.onclick = () => {
    document.querySelectorAll(".filt").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    posFilter = b.dataset.pos;
    if (lastData) renderBoard(lastData);
  };
});

const search = $("search"), results = $("results");
let searchTimer = null;
search.oninput = () => {
  clearTimeout(searchTimer);
  const q = search.value.trim();
  if (q.length < 2) { results.classList.add("hidden"); return; }
  searchTimer = setTimeout(async () => {
    const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
    const rows = await res.json();
    results.innerHTML = rows.map(r =>
      `<div data-id="${r.id}">${esc(r.name)} <span class="hint">${r.pos} · ${r.team}</span></div>`).join("");
    results.classList.toggle("hidden", rows.length === 0);
    results.querySelectorAll("div[data-id]").forEach(el => {
      el.onclick = async () => {
        await fetch(`/api/pick/${el.dataset.id}`, { method: "POST" });
        search.value = ""; results.classList.add("hidden");
        poll();
      };
    });
  }, 180);
};

$("undo").onclick = async () => { await fetch("/api/undo", { method: "POST" }); poll(); };

poll();
setInterval(poll, 2500);
