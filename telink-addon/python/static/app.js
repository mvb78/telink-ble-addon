"use strict";

const $ = (id) => document.getElementById(id);

const logEl = $("log-output");
const lampList = $("lamp-list");
const groupList = $("group-list");
const targetLabel = $("target-label");
const targetSub = $("target-sub");
const targetSel = $("target-selector");
const discoverBtn = $("discover-btn");
const groupCreateForm = $("group-create");
const groupNameInput = $("group-name");
const membershipPanel = $("membership-panel");
const membershipList = $("membership-list");
const groupEditPanel = $("group-edit-panel");
const groupEditName = $("group-edit-name");
const groupMembers = $("group-members");
const groupAddLamp = $("group-add-lamp");
const groupAddBtn = $("group-add-btn");
const queryResults = $("query-results");

let lamps = [];
let groups = [];
// activeTarget: null = all, {type:"lamp", mac, name} or {type:"group", name, address}
let activeTarget = null;

// ── helpers ──────────────────────────────────────────────────────────────

function log(msg) {
  const ts = new Date().toLocaleTimeString();
  logEl.textContent += `[${ts}] ${msg}\n`;
  logEl.scrollTop = logEl.scrollHeight;
}

async function api(path, body, method) {
  try {
    const opts = {};
    if (body !== undefined || method) {
      opts.method = method || "POST";
      opts.headers = { "Content-Type": "application/json" };
      if (body !== undefined) opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    return await res.json();
  } catch (e) {
    log(`ERROR: ${e.message}`);
    return { ok: false, msg: e.message };
  }
}

// ── target selection ─────────────────────────────────────────────────────

function setTarget(target, label, sub) {
  activeTarget = target;
  targetLabel.textContent = label;
  targetSub.textContent = sub;
  document.querySelectorAll("#sidebar li").forEach((el) => {
    el.classList.toggle("active",
      (target?.type === "lamp" && el.dataset.mac === target.mac) ||
      (target?.type === "group" && el.dataset.group === target.name) ||
      (target === null && el.dataset.all === "1"));
  });
  syncSelector();
  renderMembership();
}

function syncSelector() {
  if (!activeTarget) { targetSel.value = "all"; return; }
  const val = activeTarget.type === "lamp" ? activeTarget.mac : "group:" + activeTarget.name;
  if ([...targetSel.options].some((o) => o.value === val)) targetSel.value = val;
}

function cmdBody() {
  if (!activeTarget) return {};
  if (activeTarget.type === "lamp") return { mac: activeTarget.mac };
  // Group commands target a mesh destination address (`dst`), not a lamp
  // mesh filter (`addr`). The add-on treats `addr` as a lamp-selector.
  return { dst: activeTarget.address };
}

async function sendCmd(cmd, extra) {
  const body = { ...cmdBody(), ...extra };
  const label = activeTarget ? activeTarget.name || targetLabel.textContent : "All lamps";
  log(`→ ${cmd} → ${label}`);
  const res = await api(`api/command/${cmd}`, body);
  log(`  ${res.ok ? "OK" : "FAILED"}: ${res.msg || JSON.stringify(res)}`);
  if (res.results) renderQueryResults(res.results);
  return res;
}

// ── sidebar: lamps ───────────────────────────────────────────────────────

async function loadLamps() {
  lamps = await api("api/lamps") || [];
  lampList.innerHTML = "";

  const liAll = document.createElement("li");
  liAll.dataset.all = "1";
  liAll.className = "lamp-item";
  liAll.innerHTML = `<span class="name">All lamps</span>`;
  liAll.onclick = () => setTarget(null, "All lamps", "every saved lamp");
  lampList.appendChild(liAll);

  for (const l of lamps) {
    const li = document.createElement("li");
    li.className = "lamp-item";
    li.dataset.mac = l.mac;
    const label = l.name || l.mac;

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = label;
    name.title = `${l.mac} · pw=${l.password}`;
    name.onclick = () => selectLamp(l);

    const onBtn = document.createElement("button");
    onBtn.className = "mini on";
    onBtn.textContent = "on";
    onBtn.title = `Turn ON ${label}`;
    onBtn.onclick = (e) => { e.stopPropagation(); quickToggle(l, true); };

    const offBtn = document.createElement("button");
    offBtn.className = "mini off";
    offBtn.textContent = "off";
    offBtn.title = `Turn OFF ${label}`;
    offBtn.onclick = (e) => { e.stopPropagation(); quickToggle(l, false); };

    const addrInput = document.createElement("input");
    addrInput.type = "number";
    addrInput.min = "1";
    addrInput.max = "63";
    addrInput.value = l.mesh_address ?? "";
    addrInput.placeholder = "addr";
    addrInput.title = "Unicast mesh address (1-63). Leave the lamp advertising, then click Assign.";
    addrInput.className = "mini-addr";
    addrInput.onclick = (e) => e.stopPropagation();

    const assignBtn = document.createElement("button");
    assignBtn.className = "mini assign";
    assignBtn.textContent = l.mesh_address ? "re-assign" : "assign";
    assignBtn.title = "Provision this lamp with the given unicast mesh address (opcode 0xE0)";
    assignBtn.onclick = async (e) => {
      e.stopPropagation();
      const addrVal = parseInt(addrInput.value, 10);
      if (!addrVal || addrVal < 1 || addrVal > 63) {
        log("ERROR: mesh address must be 1-63");
        return;
      }
      const res = await api(`api/lamp/${l.mac}/assign-addr`, { addr: addrVal });
      log(`  ${res.ok ? "OK" : "FAILED"}: ${res.msg || ""}`);
      if (res.ok) loadLamps();
    };

    li.append(name, onBtn, offBtn, addrInput, assignBtn);
    lampList.appendChild(li);

    const opt = document.createElement("option");
    opt.value = l.mac;
    opt.textContent = label;
    targetSel.appendChild(opt);
  }
}

function selectLamp(l) {
  setTarget({ type: "lamp", mac: l.mac, name: l.name || l.mac },
    l.name || l.mac, `${l.mac} · password ${l.password}`);
}

async function quickToggle(lamp, stateOn) {
  const body = { mac: lamp.mac };
  log(`→ ${stateOn ? "on" : "off"} → ${lamp.name || lamp.mac}`);
  const res = await api(`api/command/${stateOn ? "on" : "off"}`, body);
  log(`  ${res.ok ? "OK" : "FAILED"}: ${res.msg || ""}`);
}

// ── sidebar: groups ──────────────────────────────────────────────────────

async function loadGroups() {
  groups = await api("api/groups") || [];
  groupList.innerHTML = "";
  groups.forEach((g) => {
    const li = document.createElement("li");
    li.className = "group-item";
    li.dataset.group = g.name;

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = g.name;
    name.title = `group @ address ${g.address} (0x${g.address.toString(16).toUpperCase()})`;
    name.onclick = () => setTarget(
      { type: "group", name: g.name, address: g.address },
      g.name, `group @ 0x${g.address.toString(16).toUpperCase()}`);

    const del = document.createElement("button");
    del.className = "mini del";
    del.textContent = "\u00d7";
    del.title = `Delete group '${g.name}'`;
    del.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete group '${g.name}'?`)) return;
      await api(`api/groups/${encodeURIComponent(g.name)}`, undefined, "DELETE");
      log(`deleted group '${g.name}'`);
      if (activeTarget?.type === "group" && activeTarget.name === g.name)
        setTarget(null, "All lamps", "every saved lamp");
      loadGroups();
    };

    li.append(name, del);
    groupList.appendChild(li);

    const opt = document.createElement("option");
    opt.value = "group:" + g.name;
    opt.textContent = g.name + " (group)";
    targetSel.appendChild(opt);
  });
}

groupCreateForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = groupNameInput.value.trim();
  if (!name) return;
  const res = await api("api/groups", { name });
  if (res.error) log(`ERROR: ${res.error}`);
  else log(`created group '${res.name}' @ 0x${res.address.toString(16).toUpperCase()}`);
  groupNameInput.value = "";
  loadGroups();
});

// ── group membership (lamp view) & members (group view) ──────────────────

function macLabel(mac) {
  const l = lamps.find((x) => x.mac === mac);
  return l ? (l.name || l.mac) : mac;
}

function renderMembership() {
  if (activeTarget?.type === "lamp") { renderLampMembership(); return; }
  if (activeTarget?.type === "group") { renderGroupEdit(); return; }
  membershipPanel.hidden = true;
  groupEditPanel.hidden = true;
}

function renderLampMembership() {
  membershipPanel.hidden = false;
  groupEditPanel.hidden = true;
  membershipList.innerHTML = "";
  if (!groups.length) {
    membershipList.innerHTML = `<span class="hint">no groups defined — create one in the sidebar</span>`;
    return;
  }
  groups.forEach((g) => {
    const row = document.createElement("label");
    row.className = "member-row";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = (g.lamps || []).includes(activeTarget.mac);
    cb.onchange = () => toggleMembership(g, cb.checked);
    const span = document.createElement("span");
    span.textContent = g.name;
    const count = document.createElement("small");
    const inGroup = (g.lamps || []).length;
    count.textContent = ` (${inGroup} lamp${inGroup === 1 ? "" : "s"})`;
    row.append(cb, span, count);
    membershipList.appendChild(row);
  });
}

function renderGroupEdit() {
  membershipPanel.hidden = true;
  groupEditPanel.hidden = false;
  const g = groups.find((x) => x.name === activeTarget.name);
  if (!g) return;
  groupEditName.textContent = g.name;
  groupMembers.innerHTML = "";
  const members = g.lamps || [];
  if (!members.length) {
    groupMembers.innerHTML = `<li class="hint">no lamps in this group yet</li>`;
  } else {
    members.forEach((mac) => {
      const li = document.createElement("li");
      li.className = "member-lamp";
      const span = document.createElement("span");
      span.textContent = macLabel(mac);
      const rem = document.createElement("button");
      rem.className = "mini del";
      rem.textContent = "remove";
      rem.onclick = () => removeLampFromGroup(g, mac);
      li.append(span, rem);
      groupMembers.appendChild(li);
    });
  }

  // add-lamp dropdown: lamps not already in this group
  groupAddLamp.innerHTML = "";
  const addable = lamps.filter((l) => !members.includes(l.mac));
  if (!addable.length) {
    groupAddLamp.innerHTML = `<option value="">all lamps already in group</option>`;
    groupAddBtn.disabled = true;
  } else {
    addable.forEach((l) => {
      const opt = document.createElement("option");
      opt.value = l.mac;
      opt.textContent = l.name || l.mac;
      groupAddLamp.appendChild(opt);
    });
    groupAddBtn.disabled = false;
  }
}

async function refreshMembership() {
  groups = await api("api/groups") || [];
  if (activeTarget?.type === "lamp") renderLampMembership();
  else if (activeTarget?.type === "group") renderGroupEdit();
}

async function toggleMembership(group, add) {
  const path = `api/groups/${encodeURIComponent(group.name)}/${add ? "add" : "remove"}`;
  const res = await api(path, { mac: activeTarget.mac });
  log(`${add ? "added to" : "removed from"} group '${group.name}': ${res.ok ? "OK" : res.error || "FAILED"}`);
  await refreshMembership();
}

async function addLampToGroup(group, mac) {
  log(`→ add ${macLabel(mac)} → group '${group.name}'`);
  const res = await api(`api/groups/${encodeURIComponent(group.name)}/add`, { mac });
  log(`  ${res.ok ? "OK" : "FAILED"}: ${res.msg || res.error || ""}`);
  await refreshMembership();
}

async function removeLampFromGroup(group, mac) {
  log(`→ remove ${macLabel(mac)} ← group '${group.name}'`);
  const res = await api(`api/groups/${encodeURIComponent(group.name)}/remove`, { mac });
  log(`  ${res.ok ? "OK" : "FAILED"}: ${res.msg || res.error || ""}`);
  await refreshMembership();
}

groupAddBtn.addEventListener("click", () => {
  const g = groups.find((x) => x.name === activeTarget.name);
  if (!g || !groupAddLamp.value) return;
  addLampToGroup(g, groupAddLamp.value);
});

// ── generic command buttons ──────────────────────────────────────────────

document.querySelectorAll("button.cmd").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const cmd = btn.dataset.cmd;
    switch (cmd) {
      case "colortemp":
        await sendCmd("colortemp", { value: +$("ct-slider").value }); break;
      case "scene":
        await sendCmd("scene", { id: +$("scene-id").value }); break;
      case "scene-add":
        await sendCmd("scene-add", {
          id: +$("scene-id").value,
          brightness: +$("brightness-slider").value,
          ct: +$("ct-slider").value,
        }); break;
      case "scene-del":
        await sendCmd("scene-del", { id: +$("scene-id").value }); break;
      case "scene-clear":
        if (confirm("Delete ALL scenes?")) await sendCmd("scene-clear"); break;
      case "cycle":
        await sendCmd("cycle", { speed: +$("cycle-speed").value }); break;
      default:
        await sendCmd(cmd);
    }
  });
});

// ── sliders & pickers ────────────────────────────────────────────────────

$("brightness-slider").addEventListener("input", (e) => {
  $("brightness-val").textContent = e.target.value + "%";
});
$("brightness-slider").addEventListener("change", (e) => {
  sendCmd("brightness", { value: +e.target.value });
});

$("ct-slider").addEventListener("input", (e) => {
  $("ct-val").textContent = e.target.value;
});
$("ct-slider").addEventListener("change", (e) => {
  sendCmd("colortemp", { value: +e.target.value });
});

// ── power toggle (HA-style switch) ────────────────────────────────────────

const powerSwitch = $("power-switch");
const powerState = $("power-state");
powerSwitch.addEventListener("change", () => {
  sendCmd(powerSwitch.checked ? "on" : "off");
});

async function pollPowerState() {
  // Best-effort: read the bulk status and flip the switch to match reality.
  const res = await api("api/command/status", {});
  if (!res.ok || !res.results || !res.results.length) return;
  const states = res.results.map((r) => r.result && r.result.state);
  if (states.every((s) => s === "ON")) {
    powerSwitch.checked = true;
    powerState.textContent = "on";
  } else if (states.every((s) => s === "OFF")) {
    powerSwitch.checked = false;
    powerState.textContent = "off";
  } else if (states.includes("ON")) {
    powerState.textContent = "mixed";
  }
}

// scene dropdown 1..16
const sceneSel = $("scene-id");
for (let i = 1; i <= 16; i++) {
  const o = document.createElement("option");
  o.value = i; o.textContent = "Scene " + i;
  sceneSel.appendChild(o);
}

// ── query results table ──────────────────────────────────────────────────

function renderQueryResults(results) {
  queryResults.innerHTML = "";
  results.forEach((r) => {
    const div = document.createElement("div");
    div.className = "query-result";
    const val = typeof r.result === "object" ? JSON.stringify(r.result) : (r.result || r.error);
    div.textContent = `${r.lamp}: ${val}`;
    queryResults.appendChild(div);
  });
}

// ── daemon status ────────────────────────────────────────────────────────

async function pollDaemon() {
  const s = await api("api/daemon");
  const el = $("daemon-status");
  el.classList.toggle("up", !!s.running);
  el.classList.toggle("down", !s.running);
  el.title = s.running ? "daemon connected — instant commands"
                       : "daemon not running — direct connect mode (~1-2 s)";
  el.querySelector(".text").textContent =
    s.running ? "daemon" : "direct mode";
}
setInterval(pollDaemon, 5000);

// ── discover ─────────────────────────────────────────────────────────────

discoverBtn.addEventListener("click", async () => {
  discoverBtn.disabled = true;
  discoverBtn.textContent = "Scanning\u2026 (45 s)";
  log("Discovery started — this takes ~45 seconds");
  await api("api/discover", {});

  const poll = setInterval(async () => {
    const s = await api("api/discover/status");
    if (!s.running) {
      clearInterval(poll);
      discoverBtn.disabled = false;
      discoverBtn.textContent = "Discover lamps";
      if (s.result?.error) log(`Discovery error: ${s.result.error}`);
      else log(`Discovery done: ${s.result?.found ?? 0} new lamp(s)`);
      targetSel.innerHTML = '<option value="all">All</option>';
      await Promise.all([loadLamps(), loadGroups()]);
      syncSelector();
    }
  }, 3000);
});

// ── init ─────────────────────────────────────────────────────────────────

(async () => {
  await Promise.all([loadLamps(), loadGroups()]);
  setTarget(null, "All lamps", "every saved lamp");
  pollDaemon();
  pollPowerState();
  setInterval(pollPowerState, 15000);
  log("Ready. Tip: start the BLE daemon for near-instant commands.");
})();
