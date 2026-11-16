/* sig 调用姿势抓取探针(排障用,把 🔔 / 🌐 行整屏发回给维护者)
 *
 * 在已登录的 ds.163.com 页面控制台运行。装好后正常使用页面
 * (切菜单/进圈子/打开个人主页/消息等),看到 🔔 或 🌐 行就复制发回。
 * 等不到就在页面里多点点,或等页面自身的轮询请求。
 */

(() => {
  const TAG = "[sig探针]";

  // ---------- 1. 劫持 fetch:看页面自己发的签名请求长什么样 ----------
  if (!window.__orig_fetch) {
    window.__orig_fetch = window.fetch;
    window.fetch = function (input, init) {
      try {
        const url = typeof input === "string" ? input : (input && input.url) || "";
        if (url && /ds\.163\.com/.test(url) && init && init.headers) {
          console.log(TAG + " 🌐 页面请求:", url);
          console.log(TAG + " 🌐 请求头 =", JSON.stringify(init.headers, null, 1));
          console.log(TAG + " 🌐 请求体 =", init.body);
        }
      } catch {}
      return window.__orig_fetch.apply(this, arguments);
    };
    console.log(TAG + "已劫持 fetch");
  }

  // ---------- 2. 劫持 sig.default:看页面怎么调签名函数 ----------
  if (window.sig && window.sig.default && !window.__sig_hijacked_v2) {
    const orig =
      window.__sig_orig ||
      (async () => {
        console.log(TAG + "重新装载签名模块…");
        return window.sig.default();
      });
    // 注意:如果之前已劫持过,window.sig.default 已经是包装函数,
    // 这里直接包当前值,层层包装不影响记录。
    const cur = window.sig.default;
    window.sig.default = function (...args) {
      console.log(TAG + " 🔔 sig.default 被调用!入参 =", JSON.stringify(args).slice(0, 500));
      const r = cur.apply(this, args);
      Promise.resolve(r)
        .then((mod) => {
          if (mod && typeof mod.gen_sign === "function") {
            // 帮用户把真实 gen_sign 也劫持了:记录页面传给 gen_sign 的参数和返回
            if (!mod.__gen_sign_wrapped) {
              const origGen = mod.gen_sign;
              mod.gen_sign = function (...a) {
                console.log(TAG + " 🔔 gen_sign 入参 =", JSON.stringify(a).slice(0, 800));
                const out = origGen.apply(this, a);
                console.log(TAG + " 🔔 gen_sign 返回 =", String(out).slice(0, 500));
                return out;
              };
              mod.__gen_sign_wrapped = true;
              console.log(TAG + "已劫持 gen_sign");
            }
          }
        })
        .catch(() => {});
      return r;
    };
    window.__sig_hijacked_v2 = true;
    console.log(TAG + "已劫持 sig.default(下次页面调用时生效)");
  } else {
    console.log(TAG + "sig.default 已被劫持过,跳过(刷新页面后重跑本脚本可重新装载)");
  }

  console.log(TAG + "===== 探针就绪。现在去页面里正常点点(切菜单/进圈子/打开搜索),");
  console.log(TAG + "出现 🔔(签名调用)和 🌐(网络请求)行后,整屏复制发回给维护者 =====");
})();
