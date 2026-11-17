# astrbot_plugin_yanyun_query

AstrBot 插件:查询《燕云十六声》角色档案(等级/门派/功力/成就/家园/在线时长等),
并在 WebUI 面板提供**一键扫码获取凭据**。

**协议已实测打通**(2026-11),数据来自网易大神 datamsapi 生涯接口。

## 数据来源与鉴权原理(一段话)

网易没有开放 API,大神 App 端接口有私有加密签名无法模拟。本插件走大神**网页版**
的公开体系:凭据 = `getReportToken` 签发的**年度 token**(dts=年份),请求校验
`GL-CheckSum = sha1(body + XSRF)`,XSRF/设备号均可自制——所以机器人服务端只需要
token 本身,签发后完全自动化。token 与角色绑定,一个 token 查一个角色。

## 安装

1. 插件目录(或 zip 解压)放进 AstrBot 的 `data/plugins/`
2. **浏览器环境会自动安装,无需手动操作**:
   - 插件加载时后台检查:没有 playwright 包就 `pip install playwright`,没有浏览器就下载 chromium(约 150MB,默认 npmmirror 镜像)
   - 有系统 Chrome/Edge 的机器直接复用,不下载任何东西
   - 如果想关闭自动安装:`auto_install_browser: false` / `preinstall_browser: false`
   - Linux 裸机若提示缺系统库,以 root 执行一次 `playwright install-deps chromium`
3. 重启 AstrBot(或 WebUI 插件页重载)
4. 发 `燕云帮助` 确认生效

> 面板一键获取需要 AstrBot **>= 4.27.5**(views + 插件 API);旧版本仍可用下文手动方式。

### Linux 服务器部署(可交给运维/agent 执行)

zip 上传到服务器后,按以下步骤执行:

```text
1. 找到 AstrBot 安装目录,把 zip 解压到其 data/plugins/ 下,得到
   data/plugins/astrbot_plugin_yanyun_query/ 目录(含 main.py、metadata.yaml、
   gamedata/ 映射表、views/ 面板页)。
2. 用 AstrBot 所用的 Python 环境安装依赖:
   pip install aiohttp playwright pillow
   playwright install chromium
   如果 chromium 下载失败或启动报缺系统库,依次尝试:
   playwright install-deps chromium(需要 root/apt)
   下载慢可设镜像:export PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/
   插件会自动回退到系统 Chrome/Edge(channel=chrome/msedge),服务器上已有其一也能跑。
3. 重启 AstrBot(或在 WebUI 插件页重载插件)。
4. 验证:聊天里发「燕云帮助」应返回指令列表;WebUI 插件详情页应出现
   「燕云凭据获取」面板。
5. 凭据配置二选一:
   a. 面板一键获取:打开面板 → 填燕云角色编号(纯数字)→ 点「一键获取 token」
      → 手机大神 App 扫屏幕上的二维码登录 → 自动写入配置;
   b. 或在插件配置 dashen_accounts 手动填:
      [{"name":"xx","role_id":"角色编号","token":"年度token","dts":"2026","enabled":true}]
      (token 手动获取方式见下文「凭据获取」)。
6. 发「燕云状态」(管理员)确认凭据有效,再发「燕云我的」看查询输出。
```

注意:
- 不要改动插件目录里的 `gamedata/`(游戏名称映射表,渲染依赖它)
- AstrBot 版本低于 4.27.5 时面板功能自动禁用,查询指令不受影响

## 凭据获取

### 方式一:群友私聊自助扫码(推荐)

1. 群友**私聊**机器人发「燕云扫码」(群里发会提示转私聊)
2. 按提示回复自己的燕云角色编号
3. 机器人发出登录二维码 → 群友用手机大神 App 扫码登录
4. 自动签发 token、写入配置、自动绑定该群友 —— 全程无需管理员

> 每次只有一个扫码任务进行中(后来者会收到"稍后再试");二维码约 2 分钟有效。

### 方式二:WebUI 面板一键获取(管理员)

1. WebUI → 插件 → 燕云十六声查询 → 详情页 → 打开「燕云凭据获取」面板
2. 填入燕云角色编号(游戏内个人信息页可查,纯数字)
3. 点「一键获取 token」→ 面板显示登录二维码(日志页同步输出点阵版)
4. 手机大神 App 扫码登录 → 自动签发 token 并写入插件配置
5. 发 `燕云状态` 验证

