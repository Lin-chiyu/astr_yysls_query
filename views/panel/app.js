/* 燕云角色面板插件:全宽账号列表 + 弹窗新增(扫码登录 → 选角色 → 签发 token)。 */

const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage || null;
let timer = null;

function setState(text) {
  $("state").textContent = text;
}
function showError(text) {
  $("err").textContent = text || "";
}

/* ---------------------------------------------------------------- 账号列表 */

function renderAccounts(accounts) {
  const box = $("rows");
  box.innerHTML = "";
  const list = accounts || [];
  $("acc-count").textContent = list.length ? `共 ${list.length} 个` : "";
  $("empty").classList.toggle("hidden", list.length > 0);

  list.forEach((a) => {
    const card = document.createElement("div");
    card.className = "acc-card";

    const top = document.createElement("div");
    top.className = "acc-top";
    const nick = document.createElement("div");
    nick.className = "acc-nick";
    nick.textContent = a.nick || "(未命名)";
    nick.title = a.nick || "";
    top.appendChild(nick);

    const usable = a.enabled && a.has_token;
    const badge = document.createElement("span");
    badge.className = "badge " + (usable ? "ok" : a.enabled ? "warn" : "off");
    badge.textContent = usable ? "可用" : a.enabled ? "缺登录码" : "已停用";

    const foot = document.createElement("div");
    foot.className = "acc-foot";
    const del = document.createElement("button");
    del.className = "btn-danger";
    del.textContent = "删除";
    del.addEventListener("click", () => delAccount(a));
    foot.appendChild(badge);
    foot.appendChild(del);

    card.appendChild(top);
    card.appendChild(foot);
    box.appendChild(card);
  });
}

async function loadAccounts() {
  try {
    const r = await bridge.apiGet("account/list");
    renderAccounts(r && (r.accounts || r));
  } catch (e) {
    showError("账号列表加载失败:" + (e && e.message ? e.message : e));
  }
}

async function delAccount(a) {
  const name = a.nick || a.role_id;
  if (!window.confirm(`确定删除「${name}」吗?\n删除后这个账号就查不了了,绑在它上面的群友也会被解绑。`)) {
    return;
  }
  try {
    const r = await bridge.apiPost("account/delete", { role_id: a.role_id });
    if (!r || r.ok === false) {
      window.alert((r && r.message) || "删除失败");
      return;
    }
    renderAccounts(r.accounts || []);
  } catch (e) {
    window.alert("删除失败:" + (e && e.message ? e.message : e));
  }
}

/* ---------------------------------------------------------------- 弹窗 */

function openModal() {
  $("mask").classList.remove("hidden");
  $("result").classList.add("hidden");
  $("roles").classList.add("hidden");
  $("qr-wrap").classList.add("hidden");
  showError("");
  setState("待机");
}

function closeModal() {
  stopPolling();
  $("mask").classList.add("hidden");
  $("qr-wrap").classList.add("hidden");
  $("roles").classList.add("hidden");
  $("btn-start").disabled = false;
  setState("待机");
  showError("");
}

/* ---------------------------------------------------------------- 扫码登录 */

function showResult(result, saved) {
  if (!result) return;
  $("result").classList.remove("hidden");
  $("r-role").textContent = result.nick || result.role_id || "";
  $("r-token").textContent = result.token || "";
  $("r-dts").textContent = result.dts || "";
  $("r-saved").textContent = saved
    ? "✓ 已自动写入插件配置"
    : "⚠ 未能自动写入配置,请把上面的登录码手动填进插件配置";
}

