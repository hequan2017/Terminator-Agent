/* Terminator 前端 —— 原生 JS，无构建步骤。 */
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = window.escapeHtml;

  const state = {
    user: null,
    convId: null,
    ws: null,
    busy: false,
    hosts: [],
    apps: [],
    current: null,   // 正在流式输出的 assistant 消息元素
    tools: {},       // call_id -> tool DOM
  };

  // ---------------- 工具函数 ----------------
  async function api(path, opts = {}) {
    const res = await fetch(path, {
      method: opts.method || "GET",
      headers: opts.body ? { "Content-Type": "application/json" } : {},
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      credentials: "same-origin",
    });
    if (res.status === 401 && path !== "/api/login") { showLogin(); throw new Error("未登录"); }
    const data = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
    if (!res.ok) {
      let msg = data?.detail ?? data;
      if (Array.isArray(msg)) msg = msg.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("；");
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return data;
  }

  let toastTimer;
  function toast(msg, isErr = false) {
    const t = $("#toast");
    t.textContent = msg;
    t.className = "toast" + (isErr ? " err" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add("hidden"), 3200);
  }

  const fmtTime = (ts) => {
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  };
  const badge = (v, text) => `<span class="badge ${esc(v || "")}">${esc(text ?? v ?? "")}</span>`;
  const STATUS_TEXT = { success: "成功", failed: "失败", running: "运行中", pending: "等待", cancelled: "已取消" };
  const RISK_TEXT = { safe: "只读", approve: "需审批", forbidden: "禁止" };
  const DECISION_TEXT = { auto: "自动", approved: "已批准", rejected: "已拒绝", blocked: "已拦截" };

  function openModal(title, html) {
    $("#modal-title").textContent = title;
    $("#modal-body").innerHTML = html;
    $("#modal").classList.remove("hidden");
    return $("#modal-body");
  }
  function closeModal() {
    $("#modal").classList.add("hidden");
    $("#modal-body").innerHTML = "";
    if (state.depWs) { state.depWs.close(); state.depWs = null; }
  }
  $("#modal-close").onclick = closeModal;
  $("#modal").addEventListener("mousedown", (e) => { if (e.target.id === "modal") closeModal(); });

  // ---------------- 登录 ----------------
  function showLogin() {
    $("#app-view").classList.add("hidden");
    $("#login-view").classList.remove("hidden");
    if (state.ws) { state.ws.onclose = null; state.ws.close(); state.ws = null; }
  }

  $("#login-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    $("#login-error").textContent = "";
    try {
      await api("/api/login", { method: "POST", body: { username: f.get("username"), password: f.get("password") } });
      await boot();
    } catch (err) {
      $("#login-error").textContent = err.message;
    }
  };

  $("#logout").onclick = async () => { await api("/api/logout", { method: "POST" }); showLogin(); };

  async function boot() {
    let me;
    try { me = await api("/api/me"); } catch { showLogin(); return; }
    state.user = me.username;
    $("#me").textContent = "👤 " + me.username;
    $("#llm-status").innerHTML = me.llm_configured
      ? `模型：${esc(me.llm_model)}`
      : `<span style="color:var(--warn)">⚠ 未配置 LLM_API_KEY</span>`;
    $("#login-view").classList.add("hidden");
    $("#app-view").classList.remove("hidden");
    connectWs();
    loadConversations();
    loadHosts();
  }

  // ---------------- 标签页 ----------------
  $$(".nav a").forEach((a) => (a.onclick = () => switchTab(a.dataset.tab)));
  function switchTab(tab) {
    $$(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
    $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + tab));
    $("#conv-section").style.visibility = tab === "chat" ? "visible" : "hidden";
    if (tab === "hosts") loadHosts();
    if (tab === "apps") loadApps();
    if (tab === "audit") loadAudit();
  }

  // ---------------- 对话列表 ----------------
  async function loadConversations() {
    const list = await api("/api/conversations");
    const ul = $("#conv-list");
    ul.innerHTML = list.map((c) => `<li data-id="${c.id}" class="${c.id === state.convId ? "active" : ""}">
        <span class="t" title="${esc(c.title)}">${esc(c.title)}</span><span class="del" title="删除">✕</span></li>`).join("")
      || `<li class="muted small" style="cursor:default">暂无对话</li>`;
    $$("li[data-id]", ul).forEach((li) => {
      li.onclick = (e) => {
        const id = +li.dataset.id;
        if (e.target.classList.contains("del")) return deleteConversation(id);
        openConversation(id);
      };
    });
  }

  async function deleteConversation(id) {
    if (!confirm("删除这个对话？")) return;
    await api(`/api/conversations/${id}`, { method: "DELETE" });
    if (state.convId === id) newConversation();
    loadConversations();
  }

  function newConversation() {
    if (state.busy) return toast("当前对话还在执行中", true);
    state.convId = null;
    $("#messages").innerHTML = "";
    $("#messages").appendChild(welcomeEl);
    $$("#conv-list li").forEach((li) => li.classList.remove("active"));
    $("#input").focus();
  }
  $("#new-conv").onclick = newConversation;

  async function openConversation(id) {
    if (state.busy) return toast("当前对话还在执行中", true);
    state.convId = id;
    $$("#conv-list li").forEach((li) => li.classList.toggle("active", +li.dataset.id === id));
    const msgs = await api(`/api/conversations/${id}/messages`);
    renderHistory(msgs);
  }

  // ---------------- 消息渲染 ----------------
  const welcomeEl = $("#welcome");
  $$(".example").forEach((b) => (b.onclick = () => { $("#input").value = b.textContent; send(); }));

  function hideWelcome() { if (welcomeEl.parentNode) welcomeEl.remove(); }

  function scrollBottom(force = false) {
    const box = $("#messages");
    if (force || box.scrollHeight - box.scrollTop - box.clientHeight < 160) box.scrollTop = box.scrollHeight;
  }

  function addUser(text) {
    hideWelcome();
    const div = document.createElement("div");
    div.className = "msg user";
    div.innerHTML = `<div class="bubble">${esc(text)}</div>`;
    $("#messages").appendChild(div);
    scrollBottom(true);
  }

  function addAssistant() {
    hideWelcome();
    const div = document.createElement("div");
    div.className = "msg assistant";
    div.innerHTML = `<div class="content md"></div>`;
    div._text = "";
    $("#messages").appendChild(div);
    return div;
  }

  function lastAssistantBlock() {
    const last = $("#messages").lastElementChild;
    if (last && last.classList.contains("assistant")) return last;
    return addAssistant();
  }

  const TOOL_LABELS = {
    run_command: "执行命令", list_hosts: "查看主机列表", list_apps: "查看应用列表",
    deploy: "发版", rollback: "回滚", deployment_history: "查询发版记录",
  };

  function createTool(ev, container) {
    const el = document.createElement("div");
    el.className = "tool";
    const label = TOOL_LABELS[ev.tool] || ev.tool;
    const hosts = (ev.hosts || []).join(", ");
    const purpose = ev.purpose || "";
    const cmdLine = ev.command ? `<div class="tool-cmd">${esc(ev.command)}</div>` :
      (ev.tool === "deploy" || ev.tool === "rollback")
        ? `<div class="tool-cmd">${esc(`${ev.tool} ${ev.app || ev.args?.app || ""} ${ev.version || ev.args?.version || ""}`)}</div>` : "";
    el.innerHTML = `
      <div class="tool-head">
        <span class="chev">▶</span>
        <span class="state"><span class="spinner"></span></span>
        <span class="title"><b>${esc(label)}</b>
          ${hosts ? `<span class="hosts">@${esc(hosts)}</span>` : ""}
          ${purpose ? `<span class="purpose"> · ${esc(purpose)}</span>` : ""}</span>
        ${ev.risk ? badge(ev.risk, RISK_TEXT[ev.risk]) : ""}
      </div>
      ${cmdLine}
      <div class="tool-approval"></div>
      <div class="tool-body"><pre class="tool-out"></pre></div>`;
    $(".tool-head", el).onclick = () => el.classList.toggle("open");
    el._outs = {};
    container.appendChild(el);
    return el;
  }

  function appendToolOutput(el, host, text) {
    const pre = $(".tool-out", el);
    const multi = (el._hostsCount || 0) > 1;
    if (multi) {
      if (!el._outs[host]) {
        const sec = document.createElement("div");
        sec.innerHTML = `<div class="host-label">── ${esc(host)} ──</div><span></span>`;
        pre.appendChild(sec);
        el._outs[host] = sec.lastElementChild;
      }
      el._outs[host].textContent += text;
    } else {
      pre.appendChild(document.createTextNode(text));
    }
    if (pre.textContent.length > 200000) pre.textContent = "...[已截断早期输出]\n" + pre.textContent.slice(-150000);
    pre.scrollTop = pre.scrollHeight;
  }

  function finishTool(el, ev) {
    const st = $(".state", el);
    const ok = ev.status === "ok";
    const icon = ok ? `<span class="status-icon ok">✓</span>`
      : ev.status === "rejected" || ev.status === "blocked" ? `<span class="status-icon warn">⊘</span>`
      : `<span class="status-icon fail">✗</span>`;
    st.innerHTML = icon;
    const pre = $(".tool-out", el);
    if (ev.results && !pre.textContent) {
      // 历史记录回放：没有流式输出时用结果填充
      ev.results.forEach((r) => {
        if (ev.results.length > 1) {
          const lbl = document.createElement("div");
          lbl.className = "host-label";
          lbl.textContent = `── ${r.host} (exit ${r.exit_code ?? "-"}) ──`;
          pre.appendChild(lbl);
        }
        pre.appendChild(document.createTextNode((r.output || "(无输出)") + "\n"));
      });
    }
    if (ev.results) {
      const codes = ev.results.map((r) => `${ev.results.length > 1 ? r.host + ": " : ""}exit ${r.exit_code ?? r.status}`).join(" · ");
      const t = $(".title", el);
      t.insertAdjacentHTML("beforeend", ` <span class="muted small">(${esc(codes)})</span>`);
    }
    if (ev.summary && !ev.results) {
      if (!pre.textContent) pre.textContent = ev.summary;
      $(".title", el).insertAdjacentHTML("beforeend", ` <span class="muted small">${esc(ev.summary)}</span>`);
    }
    if (ev.deployment_id) {
      $(".title", el).insertAdjacentHTML("beforeend",
        ` <a class="small" data-dep="${ev.deployment_id}">查看发版 #${ev.deployment_id}</a>`);
      $(`[data-dep]`, el).onclick = (e) => { e.stopPropagation(); showDeployment(ev.deployment_id); };
    }
    if (!ok && pre.textContent) el.classList.add("open");
  }

  function showApproval(el, ev) {
    const box = $(".tool-approval", el);
    const what = ev.kind === "deploy" ? `将发布 <b>${esc(ev.app)}</b> 版本 <b>${esc(ev.version)}</b> 到 ${esc(ev.hosts.join(", "))}`
      : ev.kind === "rollback" ? `将回滚 <b>${esc(ev.app)}</b> 到版本 <b>${esc(ev.version)}</b>（${esc(ev.hosts.join(", "))}）`
      : `将在 <b>${esc((ev.hosts || []).join(", "))}</b> 上执行上面的命令`;
    box.className = "tool-approval approval";
    box.innerHTML = `
      <div class="why">⚠ 需要你的确认：${what}<br><span class="small">原因：${esc(ev.reason || "")}</span></div>
      <div class="acts">
        <button class="btn ok" data-act="yes">批准执行</button>
        <button class="btn danger" data-act="no">拒绝</button>
        <input placeholder="可选：给 AI 的说明（例如拒绝原因、改用什么方式）">
      </div>`;
    const respond = (approved) => {
      const note = $("input", box).value;
      state.ws.send(JSON.stringify({ type: "approval", call_id: ev.call_id, approved, note }));
      $$("button", box).forEach((b) => (b.disabled = true));
    };
    $('[data-act="yes"]', box).onclick = () => respond(true);
    $('[data-act="no"]', box).onclick = () => respond(false);
    $("input", box).onkeydown = (e) => { if (e.key === "Enter") respond(false); };
    scrollBottom(true);
  }

  function resolveApproval(el, approved, note) {
    const box = $(".tool-approval", el);
    box.className = "tool-approval approval done";
    box.innerHTML = approved ? `✓ 已批准${note ? "：" + esc(note) : ""}` : `✗ 已拒绝${note ? "：" + esc(note) : ""}`;
  }

  function renderHistory(msgs) {
    const box = $("#messages");
    box.innerHTML = "";
    state.tools = {};
    let cur = null;
    const toolEls = {};
    for (const m of msgs) {
      if (m.role === "user") { addUser(m.content); cur = null; continue; }
      if (m.role === "assistant") {
        if (!cur) cur = addAssistant();
        if (m.content) {
          const c = document.createElement("div");
          c.className = "content md";
          c.innerHTML = window.renderMarkdown(m.content);
          cur.appendChild(c);
        }
        for (const tc of m.tool_calls || []) {
          let args = {};
          try { args = JSON.parse(tc.function.arguments || "{}"); } catch {}
          toolEls[tc.id] = { el: null, name: tc.function.name, args };
        }
        continue;
      }
      if (m.role === "tool") {
        if (!cur) cur = addAssistant();
        const meta = m._meta || {};
        const info = toolEls[m.tool_call_id] || { name: meta.tool, args: {} };
        const ev = { tool: info.name, args: info.args, ...meta };
        const el = createTool(ev, cur);
        el._hostsCount = (ev.results || []).length;
        if (meta.status === "rejected" || meta.status === "approved" || meta.note !== undefined) {
          if (meta.risk === "approve" || meta.status === "rejected") {
            resolveApproval(el, meta.status !== "rejected", meta.note || "");
          }
        }
        if (!meta.status) {
          const pre = $(".tool-out", el);
          pre.textContent = m.content;
          ev.status = "ok";
        }
        finishTool(el, ev);
      }
    }
    if (!msgs.length) box.appendChild(welcomeEl);
    scrollBottom(true);
  }

  // ---------------- WebSocket ----------------
  function connectWs() {
    if (state.ws && state.ws.readyState <= 1) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/chat`);
    state.ws = ws;
    ws.onmessage = (e) => handleEvent(JSON.parse(e.data));
    ws.onclose = (e) => {
      setBusy(false);
      if (e.code === 4401) return showLogin();
      setTimeout(connectWs, 2000);
    };
  }

  function handleEvent(ev) {
    switch (ev.type) {
      case "conversation":
        state.convId = ev.conversation_id;
        loadConversations();
        break;
      case "title":
        loadConversations();
        break;
      case "assistant_start": {
        const block = lastAssistantBlock();
        const c = document.createElement("div");
        c.className = "content md cursor";
        c._text = "";
        block.appendChild(c);
        state.current = c;
        break;
      }
      case "assistant_delta":
        if (state.current) {
          state.current._text += ev.text;
          state.current.innerHTML = window.renderMarkdown(state.current._text);
          scrollBottom();
        }
        break;
      case "assistant_end":
        if (state.current) {
          state.current.classList.remove("cursor");
          if (!state.current._text) state.current.remove();
          state.current = null;
        }
        break;
      case "tool_start": {
        const el = createTool(ev, lastAssistantBlock());
        el._hostsCount = (ev.hosts || []).length;
        state.tools[ev.call_id] = el;
        if (ev.tool === "run_command" || ev.tool === "deploy" || ev.tool === "rollback") el.classList.add("open");
        scrollBottom();
        break;
      }
      case "approval_request": {
        const el = state.tools[ev.call_id];
        if (el) showApproval(el, ev);
        break;
      }
      case "approval_result": {
        const el = state.tools[ev.call_id];
        if (el) resolveApproval(el, ev.approved, ev.note);
        break;
      }
      case "tool_output": {
        const el = state.tools[ev.call_id];
        if (el) { appendToolOutput(el, ev.host, ev.text); scrollBottom(); }
        break;
      }
      case "tool_result": {
        let el = state.tools[ev.call_id];
        if (!el) { el = createTool(ev, lastAssistantBlock()); state.tools[ev.call_id] = el; }
        finishTool(el, ev);
        if (ev.status === "ok" && ev.tool !== "deploy" && ev.tool !== "rollback") el.classList.remove("open");
        break;
      }
      case "error": {
        const div = document.createElement("div");
        div.className = "error-box";
        div.textContent = ev.message;
        lastAssistantBlock().appendChild(div);
        scrollBottom(true);
        break;
      }
      case "stopped":
        if (state.current) { state.current.classList.remove("cursor"); state.current = null; }
        $$(".spinner", $("#messages")).forEach((s) => (s.outerHTML = `<span class="status-icon warn">⊘</span>`));
        $$(".approval:not(.done)", $("#messages")).forEach((b) => { b.className = "tool-approval approval done"; b.textContent = "已停止"; });
        break;
      case "turn_end":
        setBusy(false);
        loadConversations();
        break;
    }
  }

  function setBusy(b) {
    state.busy = b;
    $("#send").classList.toggle("hidden", b);
    $("#stop").classList.toggle("hidden", !b);
  }

  function send() {
    const input = $("#input");
    const text = input.value.trim();
    if (!text || state.busy) return;
    if (!state.ws || state.ws.readyState !== 1) return toast("连接未就绪，正在重连…", true);
    addUser(text);
    input.value = "";
    autoGrow();
    setBusy(true);
    state.ws.send(JSON.stringify({ type: "chat", conversation_id: state.convId, text }));
  }

  const input = $("#input");
  function autoGrow() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 220) + "px"; }
  input.addEventListener("input", autoGrow);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  $("#send").onclick = send;
  $("#stop").onclick = () => state.ws?.send(JSON.stringify({ type: "stop" }));

  // ---------------- 服务器 ----------------
  async function loadHosts() {
    state.hosts = await api("/api/hosts");
    $("#host-rows").innerHTML = state.hosts.map((h) => `<tr>
        <td><b>${esc(h.name)}</b></td>
        <td><code>${esc(h.username)}@${esc(h.hostname)}:${h.port}</code></td>
        <td>${h.has_key ? "🔑 私钥" : ""}${h.has_key && h.has_password ? " + " : ""}${h.has_password ? "密码" : ""}</td>
        <td>${h.tags.split(",").filter(Boolean).map((t) => `<span class="tag">${esc(t.trim())}</span>`).join("")}</td>
        <td class="muted">${esc(h.description)}</td>
        <td class="actions">
          <button class="btn tiny" data-test="${h.id}">测试连接</button>
          <button class="btn tiny" data-edit="${h.id}">编辑</button>
          <button class="btn tiny ghost" data-del="${h.id}">删除</button>
        </td></tr>`).join("") || `<tr><td colspan="6" class="empty">还没有服务器，点击右上角添加</td></tr>`;
    $$("[data-test]").forEach((b) => (b.onclick = () => testHost(+b.dataset.test, b)));
    $$("[data-edit]").forEach((b) => (b.onclick = () => hostForm(state.hosts.find((h) => h.id === +b.dataset.edit))));
    $$("[data-del]").forEach((b) => (b.onclick = async () => {
      const h = state.hosts.find((x) => x.id === +b.dataset.del);
      if (!confirm(`删除服务器 ${h.name}？`)) return;
      await api(`/api/hosts/${h.id}`, { method: "DELETE" });
      toast("已删除");
      loadHosts();
    }));
  }

  async function testHost(id, btn) {
    btn.disabled = true;
    btn.textContent = "连接中…";
    try {
      const r = await api(`/api/hosts/${id}/test`, { method: "POST" });
      openModal(r.ok ? "✓ 连接成功" : "✗ 连接失败", `<pre class="log-view" style="height:auto;max-height:50vh">${esc(r.output)}</pre>
        <div class="muted small" style="margin-top:6px">耗时 ${r.duration}s</div>`);
    } catch (e) { toast(e.message, true); }
    btn.disabled = false;
    btn.textContent = "测试连接";
  }

  function hostForm(h) {
    const edit = !!h;
    h = h || { name: "", hostname: "", port: 22, username: "root", tags: "", description: "" };
    const body = openModal(edit ? `编辑服务器 · ${h.name}` : "添加服务器", `
      <form id="host-form">
        <div class="grid2">
          <label>名称（AI 通过它识别机器）<input name="name" required value="${esc(h.name)}" placeholder="web-01"></label>
          <label>地址（IP 或域名）<input name="hostname" required value="${esc(h.hostname)}" placeholder="10.0.0.11"></label>
          <label>SSH 端口<input name="port" type="number" value="${h.port}"></label>
          <label>登录用户<input name="username" required value="${esc(h.username)}"></label>
        </div>
        <label>SSH 私钥${edit && h.has_key ? "（已配置，留空则不修改）" : ""}
          <textarea name="private_key" class="code" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----&#10;...&#10;-----END OPENSSH PRIVATE KEY-----"></textarea>
          <div class="hint">支持 OpenSSH / PEM 格式（RSA、Ed25519、ECDSA）。私钥会加密后保存在服务端，不会回显。</div>
        </label>
        <div class="grid2">
          <label>私钥密码（passphrase，可选）<input name="passphrase" type="password" autocomplete="new-password"></label>
          <label>登录密码（可选，没有私钥时使用）<input name="password" type="password" autocomplete="new-password"
            placeholder="${edit && h.has_password ? "已配置，留空不修改" : ""}"></label>
        </div>
        <div class="grid2">
          <label>标签（逗号分隔）<input name="tags" value="${esc(h.tags)}" placeholder="prod,web,nginx"></label>
          <label>描述（帮助 AI 理解这台机器）<input name="description" value="${esc(h.description)}" placeholder="生产环境前端 nginx"></label>
        </div>
        <div class="error-text" id="host-err"></div>
        <div class="modal-foot">
          <button type="button" class="btn" id="cancel">取消</button>
          <button class="btn primary">${edit ? "保存" : "添加"}</button>
        </div>
      </form>`);
    $("#cancel", body).onclick = closeModal;
    $("#host-form", body).onsubmit = async (e) => {
      e.preventDefault();
      const f = Object.fromEntries(new FormData(e.target));
      f.port = +f.port || 22;
      for (const k of ["private_key", "passphrase", "password"]) if (!f[k]) delete f[k];
      try {
        await api(edit ? `/api/hosts/${h.id}` : "/api/hosts", { method: edit ? "PUT" : "POST", body: f });
        closeModal();
        toast(edit ? "已保存" : "已添加，可以点「测试连接」验证");
        loadHosts();
      } catch (err) { $("#host-err").textContent = err.message; }
    };
  }
  $("#add-host").onclick = () => hostForm(null);

  // ---------------- 应用 ----------------
  const DEPLOY_TEMPLATE = `# 可用变量：$VERSION 版本号  $APP 应用名  $HOST 主机名  $PREV_VERSION 上一成功版本
# 脚本以 set -euo pipefail 运行，任意一步失败即中止本机发版
cd /opt/$APP
git fetch --all --tags
git checkout "$VERSION"
# 例如：docker compose up -d --build   或   systemctl restart $APP
`;
  const ROLLBACK_TEMPLATE = `cd /opt/$APP
git checkout "$VERSION"
# systemctl restart $APP
`;

  async function loadApps() {
    [state.apps, state.hosts] = await Promise.all([api("/api/apps"), api("/api/hosts")]);
    const id2name = Object.fromEntries(state.hosts.map((h) => [h.id, h.name]));
    $("#app-cards").innerHTML = state.apps.map((a) => `<div class="card">
        <h4>${esc(a.name)}</h4>
        <div class="meta">${esc(a.description || "")}</div>
        <div class="meta">目标主机：${a.host_ids.map((i) => `<span class="tag">${esc(id2name[i] || "#" + i)}</span>`).join("") || "未配置"}</div>
        <div class="btns">
          <button class="btn primary tiny" data-deploy="${a.id}">🚀 发版</button>
          <button class="btn tiny" data-rollback="${a.id}" ${a.rollback_script.trim() ? "" : "disabled title='未配置回滚脚本'"}>↩ 回滚</button>
          <button class="btn tiny" data-edit-app="${a.id}">编辑</button>
          <button class="btn tiny ghost" data-del-app="${a.id}">删除</button>
        </div></div>`).join("") || `<div class="muted">还没有应用。添加应用并配置发版脚本后，就可以在对话里说「把 xx 的 v1.2 发到生产」。</div>`;
    $$("[data-deploy]").forEach((b) => (b.onclick = () => deployForm(state.apps.find((a) => a.id === +b.dataset.deploy), "deploy")));
    $$("[data-rollback]").forEach((b) => (b.onclick = () => deployForm(state.apps.find((a) => a.id === +b.dataset.rollback), "rollback")));
    $$("[data-edit-app]").forEach((b) => (b.onclick = () => appForm(state.apps.find((a) => a.id === +b.dataset.editApp))));
    $$("[data-del-app]").forEach((b) => (b.onclick = async () => {
      const a = state.apps.find((x) => x.id === +b.dataset.delApp);
      if (!confirm(`删除应用 ${a.name}？`)) return;
      await api(`/api/apps/${a.id}`, { method: "DELETE" });
      loadApps();
    }));
    loadDeployments();
  }

  async function loadDeployments() {
    const deps = await api("/api/deployments");
    $("#dep-rows").innerHTML = deps.map((d) => `<tr>
        <td>${d.id}</td><td>${esc(d.app_name || "-")}</td>
        <td>${d.action === "rollback" ? "回滚" : "发版"}</td><td><code>${esc(d.version)}</code></td>
        <td>${badge(d.status, STATUS_TEXT[d.status])}</td><td>${esc(d.triggered_by)}</td>
        <td class="muted">${fmtTime(d.created_at)}</td>
        <td class="actions"><button class="btn tiny" data-log="${d.id}">日志</button></td></tr>`).join("")
      || `<tr><td colspan="8" class="empty">暂无发版记录</td></tr>`;
    $$("[data-log]").forEach((b) => (b.onclick = () => showDeployment(+b.dataset.log)));
  }

  function appForm(a) {
    const edit = !!a;
    a = a || { name: "", description: "", host_ids: [], deploy_script: DEPLOY_TEMPLATE, rollback_script: ROLLBACK_TEMPLATE, health_check: "" };
    const body = openModal(edit ? `编辑应用 · ${a.name}` : "添加应用", `
      <form id="app-form">
        <div class="grid2">
          <label>应用名<input name="name" required value="${esc(a.name)}" placeholder="order-api"></label>
          <label>描述<input name="description" value="${esc(a.description)}" placeholder="订单服务，Java，端口 8080"></label>
        </div>
        <label>目标主机（按勾选顺序逐台发布）
          <div class="checks">${state.hosts.map((h) => `<label><input type="checkbox" name="host" value="${h.id}"
            ${a.host_ids.includes(h.id) ? "checked" : ""}>${esc(h.name)}</label>`).join("") || '<span class="muted">请先添加服务器</span>'}</div>
        </label>
        <label>发版脚本<textarea name="deploy_script" class="code" required>${esc(a.deploy_script)}</textarea></label>
        <label>回滚脚本（可选，$VERSION 为回滚目标版本）<textarea name="rollback_script" class="code" style="min-height:90px">${esc(a.rollback_script)}</textarea></label>
        <label>健康检查命令（可选，退出码 0 视为健康，失败会重试 5 次）
          <input name="health_check" class="code" value="${esc(a.health_check)}" placeholder="curl -fsS http://127.0.0.1:8080/health"></label>
        <div class="error-text" id="app-err"></div>
        <div class="modal-foot">
          <button type="button" class="btn" id="cancel">取消</button>
          <button class="btn primary">${edit ? "保存" : "添加"}</button>
        </div>
      </form>`);
    $("#cancel", body).onclick = closeModal;
    $("#app-form", body).onsubmit = async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const data = {
        name: fd.get("name"), description: fd.get("description"),
        host_ids: fd.getAll("host").map(Number),
        deploy_script: fd.get("deploy_script"), rollback_script: fd.get("rollback_script"), health_check: fd.get("health_check"),
      };
      try {
        await api(edit ? `/api/apps/${a.id}` : "/api/apps", { method: edit ? "PUT" : "POST", body: data });
        closeModal();
        toast("已保存");
        loadApps();
      } catch (err) { $("#app-err").textContent = err.message; }
    };
  }
  $("#add-app").onclick = () => appForm(null);

  function deployForm(a, action) {
    const id2name = Object.fromEntries(state.hosts.map((h) => [h.id, h.name]));
    const label = action === "rollback" ? "回滚" : "发版";
    const body = openModal(`${label} · ${a.name}`, `
      <form id="dep-form">
        <label>${action === "rollback" ? "回滚到版本（留空 = 上一个成功版本）" : "版本号 / tag / commit"}
          <input name="version" ${action === "deploy" ? "required" : ""} placeholder="v1.2.3" autofocus></label>
        <label>目标主机
          <div class="checks">${a.host_ids.map((i) => `<label><input type="checkbox" name="host" value="${esc(id2name[i] || "")}" checked>${esc(id2name[i] || "#" + i)}</label>`).join("")}</div>
        </label>
        <div class="error-text" id="dep-err"></div>
        <div class="modal-foot">
          <button type="button" class="btn" id="cancel">取消</button>
          <button class="btn ${action === "rollback" ? "danger" : "primary"}">确认${label}</button>
        </div>
      </form>`);
    $("#cancel", body).onclick = closeModal;
    $("#dep-form", body).onsubmit = async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const hosts = fd.getAll("host");
      if (!hosts.length) return ($("#dep-err").textContent = "请至少选择一台主机");
      try {
        const dep = await api(`/api/apps/${a.id}/${action}`, { method: "POST", body: { version: fd.get("version"), hosts } });
        showDeployment(dep.id);
        loadDeployments();
      } catch (err) { $("#dep-err").textContent = err.message; }
    };
  }

  function showDeployment(id) {
    const body = openModal(`发版 #${id}`, `
      <div class="row-between" style="margin-bottom:8px">
        <div id="dep-info" class="muted"></div>
        <button class="btn danger tiny hidden" id="dep-cancel">中止发版</button>
      </div>
      <pre class="log-view" id="dep-log"></pre>`);
    const logEl = $("#dep-log", body);
    const setStatus = (d) => {
      $("#dep-info", body).innerHTML = `${esc(d.app_name || "")} · ${d.action === "rollback" ? "回滚" : "发版"} <code>${esc(d.version)}</code> · ${badge(d.status, STATUS_TEXT[d.status])}`;
      $("#dep-cancel", body).classList.toggle("hidden", d.status !== "running" && d.status !== "pending");
    };
    $("#dep-cancel", body).onclick = async () => {
      if (!confirm("确定中止？正在执行的脚本会被终止，已完成的主机不会自动回滚。")) return;
      try { await api(`/api/deployments/${id}/cancel`, { method: "POST" }); } catch (e) { toast(e.message, true); }
    };
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/deployments/${id}`);
    state.depWs = ws;
    let dep = null;
    ws.onmessage = (e) => {
      const ev = JSON.parse(e.data);
      if (ev.type === "snapshot") { dep = ev.deployment; logEl.textContent = dep.log; setStatus(dep); }
      else if (ev.type === "log") logEl.textContent += ev.text;
      else if (ev.type === "status" && dep) {
        dep.status = ev.status; setStatus(dep);
        if (ev.done && $("#tab-apps").classList.contains("active")) loadDeployments();
      }
      logEl.scrollTop = logEl.scrollHeight;
    };
  }

  // ---------------- 审计 ----------------
  async function loadAudit() {
    const rows = await api("/api/audit");
    $("#audit-rows").innerHTML = rows.map((r) => `<tr title="${esc((r.output || "").slice(0, 1500))}">
        <td class="muted">${fmtTime(r.ts)}</td><td>${esc(r.source)}</td><td>${esc(r.host)}</td>
        <td class="cmd">${esc(r.command)}</td><td>${badge(r.risk, RISK_TEXT[r.risk])}</td>
        <td>${badge(r.decision, DECISION_TEXT[r.decision])}</td><td>${r.exit_code ?? "-"}</td></tr>`).join("")
      || `<tr><td colspan="7" class="empty">暂无记录</td></tr>`;
  }
  $("#refresh-audit").onclick = loadAudit;

  boot();
})();
