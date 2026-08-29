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
  renderPracticeBar(d);
  renderYourPick(d);
  renderBoard(d);
}

/* Practice mode: show the clock and let the user actually pick. */
function renderPracticeBar(d) {
  const bar = $("practicebar");
  const r = d.runner || {};
  if (r.mode !== "practice") { bar.classList.add("hidden"); return; }
  bar.classList.remove("hidden");
  if (d.on_clock.is_me && r.seconds_left !== null && r.seconds_left !== undefined) {
    const low = r.seconds_left <= 10;
    bar.className = low ? "urgent" : "";
    bar.innerHTML = `<b>YOUR PICK</b> — ${r.seconds_left}s on the clock.
      Click <b>Draft</b> on any player, or the app takes its own top pick.`;
  } else {
    bar.className = "";
    const last = (r.autopicked || []).slice(-1)[0];
    bar.innerHTML = `Practice draft running.` +
      (last ? ` Last auto-pick: <b>${esc(last)}</b>.` : "");
  }
}

async function draftPlayer(id) {
  try {
    const res = await fetch(`/api/practice/pick/${id}`, { method: "POST" });
    if (res.ok) refresh();
  } catch { /* the clock may have just expired */ }
}

function canDraft(d) {
  return (d.runner || {}).mode === "practice" && d.on_clock.is_me;
}

