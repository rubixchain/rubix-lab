"use strict";
/* Rubix Lab web UI - the page. Plain JavaScript, no build step. It reads the
   backend's JSON API (controller/ui/labui/app.py) and, while a run goes, its
   live stream. Nothing here talks to a node: every reading comes from the
   backend's cache, so any number of open pages costs the fleet nothing. */

const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
const ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};
const esc = v => String(v == null ? "" : v).replace(/[&<>"']/g, c => ESC[c]);

async function api(path, opts) {
  const r = await fetch(path, opts);
  let body;
  try { body = await r.json(); } catch (e) { body = {error: "unreadable reply (HTTP " + r.status + ")"}; }
  if (!r.ok) {
    const err = new Error((body && body.error) || ("HTTP " + r.status));
    err.body = body;
    err.status = r.status;
    throw err;
  }
  return body;
}
const post = (path, body) => api(path, {method: "POST", headers: {"Content-Type": "application/json"},
                                        body: JSON.stringify(body || {})});

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

function toDate(t) {
  if (t == null || t === "") return null;
  return typeof t === "number" ? new Date(t * 1000) : new Date(t);
}
function when(t) {
  const d = toDate(t);
  if (!d || isNaN(d)) return "-";
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function ago(t) {
  if (!t) return "not yet";
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return Math.round(s) + " s ago";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  return (s / 3600).toFixed(1) + " h ago";
}
function dur(s) {
  if (s == null || s === "") return "-";
  s = Math.round(+s);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
  return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
}
function rbt(n, digits) {
  if (n == null || n === "") return "-";
  const d = digits == null ? 3 : digits;
  return (+n).toLocaleString("en-US", {minimumFractionDigits: 0, maximumFractionDigits: d});
}
function secs(n) { return n == null ? "-" : (+n).toFixed(2); }
function shortId(d, head, tail) {
  if (!d) return "-";
  head = head || 10;
  tail = tail == null ? 6 : tail;
  return d.length > head + tail + 1 ? d.slice(0, head) + "…" + (tail ? d.slice(-tail) : "") : d;
}
function nodeName(h) {
  if (!h) return "-";
  const [ip, port] = String(h).split(":");
  return "." + ip.split(".").pop() + (port ? ":" + port : "");
}
const BADGE = {
  PASS: "ok", finished: "ok", done: "ok", up: "ok", ready: "ok", ok: "ok",
  FAIL: "bad", failed: "bad", error: "bad", down: "bad", "not ready": "bad",
  SKIP: "warn", aborted: "warn", stopped: "warn", refused: "warn", interrupted: "warn",
  "no report": "warn", running: "run", starting: "run", preparing: "run",
  waiting: "idle",
};
function badge(text) {
  return `<span class="badge b-${BADGE[text] || "idle"}">${esc(text)}</span>`;
}
function fileLinks(files) {
  const names = [["json", "JSON"], ["pdf", "PDF"], ["evidence", "evidence"], ["log", "log"],
                 ["console", "console"]];
  return names.filter(([k]) => files && files[k])
    .map(([k, label]) => `<a href="${esc(files[k])}" target="_blank" rel="noopener">${label}</a>`)
    .join(" · ") || '<span class="muted">-</span>';
}
function msgList(errors, warnings) {
  const items = (errors || []).map(e => `<li class="err">${esc(e)}</li>`)
    .concat((warnings || []).map(w => `<li class="warn">${esc(w)}</li>`));
  return items.length ? `<ul class="msgs">${items.join("")}</ul>` : "";
}
function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem("rubixlab." + key) || "null");
    localStorage.setItem("rubixlab." + key, JSON.stringify(value));
  } catch (e) { return null; }
  return null;
}

// ---------------------------------------------------------------------------
// Pages and the header
// ---------------------------------------------------------------------------

let leaving = [];
function onLeave(fn) { leaving.push(fn); }
function every(ms, fn) { const id = setInterval(fn, ms); onLeave(() => clearInterval(id)); }

const PAGES = {fleet: pageFleet, run: pageRun, live: pageLive, runs: pageLive, reports: pageReports};

