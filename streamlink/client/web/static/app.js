"use strict";

// StreamLink web UI. Talks to the local gateway with fetch() for commands and receives every
// network event over Server-Sent Events (/api/events). No framework, no build step.

const ALL = "#all";
const $ = (id) => document.getElementById(id);

const state = {
  me: null,
  status: { state: "disconnected" },
  users: [],
  convs: new Map([[ALL, []]]),
  unread: new Map(),
  active: ALL,
  transfers: new Map(),
  stats: null,
  prevStats: null,
  pending: new Map(), // cid -> pending message entry
  echoed: new Set(), // cids whose server echo arrived before the POST returned
};

// ---------------------------------------------------------------- helpers

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null) node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function formatBytes(n) {
  if (n === null || n === undefined) return "-";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function formatTime(ts) {
  const d = ts ? new Date(ts * 1000) : new Date();
  return d.toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

let toastTimer = null;
function toast(text) {
  const box = $("toast");
  box.textContent = text;
  box.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.classList.add("hidden"), 4000);
}

async function api(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function convKey(msg) {
  if (msg.kind === "all") return ALL;
  return msg.from === state.me ? msg.to : msg.from;
}

function convTitle(key) {
  return key === ALL ? "# Everyone" : `@ ${key}`;
}

// ---------------------------------------------------------------- events

function connectEvents() {
  const source = new EventSource("/api/events");
  source.onmessage = (event) => handle(JSON.parse(event.data));
  source.onerror = () => {
    setBadge("offline", "Lost the local gateway, retrying...");
  };
}

function handle(ev) {
  switch (ev.type) {
    case "snapshot": applySnapshot(ev); break;
    case "status": applyStatus(ev); break;
    case "users": state.users = ev.users; renderUsers(); break;
    case "presence": onPresence(ev); break;
    case "message": addMessage(ev, true); break;
    case "file": onTransfer(ev); break;
    case "stats": state.prevStats = state.stats; state.stats = ev; renderStats(); break;
    case "error": toast(ev.message || ev.code || "Error"); break;
  }
}

function applySnapshot(snap) {
  const defaults = snap.defaults || {};
  if (!$("login-host").value) $("login-host").value = defaults.host || "127.0.0.1";
  if (!$("login-port").value) $("login-port").value = defaults.port || 9000;
  state.convs = new Map([[ALL, []]]);
  state.unread.clear();
  state.pending.clear();
  if (snap.status && snap.status.username) state.me = snap.status.username;
  for (const msg of snap.history || []) addMessage(msg, false);
  state.transfers = new Map((snap.transfers || []).map((t) => [t.id, t]));
  state.users = snap.users || [];
  state.stats = snap.stats;
  applyStatus(snap.status);
  renderAll();
}

function applyStatus(status) {
  state.status = status;
  if (status.username) state.me = status.username;
  const loggedIn = status.state !== "disconnected";
  $("login-view").classList.toggle("hidden", loggedIn);
  $("app-view").classList.toggle("hidden", !loggedIn);
  if (!loggedIn && status.reason && status.reason !== "user quit") {
    $("login-error").textContent = status.reason;
  }
  const labels = {
    connected: "Connected",
    connecting: "Connecting",
    reconnecting: "Reconnecting",
    disconnected: "Disconnected",
  };
  let detail = status.server ? `server ${status.server}` : "";
  if (status.state === "reconnecting" && status.reason) detail += ` - ${status.reason}`;
  if (status.reconnects) detail += ` - reconnected ${status.reconnects} time(s)`;
  setBadge(status.state, detail, labels[status.state]);
  $("me-name").textContent = state.me || "";
  $("message-input").disabled = status.state !== "connected";
}

function setBadge(kind, detail, label) {
  const badge = $("conn-badge");
  badge.className = `badge ${kind}`;
  badge.textContent = label || (kind === "offline" ? "Gateway offline" : kind);
  $("conn-detail").textContent = detail || "";
}

function onPresence(ev) {
  const name = ev.user.username;
  const text = ev.event === "join"
    ? `${name} joined`
    : `${name} left${ev.reason ? ` (${ev.reason})` : ""}`;
  pushNote(ALL, text);
  if (state.convs.has(name)) pushNote(name, text);
}

function onTransfer(t) {
  const previous = state.transfers.get(t.id);
  state.transfers.set(t.id, t);
  renderTransfers();
  if (previous && previous.state === t.state) return;
  const key = t.peer;
  if (t.direction === "in" && t.state === "receiving") pushNote(key, `${t.peer} is sending ${t.name} (${formatBytes(t.size)})`);
  if (t.state === "done") {
    pushNote(key, t.direction === "in"
      ? `Received ${t.name}, SHA-256 matches`
      : `Sent ${t.name} to ${t.peer}, receiver confirmed SHA-256`);
  }
  if (t.state === "failed" || t.state === "rejected") pushNote(key, `Transfer of ${t.name} failed: ${t.error}`);
}

// ---------------------------------------------------------------- messages

function ensureConv(key) {
  if (!state.convs.has(key)) {
    state.convs.set(key, []);
    renderConversations();
  }
  return state.convs.get(key);
}

function addMessage(msg, live) {
  const key = convKey(msg);
  const list = ensureConv(key);
  if (msg.mine && msg.cid && state.pending.has(msg.cid)) {
    const entry = state.pending.get(msg.cid);
    state.pending.delete(msg.cid);
    Object.assign(entry, msg, { pending: false });
    if (entry.node) entry.node.replaceWith(renderMessage(entry));
    return;
  }
  if (msg.mine && msg.cid) state.echoed.add(msg.cid);
  const entry = { ...msg };
  list.push(entry);
  if (!live) return;
  if (key === state.active) appendToView(renderMessage(entry));
  else if (!msg.mine) {
    state.unread.set(key, (state.unread.get(key) || 0) + 1);
    renderConversations();
  }
}

function pushNote(key, text) {
  const entry = { note: text, ts: Date.now() / 1000 };
  ensureConv(key).push(entry);
  if (key === state.active) appendToView(renderMessage(entry));
}

function renderMessage(entry) {
  let node;
  if (entry.note) {
    node = el("div", { class: "note" }, `${formatTime(entry.ts)} - ${entry.note}`);
  } else {
    const cls = ["msg"];
    if (entry.mine) cls.push("mine");
    if (entry.pending) cls.push("pending");
    node = el(
      "div",
      { class: cls.join(" ") },
      el("span", { class: "meta" }, `${entry.mine ? "You" : entry.from} - ${entry.pending ? "sending..." : formatTime(entry.ts)}`),
      el("div", { class: "bubble" }, entry.text),
    );
  }
  entry.node = node;
  return node;
}

function appendToView(node) {
  const box = $("messages");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.append(node);
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function renderMessages() {
  const box = $("messages");
  box.replaceChildren(...(state.convs.get(state.active) || []).map(renderMessage));
  box.scrollTop = box.scrollHeight;
  $("chat-title").textContent = convTitle(state.active);
  if (state.active === ALL) {
    $("chat-sub").textContent = "broadcast to everyone online";
  } else {
    const online = state.users.some((u) => u.username === state.active);
    $("chat-sub").textContent = online ? "direct message - online" : "direct message - offline";
  }
}

function openConv(key) {
  ensureConv(key);
  state.active = key;
  state.unread.delete(key);
  renderConversations();
  renderMessages();
  $("message-input").focus();
}

// ---------------------------------------------------------------- sidebar and panel

function renderConversations() {
  const list = $("conversations");
  list.replaceChildren(
    ...[...state.convs.keys()].map((key) => {
      const unread = state.unread.get(key);
      return el(
        "li",
        { class: key === state.active ? "active" : "", onclick: () => openConv(key) },
        convTitle(key),
        unread ? el("span", { class: "unread" }, unread) : null,
      );
    }),
  );
}

function renderUsers() {
  $("online-count").textContent = state.users.length;
  $("users").replaceChildren(
    ...state.users.map((u) => {
      const mine = u.username === state.me;
      return el(
        "li",
        {
          class: mine ? "me" : state.active === u.username ? "active" : "",
          title: mine ? "You" : `Direct message ${u.username}`,
          onclick: mine ? null : () => openConv(u.username),
        },
        el("span", { class: "dot" }),
        el(
          "span",
          { class: "who" },
          mine ? `${u.username} (you)` : u.username,
          el("span", { class: "addr" }, u.ip ? `UDP ${u.ip}:${u.port}` : ""),
        ),
      );
    }),
  );
  if (state.active !== ALL) renderMessages();
}

function renderStats() {
  const s = state.stats;
  const box = $("net-stats");
  if (!s) {
    box.replaceChildren(el("dt", {}, "No data yet"), el("dd", {}, ""));
    return;
  }
  let up = "-";
  let down = "-";
  if (state.prevStats) {
    up = `${formatBytes(Math.max(s.bytes_sent - state.prevStats.bytes_sent, 0))}/s`;
    down = `${formatBytes(Math.max(s.bytes_received - state.prevStats.bytes_received, 0))}/s`;
  }
  const rows = [
    ["Server UDP", s.peer || "-"],
    ["RTT (SRTT)", s.srtt_ms !== null ? `${s.srtt_ms} ms` : "-"],
    ["Ping", s.ping_ms !== null ? `${s.ping_ms} ms` : "-"],
    ["RTO", `${s.rto_ms} ms`],
    ["Congestion", s.congestion],
    ["cwnd / inflight", `${s.cwnd} / ${s.inflight}`],
    ["Queued", s.queued],
    ["Packets sent/recv", `${s.packets_sent} / ${s.packets_received}`],
    ["Retransmits", `${s.retransmits} (${(s.retransmit_rate * 100).toFixed(2)}%)`],
    ["Fast / RTO", `${s.fast_retransmits} / ${s.timeouts}`],
    ["Dup / reorder", `${s.duplicates} / ${s.out_of_order}`],
    ["Decrypt errors", s.decrypt_failures],
    ["Upload", up],
    ["Download", down],
    ["Bytes sent/recv", `${formatBytes(s.bytes_sent)} / ${formatBytes(s.bytes_received)}`],
  ];
  box.replaceChildren(...rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
}

const STATE_LABELS = {
  hashing: "hashing SHA-256",
  waiting: "waiting for peer",
  offered: "offered",
  sending: "sending",
  receiving: "receiving",
  verifying: "verifying",
  done: "done",
  failed: "failed",
  rejected: "rejected",
};

function renderTransfers() {
  const items = [...state.transfers.values()].reverse();
  $("no-transfers").classList.toggle("hidden", items.length > 0);
  $("transfers").replaceChildren(
    ...items.map((t) => {
      const active = !["done", "failed", "rejected"].includes(t.state);
      const direction = t.direction === "out" ? `To ${t.peer}` : `From ${t.peer}`;
      const actions = [];
      if (t.download) actions.push(el("a", { href: t.download, class: "link" }, "Download"));
      if (active) {
        actions.push(el("button", {
          class: "link",
          onclick: () => api("/api/file/cancel", { id: t.id }).catch((e) => toast(e.message)),
        }, "Cancel"));
      }
      return el(
        "li",
        { class: `transfer ${t.state}` },
        el("div", { class: "top" }, el("span", { class: "name", title: t.name }, t.name), el("span", {}, ...actions)),
        el("div", { class: "bar" }, el("span", { style: `width:${t.percent}%` })),
        el(
          "div",
          { class: "sub" },
          el("span", {}, `${direction} - `, el("span", { class: `state-${t.state}` }, STATE_LABELS[t.state] || t.state)),
          el("span", {}, `${formatBytes(t.bytes)} / ${formatBytes(t.size)} - ${formatBytes(t.speed)}/s`),
        ),
        t.error ? el("div", { class: "sub state-failed" }, t.error) : null,
      );
    }),
  );
}

function renderAll() {
  renderConversations();
  renderUsers();
  renderMessages();
  renderStats();
  renderTransfers();
}

// ---------------------------------------------------------------- actions

async function sendFile(file) {
  if (state.active === ALL) {
    toast("Open a direct conversation before sending a file.");
    return;
  }
  const to = state.active;
  try {
    const url = `/api/file?to=${encodeURIComponent(to)}&name=${encodeURIComponent(file.name)}`;
    const response = await fetch(url, { method: "POST", body: file });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || response.statusText);
  } catch (err) {
    toast(`Could not send file: ${err.message}`);
  }
}

function setupHandlers() {
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("login-error").textContent = "";
    $("login-btn").disabled = true;
    try {
      state.me = $("login-user").value.trim();
      await api("/api/login", {
        host: $("login-host").value.trim(),
        port: Number($("login-port").value),
        username: state.me,
        password: $("login-pass").value,
      });
      $("login-pass").value = "";
    } catch (err) {
      $("login-error").textContent = err.message;
    } finally {
      $("login-btn").disabled = false;
    }
  });

  $("logout-btn").addEventListener("click", () => api("/api/logout").catch((e) => toast(e.message)));

  $("composer").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("message-input");
    const text = input.value;
    if (!text.trim()) return;
    input.value = "";
    const to = state.active === ALL ? null : state.active;
    try {
      const { cid } = await api("/api/msg", { to, text });
      const entry = { kind: to ? "dm" : "all", from: state.me, to, text, mine: true, pending: true, cid };
      if (!cid || state.echoed.has(cid)) return;
      state.pending.set(cid, entry);
      ensureConv(state.active).push(entry);
      if (state.active === (to || ALL)) appendToView(renderMessage(entry));
    } catch (err) {
      toast(err.message);
      input.value = text;
    }
  });

  $("file-input").addEventListener("change", (event) => {
    for (const file of event.target.files) sendFile(file);
    event.target.value = "";
  });

  const chat = $("chat");
  let dragDepth = 0;
  chat.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; $("drop-hint").classList.remove("hidden"); });
  chat.addEventListener("dragover", (e) => e.preventDefault());
  chat.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("drop-hint").classList.add("hidden"); } });
  chat.addEventListener("drop", (e) => {
    e.preventDefault();
    dragDepth = 0;
    $("drop-hint").classList.add("hidden");
    for (const file of e.dataTransfer.files) sendFile(file);
  });
}

setupHandlers();
connectEvents();
