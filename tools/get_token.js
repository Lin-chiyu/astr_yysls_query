/* 燕云十六声 getReportToken 凭据脚本 v8(实测可用)
 *
 * 用法(在电脑浏览器上):
 *   1. 打开 https://www.ds.163.com(网易大神网页版),用手机大神 App 扫码登录
 *   2. 按 F12 → Console(控制台)→ 粘贴整个脚本 → 回车
 *      (Chrome 粘贴被拦截时,在控制台手动输入 allow pasting 回车即可)
 *   3. 从绿色输出里复制 role_id / token / dts 三行,填进插件配置
 *
 * 签名机制(chunk 4691 拦截器源码确认):
 *   - body 先 JSON.stringify 成字符串,gen_sign 传"字符串"(不是对象!)
 *   - 返回 {sign, timestamp}:sign 原样作为 GL-CheckSum(本身即两段格式),
 *     timestamp 作为 GL-Nonce
 *   - WASM 内部自行读取浏览器 cookie/localStorage 参与签名
 *   - 其余头:GL-X-XSRF-TOKEN / GL-DeviceId / GL-Uid / GL-ClientType
 *
 * 逻辑参考 astrbot_plugin_ow_dashen(守望先锋同款机制),appKey 换成燕云的 h72。
 */

(async () => {
  // ==================== 填这里 ====================
  const ROLE_ID = "0488796812"; // 你的燕云角色编号
  // ================================================
  const APP_KEY = "h72"; // 燕云十六声内部代号(已确认,勿改)
  const SERVER = "1";
  const URL = "https://inf.ds.163.com/v1/web/game/report/getReportToken";

  const getCookie = (name) => {
    const m = document.cookie.match(new RegExp("(^|;\\s*)" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[2]) : "";
  };

  const bodyStr = JSON.stringify({
    appKey: APP_KEY,
    roleId: ROLE_ID,
    server: SERVER,
    source: 1,
    type: "yearly",
  });

  // ---------------- 1. 装载 WASM 签名模块并生成签名(传字符串!) ----------------
  let sign = "", nonce = "";
  try {
    const mod = await window.sig.default();
    const signStr = mod.gen_sign(bodyStr); // ★ 页面姿势:传 JSON 字符串
    const f = JSON.parse(signStr);
    sign = f.sign;
    nonce = String(f.timestamp);
    console.log("✓ 签名生成成功:", signStr);
  } catch (e) {
    console.warn("签名模块装载/调用失败:", e && e.message, "(刷新页面重试)");
  }

  // ---------------- 2. 构造请求头(与拦截器一致)并发送 ----------------
  const headers = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "GL-Uid": getCookie("GOD_UUID"),
    "GL-ClientType": "61",
    "GL-DeviceId":
      localStorage.getItem("ns-client-id") ||
      localStorage.getItem("ds-website-uuid") ||
      "",
    "GL-X-XSRF-TOKEN": getCookie("GL-XSRF-TOKEN"),
  };
  if (sign) {
    headers["GL-Nonce"] = nonce;
    headers["GL-CheckSum"] = sign;
  }

  const doFetch = async (hdrs, label) => {
    console.log(`>>> ${label} …`);
    try {
      const resp = await fetch(URL, {
        method: "POST",
        credentials: "include",
        headers: hdrs,
        body: bodyStr, // 与签名输入完全一致
      });
      const text = await resp.text();
      console.log(`HTTP ${resp.status}`, text.slice(0, 1000));
      try { return JSON.parse(text); } catch { return null; }
    } catch (e) {
      console.warn(`${label} 网络错误:`, e && e.message);
      return null;
    }
  };

  let json = null;
  if (sign) {
    json = await doFetch({ ...headers }, "带签名请求");
  }
  if (!json || !json.result || !json.result.token) {
    const h2 = { ...headers };
    delete h2["GL-Nonce"];
    delete h2["GL-CheckSum"];
    json = await doFetch(h2, "无签名兜底请求");
  }

  // ---------------- 3. 输出结果 ----------------
  const token = json && json.result && json.result.token;
  if (token) {
    console.log(
      "%c ↓↓↓ 复制下面三行填进插件配置 ↓↓↓",
      "color:#fff;background:#2a7;font-weight:bold;padding:2px 6px"
    );
    console.log("%c role_id:", "color:green;font-weight:bold", (json.result && json.result.roleId) || ROLE_ID);
    console.log("%c token  :", "color:green;font-weight:bold", token);
    console.log("%c dts    :", "color:green;font-weight:bold", (json.result && json.result.day) || String(new Date().getFullYear()));
  } else {
    console.log(
      "%c ✗ 没拿到 token。请把本输出整屏复制发回给维护者。",
      "color:#fff;background:#c33;font-weight:bold;padding:2px 6px"
    );
    console.log("排查信息: code =", json && json.code, "errmsg =", json && json.errmsg);
  }
})();
