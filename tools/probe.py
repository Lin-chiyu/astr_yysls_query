"""h72roletool 接口实测脚本(独立运行,零依赖,不装 AstrBot 也能跑)。

用途:验证 api.py 里推测的端点/参数/鉴权头是否正确,拿到真实响应结构,
      然后据此精修插件里的 api.py 和 render.py。

用法:
  1. 按下面 CONFIG 区的注释填入你抓到的 token / role_id
  2. 命令行运行:python tools/probe.py
  3. 把输出全部复制发给开发者(注意输出里含 token 的行可以打码)

⚠ 输出会包含接口返回的真实角色数据,分享前自行确认是否脱敏。
"""

import json
import ssl
import urllib.error
import urllib.request

# ============================ CONFIG ==================================
TOKEN = ""      # 浏览器抓到的 GL-Bigdata-Auth-Token 请求头的值(必填)
ROLE_ID = ""    # 你的燕云角色ID(建议填,接口大概率要用)
SERVER = "1"    # GL-Bigdata-Server 的值
# ======================================================================

BASE = "https://datamsapi.ds.163.com"

ENDPOINTS = [
    "/v1/h72roletool/roleInfo",
    "/v1/h72roletool/queryPvp",
    "/v1/h72roletool/proxyGameRole",
]

# 依次尝试的请求体(接口字段名未公开,多试几种)
BODIES = [
    {},
    {"roleId": ROLE_ID},
    {"roleKey": ROLE_ID},
    {"roleId": ROLE_ID, "server": SERVER},
    {"name": ROLE_ID},
]

HEADERS = {
    "GL-Bigdata-Auth-Token": TOKEN,
    "GL-Bigdata-Role-Id": ROLE_ID,
    "GL-Bigdata-Server": SERVER,
    "GL-ClientType": "60",
    "Origin": "https://act.ds.163.com",
    "Referer": "https://act.ds.163.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 "
        "app/df_client dfVersion/100111"
    ),
    "Content-Type": "application/json",
}

CTX = ssl.create_default_context()
MAX_SHOW = 2000


def show(title: str, text: str) -> None:
    print(f"\n===== {title} =====")
    print(text[:MAX_SHOW] + ("\n...(截断)" if len(text) > MAX_SHOW else ""))


def try_post(endpoint: str, body: dict) -> None:
    url = BASE + endpoint
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=HEADERS, method="POST")
    body_desc = json.dumps(body, ensure_ascii=False)
    try:
        with urllib.request.urlopen(req, timeout=15, context=CTX) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            show(f"POST {endpoint}  body={body_desc}  → HTTP {resp.status}", raw)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        show(f"POST {endpoint}  body={body_desc}  → HTTP {e.code}", detail)
    except Exception as e:
        show(f"POST {endpoint}  body={body_desc}  → 异常", f"{type(e).__name__}: {e}")


def main() -> None:
    if not TOKEN:
        print("请先在脚本顶部 CONFIG 区填入 TOKEN(获取方式见 README.md)")
        return
    print(f"凭据:token={'已填(' + str(len(TOKEN)) + '字符)'} role_id={ROLE_ID or '(未填)'}")
    for ep in ENDPOINTS:
        for body in BODIES:
            try_post(ep, body)
    print("\n===== 实测结束 =====")
    print("把上面所有输出(可脱敏)发给开发者,用于精修 api.py / render.py。")


if __name__ == "__main__":
    main()