> - 二维码约 2 分钟有效,过期请「取消」后重新获取
> - token 绑定角色:查询多个角色就分别用对应账号扫码,配置里会自动追加
> - token 是敏感凭据,请勿外传

### 方式三:手动(降级方案)

1. 电脑浏览器打开 [ds.163.com](https://ds.163.com),手机大神 App 扫码登录
2. F12 → Console(粘贴被拦就先输入 `allow pasting` 回车)
3. 粘贴 `tools/get_token.js` 全部内容 → 回车
4. 从绿色输出复制 **role_id / token / dts** 三行,填进插件配置 `dashen_accounts`

## 配置说明

| 配置项 | 说明 |
|---|---|
| `dashen_accounts` | 凭据池:`[{name, role_id, token, dts, enabled}]`,每项一个角色 |
| `server` | 区服标识,默认 `1`(正式服) |
| `timeout` | 请求超时秒数,默认 15 |
| `cooldown_seconds` | token 失效(401)后冷却秒数,默认 3600 |
| `auto_install_browser` | 缺无头浏览器时自动安装,默认开 |
| `preinstall_browser` | 插件加载时后台预装浏览器,默认开 |
| `browser_download_mirror` | chromium 下载镜像,默认 npmmirror |
| `attrs_image` | 「燕云详情」属性一览以图片卡片发送,默认开;失败自动回退文本 |
| `advisor_image` | 「燕云分析」对标报告以图片卡发送,默认开;失败自动回退文本 |
| `profile_image` | 「燕云我的/燕云查询」角色档案以图片卡发送,默认开;失败自动回退文本 |

## 指令

| 指令 | 别名 | 说明 |
|---|---|---|
| `燕云扫码` | `yy扫码` | 私聊扫码登录并自动绑定(推荐入口) |
| `燕云绑定 <角色编号>` | `yy绑定` | 绑定自己的角色(需在凭据池中) |
| `燕云解绑` | `yy解绑` | 解除绑定 |
| `燕云我的` | `yy我的` | 角色档案图片卡(功力/探索度进度条/流派配置) |
| `燕云查询 <角色编号>` | `yy查询` | 查凭据池内的角色(同「燕云我的」卡片) |
| `燕云详情 <角色编号>` | `yy详情` | 家园/面板属性/区域收集;属性一览发图片卡(词条按装备分列+汇总条数,失败回退文本) |
| `燕云战绩 <角色编号>` | `yy战绩` | PVP 数据(字段解析待补全) |
| `燕云状态` | `yy状态` | 凭据池自检(仅管理员) |
| `燕云帮助` | `yy帮助` | 指令说明 |

## 自然语言唤起(可选)

AstrBot 配置了大模型提供商后,可以直接用自然语言触发查询(基于 LLM 工具调用):

- "看看我的燕云角色" / "查一下探索度" / "我的流派是什么" → 角色档案
- "我的面板属性怎么样" / "家园数据" → 详情与面板属性
- "PVP 数据" → 战绩

已注册的 LLM 工具:yanyun_query_profile / yanyun_query_detail / yanyun_query_pvp。
不配置大模型也不影响指令使用。

## 已知限制

- **只能查凭据池内的角色**:token 绑定角色,谁的 token 查谁的角色
- token 年度有效;失效后查询报"token 失效",面板重新扫码或手动更新即可
- 面板一键获取依赖 playwright + chromium(或系统 Chrome/Edge);Windows/Linux 均支持,
  启动顺序为 chromium → 系统 Chrome → 系统 Edge
- `base.school`(门派)当前返回数字编号,映射表待社区确认
- PVP 的 type 字段含义待确认,后续版本解析具体战绩
- 非公开接口,网易随时可能调整;端点/签名逻辑集中在 `api.py`,便于跟进

## 实测工具(tools/)

- `get_token.js` — 手动方式:浏览器控制台签发年度 token
- `probe.py` — 独立实测脚本,可脱离 AstrBot 验证凭据可用性

## 免责声明

本插件调用网易非公开接口,仅供学习交流,数据版权归网易所有;请勿用于商业用途
或高频刷接口。token 属个人凭据,泄露后果自负。接口变更导致失效属正常现象。