function renderRoles(roles) {
  const box = $("roles");
  box.innerHTML = "";
  if (!roles || !roles.length) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  const tip = document.createElement("div");
  tip.className = "tip";
  tip.textContent = `检测到 ${roles.length} 个燕云角色,点击要登录的那个:`;
  box.appendChild(tip);
  roles.forEach((r) => {
    const btn = document.createElement("button");
    btn.className = "role-btn";
    const extra = [r.level ? `Lv.${r.level}` : "", r.server_name || ""]
      .filter(Boolean)
      .join(" · ");
    btn.innerHTML = `<b>${r.nick || "(未命名)"}</b><span>${extra}</span>`;
    btn.addEventListener("click", () => pick(r.role_id, r.nick));
    box.appendChild(btn);
  });
}

async function pick(roleId, nick) {
  showError("");
  try {
    setState(`正在给「${nick || "该角色"}」办登录 ...`);
    const r = await bridge.apiPost("login/issue", { role_id: roleId });
    if (!r || r.ok === false) {
      showError(r && r.message ? r.message : "签发失败");
      return;
    }
    $("roles").classList.add("hidden");
    stopPolling();
    showResult(r.result, r.saved);
    $("btn-start").disabled = false;
    await loadAccounts();
  } catch (e) {
    showError("签发失败:" + (e && e.message ? e.message : e));
  }
}

function stopPolling() {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
}

async function poll() {
  try {
    const st = await bridge.apiGet("login/status");
    setState(`[${st.state}] ${st.message || ""}`);
    if (st.qr_b64) {
      $("qr-wrap").classList.remove("hidden");
      $("qr").src = "data:image/png;base64," + st.qr_b64;
    } else {
      $("qr-wrap").classList.add("hidden");
    }
    if (st.state === "selecting") {
      renderRoles(st.roles);
    } else if (st.state === "done") {
      stopPolling();
      $("roles").classList.add("hidden");
      showResult(st.result, st.saved);
      $("btn-start").disabled = false;
      await loadAccounts();
    } else if (st.state === "failed" || st.state === "timeout") {
      stopPolling();
      $("roles").classList.add("hidden");
      showError(`登录流程失败:${st.message}`);
      $("btn-start").disabled = false;
    } else if (st.state === "issuing") {
      $("qr-wrap").classList.add("hidden");
    }
  } catch (e) {
    showError("状态查询失败:" + (e && e.message ? e.message : e));
  }
}

async function start() {
  showError("");
  $("result").classList.add("hidden");
  $("roles").classList.add("hidden");
  const roleName = $("role-id").value.trim();
  if (roleName.length > 20) {
    showError("角色名太长了,20 个字以内;留空则扫码后再选角色");
    return;
  }
  $("btn-start").disabled = true;
  try {
    const r = await bridge.apiPost("login/start", roleName ? { role_id: roleName } : {});
    if (!r || r.started === false) {
      showError(r && r.message ? r.message : "启动失败");
      $("btn-start").disabled = false;
      return;
    }
    setState("已启动,正在打开登录页...");
    stopPolling();
    timer = setInterval(poll, 1500);
    poll();
  } catch (e) {
    showError("启动失败:" + (e && e.message ? e.message : e));
    $("btn-start").disabled = false;
  }
}

async function cancel() {
  try {
    await bridge.apiPost("login/cancel", {});
  } catch {}
  stopPolling();
  $("btn-start").disabled = false;
  $("qr-wrap").classList.add("hidden");
  $("roles").classList.add("hidden");
  setState("已取消");
}

async function init() {
  $("btn-add").addEventListener("click", openModal);
  $("btn-close").addEventListener("click", closeModal);
  $("mask").addEventListener("click", (e) => {
    if (e.target === $("mask")) closeModal();
  });
  $("btn-start").addEventListener("click", start);
  $("btn-cancel").addEventListener("click", cancel);
  if (!bridge) {
    showError("未检测到 AstrBotPluginView bridge,请确认 AstrBot 版本 ≥ 4.24.1 并重新加载插件");
    $("btn-add").disabled = true;
    $("btn-start").disabled = true;
    return;
  }
  try {
    await bridge.ready();
  } catch {}
  await loadAccounts();
}

init();
