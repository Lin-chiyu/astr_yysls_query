/* sig 取证探针 v3:抓 CheckSum 拼接现场 + 调用栈(定位拦截器源码)
 *
 * 用法:刷新 ds.163.com → 控制台粘贴本脚本 → 找个帖子点赞 → 把
 *       🔧 / 📤 开头的行(尤其含"调用栈"的)整屏复制发回给维护者。
 */

(() => {
  if (window.__probe3) { console.log("[探针3] 已装过,刷新页面后重跑"); return; }
  window.__probe3 = true;

  // 劫持 open:记录每个 XHR 的 URL
  const oOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (m, u) {
    this.__probe_url = String(u);
    return oOpen.apply(this, arguments);
  };

  // 劫持 setRequestHeader:GL-CheckSum 时打印值 + 调用栈(定位拦截器代码位置)
  const oSrh = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    try {
      if (/^GL-(CheckSum|Nonce)$/i.test(k)) {
        const stack = (new Error().stack || "")
          .split("\n")
          .filter((l) => l.includes("http") && !l.includes("probe_sig3"))
          .slice(0, 4)
          .join("\n");
        console.log("[探针3] 🔧", k, "=", v, "\n调用栈:\n" + stack);
      }
    } catch {}
    return oSrh.apply(this, arguments);
  };

  // 劫持 send:打印 URL + 请求体
  const oSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (b) {
    try {
      if (this.__probe_url && /ds\.163\.com/.test(this.__probe_url)) {
        console.log("[探针3] 📤", this.__probe_url, "\n请求体 =", b);
      }
    } catch {}
    return oSend.apply(this, arguments);
  };

  console.log("[探针3] 就绪!去点一个赞,然后把 🔧/📤 行整屏复制发回。");
})();
