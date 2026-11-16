/* sig 签名模块探测脚本 v4(排障用,把整屏输出发回给维护者)
 *
 * 在已登录的 ds.163.com 页面控制台运行。
 * 做三件事:
 *   1. await window.sig.default() 看装载出来的真实模块结构
 *   2. 对模块上的每个函数尝试传入请求体调用
 *   3. 劫持 window.sig.default,记录页面自己调用它的姿势
 */

(async () => {
  const ROLE_ID = "0488796812";
  const body = { appKey: "h72", roleId: ROLE_ID, server: "1", source: 1, type: "yearly" };

  if (!window.sig || !window.sig.default) {
    console.log("window.sig.default 不存在");
    return;
  }
  const orig = window.sig.default;
  console.log("default 源码:", String(orig).slice(0, 400));

  // ---------- 1. await 装载 ----------
  let mod = null;
  try {
    mod = await orig();
    console.log("await orig() →", mod);
  } catch (e) {
    console.warn("await orig() 异常:", e && e.message);
  }

  const dumpShape = (obj, label) => {
    if (!obj) return;
    console.log(`[${label}] typeof =`, typeof obj);
    if (typeof obj === "function") console.log(`[${label}] 源码:`, String(obj).slice(0, 800));
    try { console.log(`[${label}] 自有键:`, Object.keys(obj)); } catch {}
    let proto = Object.getPrototypeOf(obj);
    let hop = 0;
    while (proto && proto !== Object.prototype && hop < 4) {
      console.log(`[${label}] 原型链第${hop + 1}层:`, Object.getOwnPropertyNames(proto));
      proto = Object.getPrototypeOf(proto);
      hop++;
    }
  };
  dumpShape(mod, "模块");

  // ---------- 2. 对模块上的函数逐个传 body 调用 ----------
  const tryCall = async (obj, name, path) => {
    try {
      let r = obj[name](body);
      if (r && typeof r.then === "function") r = await r;
      const s = (() => { try { return JSON.stringify(r); } catch { return String(r); } })();
      console.log(`${path}(body) →`, s && s.slice(0, 500));
      if (r && (r.sign || r.checksum || r.checkSum || r.GL_CheckSum)) {
        console.log(`✅✅ 找到签名!签名函数是 ${path}`);
      }
    } catch (e) {
      console.log(`${path}(body) 异常:`, (e && e.message) || e);
    }
  };

  if (mod && (typeof mod === "object" || typeof mod === "function")) {
    const keys = (() => { try { return Object.keys(mod); } catch { return []; } })();
    for (const k of keys) {
      if (typeof mod[k] === "function") await tryCall(mod, k, `mod.${k}`);
    }
    let proto = Object.getPrototypeOf(mod);
    let hop = 0;
    while (proto && proto !== Object.prototype && hop < 3) {
      for (const k of Object.getOwnPropertyNames(proto)) {
        if (k === "constructor") continue;
        try { if (typeof mod[k] === "function") await tryCall(mod, k, `mod.proto${hop}.${k}`); } catch {}
      }
      proto = Object.getPrototypeOf(proto);
      hop++;
    }
  }

  // ---------- 3. 劫持记录页面自己的调用 ----------
  window.__sig_calls = window.__sig_calls || [];
  if (!window.__sig_hijacked) {
    window.sig.default = function (...args) {
      let r;
      try { r = orig.apply(this, args); } catch (e) { console.warn("原函数异常", e); throw e; }
      window.__sig_calls.push({ args, ret: r });
      console.log("🔔 页面调用了签名函数!参数 =", args, "返回 =", r);
      return r;
    };
    window.__sig_hijacked = true;
    console.log("已劫持 window.sig.default。接下来在页面里随便点(切菜单/打开搜索/进圈子等),");
    console.log("看到 🔔 行就复制发回;也可以运行 __sig_calls 查看全部捕获。");
  }

  console.log("===== 探测结束,整屏复制发回给维护者 =====");
})();
