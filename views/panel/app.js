/* 燕云凭据面板逻辑:bridge 调后端 API,轮询登录状态。 */

const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage || null;
let timer = null;

function setState(text) {
  $("state").textContent = text;
}
function showError(text) {
  $("err").textContent = text || "";
}
function showResult(result, saved) {
  if (!result) return;
  $("result").classList.remove("hidden");
  $("r-role").textContent = result.role_id || "";
  $("r-token").textContent = result.token || "";
  $("r-dts").textContent = result.dts || "";
  $("r-saved").textContent = saved
    ? "✓ 已自动写入插件配置,发「燕云状态」即可验证"
    : "⚠ 未能自动写入配置,请将上面 token 手动填入插件配置";
}

function stopPolling() {
  if (timer) { clearInterval(timer); timer = null; }
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
    if (st.state === "done") {
      stopPolling();
      showResult(st.result, st.saved);
      $("btn-start").disabled = false;
    } else if (st.state === "failed" || st.state === "timeout") {
      stopPolling();
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
  const roleId = $("role-id").value.trim();
  if (!/^\d{6,20}$/.test(roleId)) {
    showError("请先填写正确的燕云角色编号(纯数字),可在游戏内个人信息页查看");
    return;
  }
  $("btn-start").disabled = true;
  try {
    const r = await bridge.apiPost("login/start", { role_id: roleId });
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
  try { await bridge.apiPost("login/cancel", {}); } catch {}
  stopPolling();
  $("btn-start").disabled = false;
  $("qr-wrap").classList.add("hidden");
  setState("已取消");
}

async function init() {
  $("btn-start").addEventListener("click", start);
  $("btn-cancel").addEventListener("click", cancel);
  if (!bridge) {
    showError("未检测到 AstrBotPluginView bridge,请确认 AstrBot 版本 ≥ 4.24.1 并重新加载插件");
    $("btn-start").disabled = true;
    return;
  }
  try {
    await bridge.ready();
  } catch {}
}

init();