function route() {
  leaving.forEach(fn => { try { fn(); } catch (e) { /* page already gone */ } });
  leaving = [];
  const parts = (location.hash || "#/fleet").replace(/^#\/?/, "").split("/");
  const name = PAGES[parts[0]] ? parts[0] : "fleet";
  $$("header nav a").forEach(a => a.classList.toggle("on", a.dataset.page === (name === "runs" ? "live" : name)));
  const main = $("#main");
  main.innerHTML = "";
  PAGES[name](main, parts.slice(1).map(decodeURIComponent)).catch(e => {
    main.innerHTML = `<div class="note red">${esc(e.message)}</div>`;
  });
}

let STATUS = null;
async function refreshStatus() {
  try { STATUS = await api("/api/status"); } catch (e) { return; }
  $("#demo").hidden = !STATUS.demo;
  const b = $("#busy");
  const run = STATUS.run;
  if (run && (run.status === "starting" || run.status === "running")) {
    if (run.exclusive_now && run.exclusive_now.length) {
      b.className = "busy exclusive";
      b.textContent = "Exclusive unit " + run.exclusive_now.join(", ") + " - background reads paused";
    } else {
      b.className = "busy run";
      b.textContent = (run.source === "terminal" ? "Terminal run going" : "Run going") + " - " + run.id;
    }
  } else if (STATUS.holder) {
    b.className = "busy run";
    b.textContent = "Fleet held: " + (STATUS.holder.script || "a run");
  } else {
    b.className = "busy idle";
    b.textContent = "Fleet free";
  }
}
function fleetBusy() {
  if (!STATUS) return "";
  const run = STATUS.run;
  if (run && (run.status === "starting" || run.status === "running")) return "a run is going (" + run.id + ")";
  if (STATUS.holder) return STATUS.holder_text;
  return "";
}

// ---------------------------------------------------------------------------
// Fleet
// ---------------------------------------------------------------------------

async function pageFleet(main) {
  main.innerHTML = `
    <div class="row"><div class="grow"><h1>Fleet</h1>
      <div class="sub">Read in the background by the UI: liveness (node/peer_id) every 30 s, DIDs and quorum
      lists every 5 min, the faucet every minute, the fullnodes every 2 min - slower during a run, and not at
      all while an exclusive unit runs.</div></div>
      <div class="toolbar"><button id="f-refresh">Read the fleet now</button>
      <button id="f-builds" title="SSH to every machine and read each node's build (like node-versions.py)">Collect builds</button></div></div>
    <div id="f-msg"></div>
    <div class="cards" id="f-cards"></div>
    <div id="f-tiers" class="sub"></div>
    <div class="row" style="margin-top:14px">
      <div class="panel grow" id="f-faucet"></div>
      <div class="panel grow" id="f-fullnodes" style="flex-grow:2"></div>
    </div>
    <div class="panel"><h2>Machines <span class="sub">nodes up of nodes run; click one to see only its nodes</span></h2>
      <div class="machines" id="f-machines"></div><div id="f-out" class="small muted" style="margin-top:10px"></div></div>
    <div class="panel"><div class="toolbar" style="margin-top:0"><h2 style="margin:0">Nodes</h2>
      <input type="text" id="f-filter" placeholder="filter: ip, port, DID, peer ID" size="34">
      <label><input type="checkbox" id="f-problems"> problems only</label>
      <span class="spacer"></span><span id="f-count" class="sub"></span></div>
      <div class="tablewrap"><table id="f-nodes"></table></div></div>`;
  let data = null, machine = null;
  const open = new Set();

  async function load() {
    try { data = await api("/api/fleet"); } catch (e) { $("#f-msg").innerHTML = `<div class="note red">${esc(e.message)}</div>`; return; }
    render();
  }
  function render() {
    const t = data.totals || {};
    const fa = data.faucet || {};
    const fnodes = (data.fullnodes && data.fullnodes.fullnodes) || [];
    $("#f-msg").innerHTML = (data.hosts_error ? `<div class="note red">${esc(data.hosts_error)}</div>` : "") +
      (data.mode === "exclusive" ? `<div class="note">An exclusive unit is running: the UI is not reading the fleet until it ends. What you see was read before.</div>` : "");
    $("#f-cards").innerHTML = [
      ["Pool machines", `${t.pool_machines || 0}`, `${t.machines || 0} machines with nodes in hosts.txt`],
      ["Pool nodes up", `${t.pool_up || 0} / ${t.pool_nodes || 0}`, `${t.up || 0} / ${t.nodes || 0} with the fullnodes and faucet pair`],
      ["DIDs on pool nodes", `${t.dids || 0}`, `layout says ${t.did_target || 0}`],
      ["Faucet free", fa.faucet ? rbt(fa.faucet.balance, 0) : "-", fa.ok === false ? "NOT READY" : (fa.ok ? "ready" : "not read yet")],
      ["Fullnodes", `${fnodes.filter(f => !f.error).length} / ${fnodes.length}`, "databases readable"],
    ].map(([k, v, d]) => `<div class="card"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="d">${esc(d)}</div></div>`).join("");
    $("#f-tiers").innerHTML = "Last read: " + Object.entries(data.tiers || {}).map(([name, s]) =>
      `<b>${esc(name)}</b> ${s.running ? "reading now" : esc(ago(s.last))}${s.paused ? " (paused)" : ""}${s.error ? ` <span style="color:var(--bad)">${esc(s.error)}</span>` : ""}`).join(" · ") +
      (data.builds && data.builds.running ? " · <b>builds</b> collecting over SSH…" : "") +
      (data.builds && data.builds.error ? ` · builds: <span style="color:var(--bad)">${esc(data.builds.error)}</span>` : "");
    renderFaucet(fa);
    renderFullnodes(data.fullnodes);
    renderMachines();
    renderNodes();
  }
  function renderFaucet(fa) {
    if (!fa || !fa.faucet) { $("#f-faucet").innerHTML = "<h2>Faucet</h2><div class='muted'>not read yet</div>"; return; }
    const wallet = w => w && w.did ? `${esc(nodeName(w.host))} <span class="mono">${esc(shortId(w.did, 14, 8))}</span><br>
      free <b>${rbt(w.balance)}</b> · locked ${rbt(w.locked)} · pledged ${rbt(w.pledged)}${w.error ? ` <span class="muted">${esc(w.error)}</span>` : ""}` : "-";
    const ql = fa.faucet_quorums;
    $("#f-faucet").innerHTML = `<h2>Faucet ${badge(fa.ok ? "ready" : "not ready")} <span class="sub">${esc(ago(fa.checked))}</span></h2>
      <div class="sub" style="margin-bottom:8px">${esc(fa.note)}</div>
      <div class="kv"><div class="k">Faucet DID</div><div>${wallet(fa.faucet)}</div>
      <div class="k">Faucet quorum</div><div>${wallet(fa.quorum)}</div>
      <div class="k">Faucet node's quorum list</div><div>${ql == null ? "-" : ql.length === 1 ? "exactly one: the faucet quorum" :
        `<span style="color:var(--bad)">${ql.length} entries - must be exactly the faucet quorum</span>`}</div></div>
      <div class="sub" style="margin-top:8px">All RBT a test uses comes from here, in whole tokens, one faucet transfer at a time (rc.fund_did).</div>`;
  }
  function renderFullnodes(f) {
    const el = $("#f-fullnodes");
    if (!f) { el.innerHTML = "<h2>Fullnodes</h2><div class='muted'>not read yet</div>"; return; }
    if (f.error) { el.innerHTML = `<h2>Fullnodes</h2><div class="note">${esc(f.error)}</div>`; return; }
    const fns = f.fullnodes || [];
    const head = fns.map(x => `<th class="num">${esc(nodeName(x.host))}</th>`).join("");
    const reasons = (f.reasons || []).map(r => `<tr><td>${esc(r.reason)}</td><td class="num">${r.total}</td>${
      fns.map(x => `<td class="num">${r.by[x.host] || ""}</td>`).join("")}</tr>`).join("");
    const dis = f.disagreements || {count: 0, sample: []};
    el.innerHTML = `<h2>Fullnodes <span class="sub">each validates every published transaction on its own · ${esc(ago(f.checked))}</span></h2>
      <div class="tablewrap"><table><thead><tr><th>Fullnode</th><th class="num">Accepted (processed)</th><th class="num">Rejected</th><th>Database</th></tr></thead><tbody>${
        fns.map(x => `<tr><td>${esc(x.host)}</td><td class="num">${x.processed == null ? "-" : x.processed}</td><td class="num">${x.error ? "-" : x.invalid}</td><td>${x.error ? `<span style="color:var(--bad)">${esc(x.error)}</span>` : "read"}</td></tr>`).join("")}</tbody></table></div>
      <h2 style="margin-top:14px">Rejection reasons <span class="sub">IDs masked so the same failure groups</span></h2>
      ${reasons ? `<div class="tablewrap"><table><thead><tr><th>Reason</th><th class="num">Total</th>${head}</tr></thead><tbody>${reasons}</tbody></table></div>` : "<div class='muted'>none</div>"}
      <h2 style="margin-top:14px">Fullnodes that disagree <span class="sub">the same transaction accepted by one and rejected by another</span></h2>
      ${dis.count ? `<div>${dis.count} transaction(s). ${dis.sample.slice(0, 8).map(d => `<div class="small"><span class="mono">${esc(shortId(d.txid, 12, 4))}</span> rejected by ${esc(d.rejected_by.map(nodeName).join(", "))}, accepted by ${esc(d.accepted_by.map(nodeName).join(", "))} - ${esc(d.reason)}</div>`).join("")}</div>` : "<div class='muted'>none</div>"}`;
  }
  function renderMachines() {
    $("#f-machines").innerHTML = (data.machines || []).map(m => {
      const frac = m.nodes ? m.up / m.nodes : 0;
      const cls = !m.nodes ? "machine nonode" : "machine" + (machine === m.ip ? " sel" : "");
      return `<div class="${cls}" data-ip="${esc(m.ip)}" title="${esc(m.ip)} - ${esc(m.role)}">
        <div class="ip">.${esc(m.ip.split(".").pop())}</div><div class="role">${esc(m.role)}</div>
        <div class="small">${m.nodes ? `${m.up}/${m.nodes} up` : "no node"}</div>
        ${m.nodes ? `<div class="bar"><i style="width:${Math.round(frac * 100)}%"></i></div>` : ""}</div>`;
    }).join("");
    $("#f-out").innerHTML = (data.commented || []).length ? "Commented out of hosts.txt: " +
      data.commented.map(c => `<span title="${esc(c.note)}">${esc(c.ip)}</span>`).join(", ") : "";
    $$("#f-machines .machine:not(.nonode)").forEach(el => el.onclick = () => {
      machine = machine === el.dataset.ip ? null : el.dataset.ip;
      renderMachines(); renderNodes();
    });
  }
  function problem(n) {
    if (!n.up) return "down";
    if (n.role === "fullnode") return "";
    if (!n.dids) return "DIDs not read";
    if (n.dids.length !== n.did_target) return `holds ${n.dids.length} DIDs, layout says ${n.did_target}`;
    return "";
  }
  function renderNodes() {
    const f = ($("#f-filter").value || "").toLowerCase();
    const onlyBad = $("#f-problems").checked;
    const rows = (data.nodes || []).filter(n => (!machine || n.machine === machine) &&
      (!onlyBad || problem(n)) &&
      (!f || [n.host, n.role, n.peer_id, (n.dids || []).join(" "), (n.quorums || []).join(" ")].join(" ").toLowerCase().includes(f)));
    $("#f-count").textContent = `${rows.length} of ${(data.nodes || []).length} nodes`;
    $("#f-nodes").innerHTML = `<thead><tr><th>Node</th><th>Role</th><th class="num">Slot</th><th>Up</th><th>Peer ID</th><th class="num">DIDs</th><th>Quorum list</th><th>Build</th><th>Note</th></tr></thead><tbody>${
      rows.map(n => {
        const p = problem(n);
        const dids = n.dids ? `${n.dids.length}/${n.did_target}` : `?/${n.did_target}`;
        const q = n.quorums == null ? "-" : n.quorums.length ? n.quorums.map(x => shortId(x, 8, 4)).join(", ") : "none";
        const main = `<tr class="click" data-h="${esc(n.host)}"><td class="mono">${esc(n.host)}</td><td>${esc(n.role)}</td><td class="num">${n.slot}</td>
          <td>${badge(n.up ? "up" : "down")}</td><td class="mono small">${esc(shortId(n.peer_id, 12, 6))}</td>
          <td class="num"${p && n.up ? ' style="color:var(--warn)"' : ""}>${esc(dids)}</td><td class="mono small">${esc(q)}</td>
          <td class="small">${esc(n.build || "")}</td><td class="small muted">${esc(p || (n.up ? "" : n.note))}</td></tr>`;
        if (!open.has(n.host)) return main;
        return main + `<tr class="detail"><td colspan="9"><div class="kv">
          <div class="k">DIDs</div><div class="mono small">${(n.dids || []).map(esc).join("<br>") || "-"}</div>
          <div class="k">Quorum list</div><div class="mono small">${(n.quorums || []).map(esc).join("<br>") || "none"}</div>
          <div class="k">Peer ID</div><div class="mono small">${esc(n.peer_id || "-")}</div>
          <div class="k">Ports</div><div>API ${n.api_port} · Postgres ${n.db_port}</div>
          <div class="k">On its machine</div><div class="mono small">~/Desktop/rubix/rubix-lab/nodes/node${n.slot} · ${esc(n.unit)} · ${esc(n.container)}</div>
          <div class="k">Last read</div><div>liveness ${esc(ago(n.checked))} · DIDs ${esc(ago(n.inv_checked))}</div></div></td></tr>`;
      }).join("")}</tbody>`;
    $$("#f-nodes tr.click").forEach(tr => tr.onclick = () => {
      const h = tr.dataset.h;
      open.has(h) ? open.delete(h) : open.add(h);
      renderNodes();
    });
  }
  $("#f-filter").oninput = () => data && renderNodes();
  $("#f-problems").onchange = () => data && renderNodes();
  $("#f-refresh").onclick = async () => {
    try { await post("/api/fleet/refresh", {}); $("#f-msg").innerHTML = "<div class='note blue'>Reading the fleet now - it fills in within a few seconds.</div>"; }
    catch (e) { $("#f-msg").innerHTML = `<div class="note">${esc(e.message)}</div>`; }
  };
  $("#f-builds").onclick = async () => {
    try { await post("/api/fleet/builds", {}); $("#f-msg").innerHTML = "<div class='note blue'>Collecting every node's build over SSH (one connection per machine)…</div>"; }
    catch (e) { $("#f-msg").innerHTML = `<div class="note">${esc(e.message)}</div>`; }
  };
  await load();
  every(8000, load);
}

// ---------------------------------------------------------------------------
// Run builder
// ---------------------------------------------------------------------------

async function pageRun(main) {
  main.innerHTML = `<h1>Run builder</h1>
    <div class="sub">Pick catalogue cases and their scale knobs. The plan on the right is worked out by the runner's own
    code against the fleet the UI last read, and checked again when you press Start.</div>
    <div class="toolbar"><span class="tab on">Catalogue run</span>
    <span class="tab off" title="Custom runs (senders, receivers, quorums, assets, patterns) come in the next phase">Custom run - next phase</span></div>
    <div class="builder"><div id="b-left"></div><div class="panel planbox" id="b-plan"><div class="muted">working out the plan…</div></div></div>`;
  const cat = await api("/api/catalogue");
  const saved = store("form") || {};
  const known = new Set(cat.cases.map(c => c.id));
  const knobDefault = Object.fromEntries(cat.knobs.map(k => [k.name, k.ui_default]));
  const form = {
    cases: new Set((saved.cases || []).filter(c => known.has(c))),
    knobs: Object.assign({}, knobDefault, saved.knobs || {}),
  };
  const byUnit = {};
  cat.cases.forEach(c => (byUnit[c.unit] = byUnit[c.unit] || []).push(c));

  function unitNeeds(u) {
    const s = u.spec;
    const bits = [];
    if (s.senders) bits.push((s.senders === "all" ? "every free node" : s.senders) + " sender(s)");
    if (s.receivers) bits.push(s.receivers + " receiver(s)");
    else if (s.senders) bits.push("senders receive");
    if (s.quorums) bits.push(s.quorums + " quorum(s)");
    if (s.fund) bits.push("fund " + s.fund + " RBT each");
    if (s.quorum_fund != null) bits.push("quorum fund " + s.quorum_fund);
    if (s.same_node) bits.push("all on one node");
    return bits.join(" · ") || "reads the fleet itself";
  }
  function flags(u) {
    return (u.first ? '<span class="tag ex">first</span>' : "") + (u.exclusive && !u.first ? '<span class="tag ex">runs alone</span>' : "") +
      (u.last ? '<span class="tag ex">last</span>' : "");
  }
  $("#b-left").innerHTML = (cat.error ? `<div class="note red">${esc(cat.error)}</div>` : "") + `
    <div class="panel"><div class="toolbar" style="margin-top:0"><h2 style="margin:0">Cases <span class="sub">master-catalogue.csv, in run order</span></h2>
      <span class="spacer"></span><span id="b-count" class="sub"></span>
      <button class="small" id="b-all">all</button><button class="small" id="b-none">none</button></div>
      ${cat.units.map(u => `<div class="unit"><div class="uh">
        <input type="checkbox" class="b-unit" data-unit="${esc(u.name)}" title="the whole unit">
        <b>${esc(u.name)}</b>${flags(u)}<span class="needs">${esc(unitNeeds(u))}</span></div>
        ${(byUnit[u.name] || []).map(c => `<label class="case"><input type="checkbox" class="b-case" value="${esc(c.id)}">
          <div class="text"><div><span class="id">${esc(c.id)}</span> <span class="tag">${esc(c.asset)}</span><span class="tag">${esc(c.group)}</span>${c.priority ? `<span class="tag">${esc(c.priority)}</span>` : ""}${c.no_rerun ? '<span class="tag" title="NO_DELAY_RERUN: runs once, without delay">once</span>' : '<span class="tag" title="runs without delay, then again with delay">twice</span>'}</div>
          <div>${esc(c.case) || '<span class="muted">no row in master-catalogue.csv</span>'}</div>
          <div class="exp">Expected: ${esc(c.expected)}</div>
          <details><summary>more</summary>
            <div><b>Also checked:</b> ${esc(c.checks)}</div><div><b>Setup:</b> ${esc(c.setup)} · <b>Quorum:</b> ${esc(c.quorum_setup)}</div>
            <div><b>Code:</b> ${esc(c.code_ref)} · <b>Module:</b> ${esc(c.module)}</div></details></div></label>`).join("")}</div>`).join("")}
    </div>
    <div class="panel"><div class="toolbar" style="margin-top:0"><h2 style="margin:0">Knobs <span class="sub">from test_runner.py's options; defaults are the catalogue's sizes</span></h2>
      <span class="spacer"></span><button class="small" id="b-reset">defaults</button></div>
      <div class="knobs" id="b-knobs"></div></div>`;

  function renderKnobs() {
    $("#b-knobs").innerHTML = cat.knobs.map(k => {
      const v = form.knobs[k.name];
      const changed = String(v) !== String(k.ui_default);
      let input;
      if (k.type === "bool") input = `<input type="checkbox" data-k="${k.name}" ${v ? "checked" : ""}>`;
      else if (k.type === "int" || k.type === "float")
        input = `<input type="number" data-k="${k.name}" step="${k.type === "int" ? 1 : "any"}" value="${esc(v)}">`;
      else input = `<input type="text" data-k="${k.name}" value="${esc(v)}" size="30">`;
      return `<div class="knob${changed ? " changed" : ""}"><label>${esc(k.flag)} ${input}</label>
        <div class="def">default ${esc(k.ui_default === "" ? "(empty)" : k.ui_default)}${String(k.ui_default) !== String(k.default) ? ` in the UI (terminal: ${esc(k.default)})` : ""}</div>
        <div class="help">${esc(k.help)}</div></div>`;
    }).join("");
    $$("#b-knobs [data-k]").forEach(el => el.onchange = el.oninput = () => {
      const k = cat.knobs.find(x => x.name === el.dataset.k);
      form.knobs[k.name] = k.type === "bool" ? el.checked : el.value;
      el.closest(".knob").classList.toggle("changed", String(form.knobs[k.name]) !== String(k.ui_default));
      changed();
    });
  }
  function syncChecks() {
    $$(".b-case").forEach(el => { el.checked = form.cases.has(el.value); });
    $$(".b-unit").forEach(el => {
      const ids = (byUnit[el.dataset.unit] || []).map(c => c.id);
      const n = ids.filter(id => form.cases.has(id)).length;
      el.checked = n === ids.length && n > 0;
      el.indeterminate = n > 0 && n < ids.length;
    });
    $("#b-count").textContent = `${form.cases.size} of ${cat.cases.length} picked`;
  }
  $$(".b-case").forEach(el => el.onchange = () => {
    el.checked ? form.cases.add(el.value) : form.cases.delete(el.value);
    syncChecks(); changed();
  });
  $$(".b-unit").forEach(el => el.onchange = () => {
    (byUnit[el.dataset.unit] || []).forEach(c => el.checked ? form.cases.add(c.id) : form.cases.delete(c.id));
    syncChecks(); changed();
  });
  $("#b-all").onclick = () => { cat.cases.forEach(c => form.cases.add(c.id)); syncChecks(); changed(); };
  $("#b-none").onclick = () => { form.cases.clear(); syncChecks(); changed(); };
  $("#b-reset").onclick = () => { form.knobs = Object.assign({}, knobDefault); renderKnobs(); changed(); };

  let timer = null, plan = null, confirming = false, starting = false, startError = null;
  function changed() {
    store("form", {cases: Array.from(form.cases), knobs: form.knobs});
    confirming = false; startError = null;
    clearTimeout(timer);
    timer = setTimeout(refreshPlan, 350);
  }
  function request() {
    const knobs = {};
    cat.knobs.forEach(k => {
      let v = form.knobs[k.name];
      if (k.type === "int" || k.type === "float") v = v === "" || v == null ? v : Number(v);
      knobs[k.name] = v;
    });
    return {mode: "catalogue", cases: cat.cases.map(c => c.id).filter(id => form.cases.has(id)), knobs};
  }
  async function refreshPlan() {
    if (!form.cases.size) { plan = null; renderPlan(); return; }
    try { plan = await post("/api/plan", request()); } catch (e) { plan = {ok: false, errors: [e.message], warnings: [], units: []}; }
    renderPlan();
  }
  function renderPlan() {
    const el = $("#b-plan");
    if (!form.cases.size) { el.innerHTML = "<h2>Plan</h2><div class='muted'>Pick at least one case.</div>"; return; }
    if (!plan) { el.innerHTML = "<h2>Plan</h2><div class='muted'>working out the plan…</div>"; return; }
    const busy = fleetBusy();
    const units = plan.units || [];
    const draw = plan.draw || {};
    el.innerHTML = `<h2>Plan ${plan.ok ? badge("ready") : badge("not ready")}</h2>
      ${msgList(plan.errors, plan.warnings)}
      ${plan.pool ? `<div class="kv" style="margin-bottom:10px">
        <div class="k">Cases</div><div>${(plan.order || []).length} in ${units.length} unit(s), in catalogue order</div>
        <div class="k">Pool</div><div>${plan.pool.nodes} usable nodes on ${plan.pool.machines} machines, ${plan.pool.dids} DIDs</div>
        <div class="k">Faucet draw, worst case</div><div><b>${rbt(draw.worst, 0)} RBT</b>${draw.faucet_free != null ? ` <span class="muted">(faucet holds ${rbt(draw.faucet_free, 0)})</span>` : ""}<br>
          <span class="small muted">if every participant held nothing; a run draws only shortfalls, so usually far less</span></div></div>` : ""}
      ${units.length ? `<div class="tablewrap"><table><thead><tr><th>Unit</th><th class="num">Nodes</th><th>Quorums</th><th>Participants</th><th class="num">Draw</th></tr></thead><tbody>${
        units.map(u => `<tr><td><b>${esc(u.name)}</b>${u.exclusive ? ' <span class="tag ex">alone</span>' : ""}${u.skip ? ` ${badge("SKIP")}` : ""}<div class="small muted">${esc(u.cases.join(", "))}</div></td>
          <td class="num">${u.nodes || "-"}</td>
          <td class="small">${u.quorums ? `${u.quorums} × ${rbt(u.quorum_target, 0)} RBT` : "-"}</td>
          <td class="small">${u.senders + u.receivers ? `${u.senders} s${u.receivers ? ` + ${u.receivers} r` : ""} × ${rbt(u.participant_target, 0)} RBT` : "-"}${u.extra ? `<br>+ ${rbt(u.extra, 0)} drawn by the case` : ""}</td>
          <td class="num">${rbt(u.draw, 0)}</td></tr>`).join("")}</tbody></table></div>
        <div class="small muted" style="margin-top:4px">Quorums are topped up to their target before the unit starts (test_runner.unit_targets); a pledge is held until the receiver spends onward.</div>` : ""}
      ${plan.command ? `<div style="margin-top:10px" class="small muted">The same run from a terminal:</div><pre class="cmd">${esc(plan.command)}</pre>` : ""}
      <div style="margin-top:14px">${busy ? `<div class="note">The fleet is busy: ${esc(busy)}</div>` : ""}
      ${startError ? `<div class="note red">${esc(startError)}</div>` : ""}
      ${confirming ? `<div class="note blue">Start this run on the fleet now? It resets the quorum lists of the nodes it uses and draws from the faucet.</div>
        <button class="primary" id="b-go" ${starting ? "disabled" : ""}>${starting ? "Starting…" : "Yes, start the run"}</button> <button id="b-cancel">Cancel</button>`
        : `<button class="primary" id="b-start" ${plan.ok && !busy ? "" : "disabled"}>Start run</button>`}</div>`;
    const s = $("#b-start");
    if (s) s.onclick = () => { confirming = true; renderPlan(); };
    const c = $("#b-cancel");
    if (c) c.onclick = () => { confirming = false; renderPlan(); };
    const g = $("#b-go");
    if (g) g.onclick = async () => {
      starting = true; renderPlan();
      try {
        const r = await post("/api/runs", request());
        location.hash = "#/runs/" + encodeURIComponent(r.id);
      } catch (e) {
        startError = e.message;
        if (e.body && e.body.plan) plan = e.body.plan;
      }
      starting = false; confirming = false; renderPlan();
    };
  }
  renderKnobs();
  syncChecks();
  refreshPlan();
  every(5000, () => { if (!confirming && !starting) refreshPlan(); });
}

// ---------------------------------------------------------------------------
// A run: live, or from history
// ---------------------------------------------------------------------------

async function pageLive(main, args) {
  let id = args && args[0];
  if (!id) {
    const cur = await api("/api/runs/current");
    if (!cur.id) {
      main.innerHTML = `<h1>Live run</h1><div class="empty">No run since the UI started. Start one from the
        <a href="#/run">Run builder</a>, or open a past one from <a href="#/reports">Reports</a>.</div>`;
      return;
    }
    id = cur.id;
  }
  main.innerHTML = `<div id="l-head"></div><div id="l-banner"></div><div id="l-progress"></div>
    <div class="panel"><h2>Cases <span class="sub">click a row for its database checks and full notes</span></h2><div class="tablewrap"><table id="l-cases"></table></div></div>
    <div class="row"><div class="panel grow"><h2>Units and participants</h2><div class="tablewrap"><table id="l-units"></table></div></div></div>
    <div class="row"><div class="panel grow"><h2>Refusals <span class="sub">grouped by the node's own reason; known product bugs tagged</span></h2><div id="l-refusals"></div></div>
      <div class="panel grow"><h2>Fullnode verdicts</h2><div id="l-fullnode"></div></div></div>
    <div id="l-conditions"></div>
    <div class="panel"><h2>Operations <span class="sub" id="l-opcount"></span></h2><div class="tablewrap"><table class="ops" id="l-ops"></table></div></div>
    <div class="panel" id="l-consolebox"><div class="toolbar" style="margin-top:0"><h2 style="margin:0">Console</h2><span class="spacer"></span>
      <label class="small"><input type="checkbox" id="l-follow" checked> follow</label></div><pre class="console" id="l-console"></pre></div>`;
  let snap = null, es = null, timer = null, consoleN = 0, lastSeq = 0, confirmStop = false;
  const openCases = new Set();

  async function load() {
    snap = await api("/api/runs/" + encodeURIComponent(id));
    lastSeq = Math.max(lastSeq, snap.seq || 0);
    render();
  }
  function soon() {
    if (timer) return;
    timer = setTimeout(() => { timer = null; load().catch(() => {}); }, 1200);
  }
  const running = () => snap && snap.live && (snap.status === "starting" || snap.status === "running");

  function render() {
    renderHead(); renderBanner(); renderProgress(); renderCases(); renderUnits();
    renderRefusals(); renderFullnode(); renderConditions(); renderOps();
    if (!consoleN && snap.console && snap.console.length) {
      appendConsole(snap.console);
      consoleN = snap.console_n || snap.console.length;
    }
    if (!snap.live && !(snap.console || []).length) $("#l-consolebox").hidden = true;
  }
  function renderHead() {
    const s = snap;
    const title = s.source === "terminal" ? "Run from a terminal" : "Run " + s.id;
    const started = s.started ? when(s.started) : when(s.started_at);
    $("#l-head").innerHTML = `<div class="row"><div class="grow"><h1>${esc(title)} ${badge(s.status)}</h1>
      <div class="sub">${esc(s.kind || "")}${s.cases_module ? " · cases " + esc(s.cases_module) : ""} · started ${esc(started)} · ${running() ? "running for" : "took"} ${esc(dur(s.elapsed))}${s.version ? " · build " + esc(s.version) : ""}</div>
      ${s.cmdline ? `<pre class="cmd">${esc(s.cmdline)}</pre>` : ""}</div>
      <div class="toolbar">${s.files ? fileLinks(s.files) : ""}
      ${s.can_stop ? (confirmStop ? `<span class="note red" style="margin:0">Stop this run? Transfers already sent keep going on the nodes.</span>
        <button class="danger" id="l-stop-yes">Yes, stop it</button><button id="l-stop-no">Keep running</button>` : `<button class="danger" id="l-stop">Stop run</button>`) : ""}</div></div>`;
    const st = $("#l-stop");
    if (st) st.onclick = () => { confirmStop = true; renderHead(); };
    const no = $("#l-stop-no");
    if (no) no.onclick = () => { confirmStop = false; renderHead(); };
    const yes = $("#l-stop-yes");
    if (yes) yes.onclick = async () => {
      try { await post("/api/runs/" + encodeURIComponent(id) + "/stop", {}); } catch (e) { alertNote(e.message); }
      confirmStop = false; load();
    };
  }
  function alertNote(text) { $("#l-banner").insertAdjacentHTML("afterbegin", `<div class="note red">${esc(text)}</div>`); }
  function renderBanner() {
    const s = snap;
    let html = "";
    if (s.exclusive_now && s.exclusive_now.length && running())
      html += `<div class="note">${esc(s.exclusive_now.join(", "))} runs alone: nothing else starts beside it, and the UI has stopped reading the fleet until it ends.</div>`;
    if (s.source === "terminal")
      html += `<div class="note blue">Started from ${esc((s.holder || {}).by || "a terminal")} (pid ${esc(s.pid)}). The UI shows its log; units, cases and figures appear in Reports when it has finished.</div>`;
    if (s.stop) {
      const checks = (s.stop.checks || []).map(c => `<div>${esc(c.when)}: ${c.note ? esc(c.note) : c.locked.length ?
        `<b>${c.locked.length} participant(s) still hold Locked RBT</b>: ` + c.locked.map(l => `${esc(nodeName(l.host))} ${esc(l.role)} ${rbt(l.locked)}`).join(", ") :
        `no Locked RBT on ${c.checked} participant(s)${c.unreadable ? `, ${c.unreadable} unreadable` : ""}`}</div>`).join("");
      html += `<div class="note">${esc(s.stop.message || "Stopped.")}<div style="margin-top:6px">${checks || ""}${s.stop.pending ? "<div>Checking the participants for RBT left Locked (30 s and 2 min after the stop)…</div>" : ""}</div></div>`;
    }
    if (s.note && !s.stop) html += `<div class="note${s.status === "finished" ? " blue" : ""}">${esc(s.note)}</div>`;
    const d = s.run_done;
    if (d && d.status === "finished")
      html += `<div class="note blue">Finished: ${d.passed} passed, ${d.failed} failed, ${d.skipped} skipped in ${esc(dur(d.duration))}.${(d.ledger || []).length ? `<div class="small" style="margin-top:4px">${(d.ledger || []).map(esc).join("<br>")}</div>` : ""}</div>`;
    if (!s.live && s.passed != null)
      html += `<div class="note blue">${s.passed} passed, ${s.failed} failed, ${s.skipped} skipped.</div>`;
    $("#l-banner").innerHTML = html;
  }
  function renderProgress() {
    const s = snap;
    if (!s.live) { $("#l-progress").innerHTML = ""; return; }
    const units = s.units || [];
    const cases = s.cases || [];
    const done = s.run_done && s.run_done.status === "finished";
    const chips = (s.phases || []).map(p => `<div class="chip"><b>${esc(p.phase)}</b>${p.ok === true || (p.ok == null && done) ? "✓ " : p.ok === false ? "✗ " : "… "}${esc(p.detail || "")}${
      p.excluded && p.excluded.length ? ` <span class="muted" title="${esc(p.excluded.join("\n"))}">(${p.excluded.length} excluded)</span>` : ""}</div>`);
    chips.push(`<div class="chip"><b>units</b>${units.filter(u => u.status === "done" || u.status === "error").length} / ${units.length} done</div>`);
    chips.push(`<div class="chip"><b>cases</b>${cases.filter(c => ["PASS", "FAIL", "SKIP"].includes(c.status)).length} / ${cases.length} done</div>`);
    chips.push(`<div class="chip"><b>operations</b>${s.ops_total || 0}${s.funding && (s.funding.ok + s.funding.refused) ? ` (${s.funding.ok + s.funding.refused} faucet top-ups)` : ""}</div>`);
    $("#l-progress").innerHTML = `<div class="chips" style="margin-bottom:16px">${chips.join("")}</div>`;
  }
  function statLines(tid) {
    return (snap.stats || []).filter(x => x.case === tid).map(x =>
      `<div class="small"><span class="muted">${esc(x.mode || "")}</span> ${x.ok} ok${x.refused ? `, <b style="color:var(--bad)">${x.refused} refused</b>` : ""}${x.known ? `, ${x.known} known-bug` : ""}${
        x.p50 != null ? ` · p50 ${secs(x.p50)} p95 ${secs(x.p95)} max ${secs(x.max)} s` : ""}</div>`).join("");
  }
  function renderCases() {
    const rows = (snap.cases || []).map(c => {
      const tid = c.test_id;
      const main = `<tr class="click" data-t="${esc(tid)}"><td class="mono"><b>${esc(tid)}</b></td><td>${esc(c.unit || "")}</td>
        <td>${badge(c.status || "waiting")}${c.mode && c.status === "running" ? ` <span class="small muted">${esc(c.mode)}</span>` : ""}</td>
        <td class="num">${c.seconds != null ? secs(c.seconds) : ""}</td><td>${statLines(tid)}</td>
        <td class="small">${esc(c.actual || "")}</td><td class="small">${esc(c.db || "")}</td><td class="small">${esc(c.fullnode || "")}</td></tr>`;
      if (!openCases.has(tid)) return main;
      const checks = (c.checks || []).map(k => `<tr><td>${esc(k.check)}</td><td>${k.ok === true ? badge("ok") : k.ok === false ? badge("FAIL") : badge("-")}</td><td class="small muted">${esc(k.mode || "")}</td><td class="small">${esc(k.detail)}</td></tr>`).join("");
      const known = Object.entries(c.known_bugs || {}).map(([k, n]) => `<div class="small">${n}× ${esc(k)}</div>`).join("");
      const buckets = c.fullnode_buckets ? Object.entries(c.fullnode_buckets).filter(([, n]) => n).map(([k, n]) => `${esc(k.replace(/_/g, " "))}: ${n}`).join(" · ") : "";
      return main + `<tr class="detail"><td colspan="8"><div class="kv">
        ${c.case ? `<div class="k">Case</div><div>${esc(c.case)}</div>` : ""}${c.expected ? `<div class="k">Expected</div><div>${esc(c.expected)}</div>` : ""}
        <div class="k">Actual</div><div>${esc(c.actual || "-")}</div><div class="k">Note</div><div class="small">${esc(c.note || "-")}</div>
        ${known ? `<div class="k">Known product bugs</div><div>${known}</div>` : ""}
        ${buckets ? `<div class="k">Fullnode, without vs with delay</div><div class="small">${buckets}</div>` : ""}</div>
        ${checks ? `<table style="margin-top:8px"><thead><tr><th>DB check</th><th>Result</th><th>Run</th><th>Detail</th></tr></thead><tbody>${checks}</tbody></table>` : ""}</td></tr>`;
    }).join("");
    $("#l-cases").innerHTML = `<thead><tr><th>Case</th><th>Unit</th><th>Status</th><th class="num">Seconds</th><th>Operations</th><th>Actual</th><th>DB check</th><th>Fullnode</th></tr></thead><tbody>${rows || `<tr><td colspan="8" class="muted">no case yet</td></tr>`}</tbody>`;
    $$("#l-cases tr.click").forEach(tr => tr.onclick = () => {
      const t = tr.dataset.t;
      openCases.has(t) ? openCases.delete(t) : openCases.add(t);
      renderCases();
    });
  }
  function people(list) {
    if (!list || !list.length) return "-";
    const names = list.map(e => nodeName(e.host));
    const text = names.length > 6 ? names.slice(0, 6).join(", ") + ` +${names.length - 6}` : names.join(", ");
    return `<span title="${esc(list.map(e => e.host + " " + (e.did || "")).join("\n"))}">${list.length}: ${esc(text)}</span>`;
  }
  function renderUnits() {
    const rows = (snap.units || []).map(u => `<tr><td><b>${esc(u.name)}</b>${u.exclusive ? ' <span class="tag ex">alone</span>' : ""}<div class="small muted">${esc((u.cases || []).join(", "))}</div></td>
      <td>${badge(u.status || (u.ended || !snap.live ? "done" : "waiting"))}${u.current ? ` <span class="small">${esc(u.current)}</span>` : ""}</td>
      <td class="small">${people(u.quorums)}</td><td class="small">${people(u.senders)}</td><td class="small">${people(u.receivers)}</td>
      <td class="num">${u.seconds != null ? dur(u.seconds) : ""}</td>
      <td class="small">${(u.dirty || []).length ? `ex-quorum spenders (known bug): ${esc(u.dirty.join(", "))}` : ""}${u.error ? `<span style="color:var(--bad)">${esc(u.error)}</span>` : ""}</td></tr>`).join("");
    $("#l-units").innerHTML = `<thead><tr><th>Unit</th><th>Status</th><th>Quorums</th><th>Senders</th><th>Receivers</th><th class="num">Took</th><th>Note</th></tr></thead><tbody>${rows || `<tr><td colspan="7" class="muted">no unit yet</td></tr>`}</tbody>`;
  }
  function renderRefusals() {
    const r = snap.refusals || [];
    $("#l-refusals").innerHTML = r.length ? `<table><thead><tr><th class="num">Count</th><th>Reason</th><th>Cases</th></tr></thead><tbody>${
      r.map(x => `<tr><td class="num">${x.count}</td><td class="small">${esc(x.reason)}${x.known_bug ? `<div><span class="tag ex">KNOWN PRODUCT BUG</span> ${esc(x.known_bug)}</div>` : ""}</td><td class="small">${esc((x.cases || []).join(", "))}</td></tr>`).join("")}</tbody></table>`
      : "<div class='muted'>none</div>";
  }
  function renderFullnode() {
    const d = snap.run_done || {};
    const totals = {};
    (snap.cases || []).forEach(c => Object.entries(c.fullnode_buckets || {}).forEach(([k, n]) => { totals[k] = (totals[k] || 0) + n; }));
    const buckets = Object.entries(totals).filter(([, n]) => n).map(([k, n]) => `<div>${esc(k.replace(/_/g, " "))}: <b>${n}</b></div>`).join("");
    const reasons = (d.fullnode_reasons || []).map(r => `<tr><td class="small">${esc(r.reason)}</td><td class="small">${esc(r.by.join(", "))}</td><td class="small">${esc(r.cases.join(", "))}</td></tr>`).join("");
    $("#l-fullnode").innerHTML = (buckets ? `<div style="margin-bottom:8px"><b>Same case without and with delay</b>${buckets}</div>` : "") +
      (reasons ? `<table><thead><tr><th>Rejected because</th><th>By</th><th>Cases</th></tr></thead><tbody>${reasons}</tbody></table>` : "") +
      ((d.disagreed || []).length ? `<div class="note">Fullnodes reached different verdicts in ${esc(d.disagreed.join(", "))}</div>` : "") +
      (!buckets && !reasons ? `<div class="muted">${snap.run_done ? "no fullnode rejected anything this run" : "per case in the Fullnode column; the run's summary appears here when it ends"}</div>` : "");
  }
  function renderConditions() {
    const c = snap.conditions;
    $("#l-conditions").innerHTML = c && c.length ? `<div class="panel"><h2>Run conditions <span class="sub">from the report</span></h2><div class="kv">${
      c.map(([k, v]) => `<div class="k">${esc(k)}</div><div class="small">${esc(v)}</div>`).join("")}</div></div>` : "";
  }
  function opRow(o) {
    return `<tr><td class="small">${esc(when(o.t || o.at).slice(11))}</td><td class="mono small">${esc(o.case || o.case_id || (o.funding ? "faucet" : ""))}</td>
      <td class="small muted">${esc(o.mode || "")}</td><td class="small">${esc(nodeName(o.host))}</td>
      <td class="small">${o.kind === "ft_mint" ? "FT mint" : (o.rbt ? rbt(o.rbt) + " RBT" : "") + ((o.assets || []).length ? " " + esc(o.assets.join("+")) : "")}</td>
      <td>${o.ok ? badge("ok") : badge("FAIL")}</td><td class="num small">${o.secs != null ? secs(o.secs) : ""}</td>
      <td class="small muted">${esc(o.pacing || "")}</td><td class="mono small">${esc(shortId(o.txid, 10, 0))}</td>
      <td class="small">${o.ok ? "" : esc(o.reason || "")}${o.known_bug ? ' <span class="tag ex">known</span>' : ""}</td></tr>`;
  }
  const OPS_HEAD = `<thead><tr><th>Time</th><th>Case</th><th>Run</th><th>From</th><th>What</th><th>Result</th><th class="num">Secs</th><th>Paced</th><th>Tx</th><th>Refusal</th></tr></thead>`;
  function renderOps() {
    const ops = snap.ops || [];
    $("#l-opcount").textContent = `${snap.ops_total || 0} in all, newest first${ops.length < (snap.ops_total || 0) ? ", last " + ops.length + " shown" : ""}`;
    $("#l-ops").innerHTML = OPS_HEAD + `<tbody>${ops.map(opRow).join("") || `<tr><td colspan="10" class="muted">none yet</td></tr>`}</tbody>`;
  }
  function addOp(e) {
    const tb = $("#l-ops tbody");
    if (!tb) return;
    tb.insertAdjacentHTML("afterbegin", opRow(e));
    while (tb.rows.length > 300) tb.deleteRow(tb.rows.length - 1);
  }
  function appendConsole(lines) {
    const pre = $("#l-console");
    pre.appendChild(document.createTextNode(lines.join("\n") + "\n"));
    if (pre.childNodes.length > 2000) pre.removeChild(pre.firstChild);
    if ($("#l-follow").checked) pre.scrollTop = pre.scrollHeight;
  }

  function connect() {
    if (es || !running()) return;
    es = new EventSource(`/api/runs/${encodeURIComponent(id)}/stream?since=${lastSeq}&csince=${consoleN}`);
    es.addEventListener("ev", m => {
      const e = JSON.parse(m.data);
      lastSeq = Math.max(lastSeq, e.seq);
      if (e.type === "op") addOp(e);
      soon();
    });
    es.addEventListener("console", m => {
      const d = JSON.parse(m.data);
      appendConsole(d.lines);
      consoleN = d.from + d.lines.length - 1;
    });
    es.addEventListener("end", () => { es.close(); es = null; load(); });
    es.addEventListener("reset", () => { es.close(); es = null; load().then(connect); });
    es.onerror = () => {   // reconnect from where this page is, not where it started
      if (es) es.close();
      es = null;
      setTimeout(() => load().then(connect).catch(() => {}), 3000);
    };
  }
  onLeave(() => { if (es) es.close(); es = null; clearTimeout(timer); });
  await load();
  connect();
  every(10000, () => {
    if (running() || (snap && snap.stop && snap.stop.pending)) load().then(connect).catch(() => {});
  });
}

// ---------------------------------------------------------------------------
// Reports
// ---------------------------------------------------------------------------

async function pageReports(main) {
  main.innerHTML = `<h1>Reports</h1>
    <div class="sub">Every run: started here, from a terminal, and the older reports found in reports/ (looked for every minute).</div>
    <div class="toolbar"><input type="text" id="r-filter" placeholder="filter: case, id, build, status" size="34">
      <select id="r-kind"><option value="">every kind</option><option>catalogue</option><option>smoke</option><option>probe</option></select>
      <span class="spacer"></span><span id="r-count" class="sub"></span><button id="r-scan">Look for new reports now</button></div>
    <div class="panel"><div class="tablewrap"><table id="r-table"></table></div></div>`;
  let runs = [];
  async function load() {
    runs = (await api("/api/runs")).runs;
    render();
  }
  function render() {
    const f = ($("#r-filter").value || "").toLowerCase();
    const kind = $("#r-kind").value;
    const rows = runs.filter(r => (!kind || r.kind === kind) &&
      (!f || [r.id, r.name, r.cases, r.status, r.version, r.note, r.source].join(" ").toLowerCase().includes(f)));
    $("#r-count").textContent = `${rows.length} of ${runs.length}`;
    $("#r-table").innerHTML = `<thead><tr><th>Started</th><th>Kind</th><th>From</th><th>Cases</th><th>Status</th><th class="num">Pass</th><th class="num">Fail</th><th class="num">Skip</th><th class="num">Took</th><th>Build</th><th>Files</th></tr></thead><tbody>${
      rows.map(r => `<tr class="click" data-id="${esc(r.id)}"><td class="small">${esc(when(r.started_at))}</td><td>${esc(r.kind || "")}</td>
        <td class="small">${esc(r.source === "ui" ? "web UI" : r.source || "")}</td>
        <td class="small" title="${esc(r.cases || "")}">${esc(shortId(r.cases || r.name || "", 60, 0))}${r.note ? `<div class="muted">${esc(r.note)}</div>` : ""}</td>
        <td>${badge(r.status || "")}</td><td class="num">${r.passed ?? ""}</td><td class="num">${r.failed ? `<b style="color:var(--bad)">${r.failed}</b>` : (r.failed ?? "")}</td><td class="num">${r.skipped ?? ""}</td>
        <td class="num small">${esc(dur(r.duration))}</td><td class="small">${esc(shortId(r.version || "", 28, 0))}</td>
        <td class="small">${fileLinks(r.files)}</td></tr>`).join("") || `<tr><td colspan="11" class="muted">no runs found yet</td></tr>`}</tbody>`;
    $$("#r-table tr.click").forEach(tr => tr.onclick = e => {
      if (e.target.closest("a")) return;
      location.hash = "#/runs/" + encodeURIComponent(tr.dataset.id);
    });
  }
  $("#r-filter").oninput = render;
  $("#r-kind").onchange = render;
  $("#r-scan").onclick = async () => { await post("/api/import", {}); setTimeout(load, 6000); };
  await load();
  every(15000, load);
}

// ---------------------------------------------------------------------------

window.addEventListener("hashchange", route);
refreshStatus().then(route);
setInterval(refreshStatus, 4000);