function renderYourPick(d) {
  const panel = $("yourpick");
  // Only surface the shortlist when the decision is actually yours.
  if (!d.on_clock.is_me || !(d.top3 || []).length) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  const note = $("disclaimer");
  if (note) {
    note.textContent = canDraft(d)
      ? "Click Draft on any player to make the pick."
      : "Recommendations only. Make the pick yourself in ESPN.";
  }
  $("pickcards").innerHTML = d.top3.map(p => {
    const last = p.survival > 0 ? `${Math.round(p.survival * 100)}% to last` : "—";
    return `<div class="pcard" data-id="${p.id}">
      <div class="tag">${esc(p.tag)}</div>
      <div class="nm">${esc(p.name)}${tagBadges(p)}</div>
      <div class="sub"><span class="posbadge pos-${p.pos}">${p.pos}${p.pos_rank}</span>
        &nbsp;${p.team}${p.bye ? " · bye " + p.bye : ""} · tier ${p.tier}</div>
      ${roomLine(p)}
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
    const flags = badgeSet(p);
    const last = p.survival > 0
      ? `<span class="${p.will_last ? "last-yes" : "last-no"}">${Math.round(p.survival * 100)}%</span>`
      : `<span class="hint">—</span>`;
    const risky = p.risk_level === "high" || p.risk_level === "medium";
    const main = `<tr data-id="${p.id}" class="${i < 3 ? "top" : ""}${risky ? " has-risk" : ""}">
      <td class="num">${i + 1}</td>
      <td><span class="pname" data-hist="${p.id}">${esc(p.name)}</span>${flags}
          <span class="pmeta">${p.team}${p.bye ? " · bye " + p.bye : ""}</span>
          ${roomLine(p)}</td>
      <td><span class="posbadge pos-${p.pos}">${p.pos}${p.pos_rank}</span></td>
      <td class="num">T${p.tier}</td>
      <td class="num">${p.adp ? p.adp.toFixed(1) : "—"}</td>
      <td class="num">${p.proj.toFixed(0)}</td>
      <td class="num">${p.vor.toFixed(0)}</td>
      <td class="num">${last}</td>
      <td class="why">${esc((p.reasons || []).join(" · "))}</td>
      <td class="pickcell">${canDraft(lastData)
        ? `<button class="draftbtn" data-draft="${p.id}">Draft</button>` : ""}</td>
    </tr>`;
    return main + (expanded === p.id ? detailRow(p) : "");
  }).join("");

  body.querySelectorAll("[data-hist]").forEach(el => wireHistoryHover(el));
  body.querySelectorAll("[data-draft]").forEach(btn => {
    btn.onclick = (ev) => {
      ev.stopPropagation();   // don't also expand the row
      draftPlayer(Number(btn.dataset.draft));
    };
  });
  body.querySelectorAll("tr[data-id]").forEach(tr => {
    tr.onclick = () => {
      const id = Number(tr.dataset.id);
      expanded = expanded === id ? null : id;
      renderBoard(lastData);
    };
  });
}

/* Who else is in this player's position room on his NFL team. */
function roomLine(p) {
  const r = p.room;
  if (!r) return "";
  const me = `${r.pos}${r.rank}`;

  // A same-position teammate going early is the headline: that's a committee.
  const splits = r.mates.filter(m => m.split);
  if (splits.length) {
    const who = splits.map(m =>
      `${esc(m.name)} <span class="rk">${r.pos}${m.rank}</span>` +
      (m.adp ? ` <span class="rk">ADP ${Math.round(m.adp)}</span>` : "")).join(", ");
    return `<div class="room split"><b>${me}</b> · shares with ${who}</div>`;
  }

  const ahead = r.mates.filter(m => m.ahead);
  if (ahead.length) {
    const lead = ahead[ahead.length - 1];
    return `<div class="room"><b>${me}</b> · behind ${esc(lead.name)} ` +
           `<span class="rk">${r.pos}${lead.rank}</span></div>`;
  }
  const behind = r.mates.filter(m => !m.ahead);
  if (behind.length) {
    return `<div class="room"><b>${me}</b> · next up ${esc(behind[0].name)} ` +
           `<span class="rk">${r.pos}${behind[0].rank}</span></div>`;
  }
  return `<div class="room"><b>${me}</b></div>`;
}

/* One badge per distinct label: the ESPN designation and the risk flags
   frequently repeat each other (QUES QUES), which reads as a bug. */
function badgeSet(p) {
  const out = [];
  const seen = new Set();
  const push = (cls, label) => {
    const key = String(label).toUpperCase();
    if (!label || seen.has(key)) return;
    seen.add(key);
    out.push(`<span class="flag ${cls}">${esc(label)}</span>`);
  };
  if (p.is_value) push("value", "VALUE");
  if (p.injury_flag) push("inj", shortInj(p.injury));
  const rc = p.risk_level === "high" ? "risk-high" : "risk-medium";
  if (p.risk_level === "high" || p.risk_level === "medium") {
    const labels = (p.risk_flags || []).length ? p.risk_flags : ["RISK"];
    labels.forEach(f => push(rc, f));
  }
  (p.tags || []).forEach(t => push(`tag-${t.tone}`, t.label.toUpperCase()));
  return out.join("");
}

/* ---- last season's scoring, on hover over a player's name ---- */
const historyCache = new Map();
let histTip = null;

function ensureTip() {
  if (!histTip) {
    histTip = document.createElement("div");
    histTip.id = "histtip";
    histTip.className = "hidden";
    document.body.appendChild(histTip);
  }
  return histTip;
}

function wireHistoryHover(el) {
  const id = Number(el.dataset.hist);
  let hoverToken = 0;

  el.onmouseenter = async () => {
    const tip = ensureTip();
    const token = ++hoverToken;
    tip.innerHTML = `<div class="ht-head">Loading…</div>`;
    positionTip(tip, el);
    tip.classList.remove("hidden");

    let data = historyCache.get(id);
    if (!data) {
      try {
        data = await (await fetch(`/api/history/${id}`)).json();
        historyCache.set(id, data);
      } catch { data = null; }
    }
    // The pointer may have moved on while we were fetching.
    if (token !== hoverToken) return;
    tip.innerHTML = historyHtml(data, el.textContent);
    positionTip(tip, el);
  };

  el.onmouseleave = () => {
    hoverToken++;
    if (histTip) histTip.classList.add("hidden");
  };
}

function positionTip(tip, el) {
  const r = el.getBoundingClientRect();
  tip.style.left = Math.min(window.innerWidth - 430, r.left) + "px";
  // Flip above the row when there isn't room below.
  const below = window.innerHeight - r.bottom;
  if (below < 180) {
    tip.style.top = (r.top + window.scrollY - 172) + "px";
  } else {
    tip.style.top = (r.bottom + window.scrollY + 6) + "px";
  }
}

function historyHtml(d, fallbackName) {
  const h = d && d.history;
  const name = (d && d.name) || fallbackName || "";
  if (!h || !h.weekly.length) {
    return `<div class="ht-head">${esc(name)}</div>
      <div class="ht-none">No prior-season games — rookie, or missed the year.</div>`;
  }
  const max = Math.max(...h.weekly.map(w => w.pts), 1);
  const byWeek = Object.fromEntries(h.weekly.map(w => [w.week, w.pts]));
  const first = h.weekly[0].week, last = h.weekly[h.weekly.length - 1].week;
  const cols = [];
  for (let w = first; w <= last; w++) {
    if (byWeek[w] === undefined) {
      cols.push(`<div class="ht-col"><div class="ht-val">—</div>
        <div class="ht-bar out" style="height:10px" title="Week ${w}: did not play"></div>
        <div class="ht-wk">${w}</div></div>`);
    } else {
      const pts = byWeek[w];
      const px = Math.max(3, Math.round((pts / max) * 38));
      cols.push(`<div class="ht-col"><div class="ht-val">${pts.toFixed(1)}</div>
        <div class="ht-bar" style="height:${px}px" title="Week ${w}: ${pts}"></div>
        <div class="ht-wk">${w}</div></div>`);
    }
  }
  return `<div class="ht-head">${esc(name)} — ${h.season}</div>
    <div class="ht-sub">${h.total} pts (${esc(d.scoring)}) · ${h.games} games ·
      ${h.ppg} ppg · best ${h.best}</div>
    <div class="ht-chart">${cols.join("")}</div>`;
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
  return `<tr class="detail"><td colspan="10">${riskBlock(p)}${outlook}${news}</td></tr>`;
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
