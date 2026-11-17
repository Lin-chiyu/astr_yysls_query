"""扫码登录服务:Playwright 拿登录 cookie → 纯 HTTP 签发年度 token。

全流程已实测打通(2026-11-17):
  1. headless 浏览器打开 ds.163.com(裸域,www 子域无 DNS 记录)
  2. 点"登录" → URS 弹窗(dl.reg.163.com/webzj)里的二维码是 JS 动态渲染、无 src,
     因此用全页截图 + 固定坐标裁剪(viewport 固定 1440x900,坐标稳定)
  3. 轮询 cookie,GL-XSRF-TOKEN + GOD_UUID 出现即登录成功
  4. 全量 cookie + sha1(body+XSRF) 纯 HTTP 调 getReportToken(实测可用,
     无需 window.sig/WASM;同角色同年服务器返回同一 token)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import sys
import time
import uuid
from typing import Any, Callable

import aiohttp
from astrbot.api import logger

try:
    from playwright.async_api import async_playwright

    PLAYWRIGHT_OK = True
except ImportError:  # 部署机未装 playwright 时插件其余功能不受影响
    async_playwright = None  # type: ignore[assignment]
    PLAYWRIGHT_OK = False

try:
    from PIL import Image

    PIL_OK = True
except ImportError:
    PIL_OK = False

HOME_URL = "https://ds.163.com/"
# viewport 固定 1440x900,登录弹窗二维码在页面上的稳定裁剪区域(x1,y1,x2,y2)
QR_CROP_BOX = (390, 300, 640, 560)
SCAN_TIMEOUT_SECONDS = 180
ISSUE_URL = "https://inf.ds.163.com/v1/web/game/report/getReportToken"

# ---- 浏览器环境自愈(首次使用自动安装) ----
DEFAULT_MIRROR = "https://npmmirror.com/mirrors/playwright/"
BROWSER_DOWNLOAD_TIMEOUT = 600  # 下载 chromium 超时(秒)
LAUNCH_PROBE_TIMEOUT = 45
_LAUNCH_TARGETS = (
    ("chromium(内置)", {}),
    ("系统 Chrome", {"channel": "chrome"}),
    ("系统 Edge", {"channel": "msedge"}),
)


async def _run_cmd(args: list[str], env_extra: dict | None = None, timeout: int = BROWSER_DOWNLOAD_TIMEOUT) -> tuple[bool, str]:
    """跑子进程,返回 (成功, 合并输出尾部)。"""
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
    except Exception as e:
        return False, f"无法启动子进程: {type(e).__name__}: {e}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return False, f"命令超时(>{timeout}s)"
    text = (out or b"").decode("utf-8", errors="replace")
    return proc.returncode == 0, text[-800:]


def _is_missing_system_libs(err: str) -> bool:
    low = err.lower()
    return any(
        k in low
        for k in (
            "shared libraries",
            "error while loading shared lib",
            "missing dependencies",
            "install-deps",
            "libnss3",
            "libatk",
        )
    )


async def _probe_launch() -> tuple[bool, str, dict]:
    """探测可用浏览器:内置 chromium → 系统 Chrome → 系统 Edge。"""
    global async_playwright, PLAYWRIGHT_OK
    if not PLAYWRIGHT_OK:
        try:
            from playwright.async_api import async_playwright as _ap  # noqa: PLC0415

            async_playwright = _ap
            PLAYWRIGHT_OK = True
        except ImportError as e:
            return False, f"playwright 包不可用: {e}", {}

    try:
        pw = await async_playwright().start()
    except Exception as e:
        return False, f"playwright 驱动启动失败: {type(e).__name__}: {e}", {}

    errors = []
    try:
        for label, kwargs in _LAUNCH_TARGETS:
            try:
                browser = await asyncio.wait_for(
                    pw.chromium.launch(headless=True, **kwargs), timeout=LAUNCH_PROBE_TIMEOUT
                )
                await browser.close()
                await pw.stop()
                return True, f"可用浏览器: {label}", kwargs
            except Exception as e:
                errors.append(f"{label}: {type(e).__name__}: {str(e)[:200]}")
    finally:
        try:
            await pw.stop()
        except Exception:
            pass
    detail = " | ".join(errors)
    return False, detail, {}


_ensure_lock: asyncio.Lock | None = None


def _get_ensure_lock() -> asyncio.Lock:
    global _ensure_lock
    if _ensure_lock is None:
        _ensure_lock = asyncio.Lock()
    return _ensure_lock


async def ensure_browser_ready(
    mirror: str = "", auto_install: bool = True, progress: Callable[[str], None] | None = None
) -> tuple[bool, str, dict]:
    """确保有可用的无头浏览器,缺失时自动安装(Windows / Linux 通用)。

    返回 (可用, 说明, launch kwargs)。
    顺序:pip 装 playwright 包 → 探测内置/系统浏览器 → playwright install chromium → 复探。
    """
    async with _get_ensure_lock():
        return await _ensure_browser_ready_locked(mirror, auto_install, progress)


async def _ensure_browser_ready_locked(
    mirror: str, auto_install: bool, progress: Callable[[str], None] | None
) -> tuple[bool, str, dict]:
    def say(msg: str):
        logger.info(f"[燕云查询] {msg}")
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    # 1) playwright 包
    ok, msg, kwargs = await _probe_launch()
    if ok:
        return True, msg, kwargs
    if "playwright 包不可用" in msg:
        if not auto_install:
            return False, "未安装 playwright 包,且自动安装已关闭(配置 auto_install_browser)", {}
        say("检测到未安装 playwright 包,正在 pip 安装(首次约 1 分钟)…")
        good, out = await _run_cmd([sys.executable, "-m", "pip", "install", "-q", "playwright"], timeout=300)
        if not good:
            return False, f"pip 安装 playwright 失败: {out}", {}
        say("playwright 包安装完成,重新探测…")
        ok, msg, kwargs = await _probe_launch()
        if ok:
            return True, msg, kwargs

    # 2) 尝试装 chromium
    if not auto_install:
        return False, f"没有可用浏览器,且自动安装已关闭:{msg}", {}
    host = (mirror or os.environ.get("PLAYWRIGHT_DOWNLOAD_HOST") or DEFAULT_MIRROR).strip()
    say(f"没有可用浏览器,开始下载 chromium(约 150MB,镜像 {host})…")
    good, out = await _run_cmd(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        env_extra={"PLAYWRIGHT_DOWNLOAD_HOST": host},
    )
    if not good:
        say("镜像下载失败,尝试官方源…")
        good, out2 = await _run_cmd([sys.executable, "-m", "playwright", "install", "chromium"])
        if not good:
            return False, f"chromium 下载失败(镜像+官方均失败): {out2 or out}", {}

    say("chromium 下载完成,重新探测…")
    ok, msg, kwargs = await _probe_launch()
    if ok:
        return True, msg, kwargs

    # 3) Linux 常见:缺系统库,需要 root 装依赖
    if _is_missing_system_libs(msg):
        return (
            False,
            "chromium 已下载但缺少系统依赖库。Linux 请以 root 执行:\n"
            "  playwright install-deps chromium\n"
            "  (Debian/Ubuntu 也可:apt-get install -y libnss3 libatk1.0-0 libatk-bridge2.0-0 "
            "libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 "
            "libgbm1 libpango-1.0-0 libasound2)\n"
            f"探测详情:{msg}",
            {},
        )
    return False, f"已尝试安装,浏览器仍不可用:{msg}", {}


def qr_png_to_ascii(png_bytes: bytes, width: int = 56) -> str:
    """二维码 PNG → 半块字符点阵(用于日志面板显示兜底)。"""
    if not PIL_OK:
        return "(未安装 Pillow,无法生成点阵)"
    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    h = max(1, int(img.height * width / img.width / 2))
    small = img.resize((width, h * 2))
    px = small.load()
    lines = []
    for y in range(0, h * 2, 2):
        row = ""
        for x in range(width):
            top = px[x, y] < 140
            bottom = px[x, y + 1] < 140
            row += "█" if (top and bottom) else "▀" if top else "▄" if bottom else " "
        lines.append(row)
    return "\n".join(lines)


class LoginService:
    """扫码登录 + token 签发状态机(单任务)。

    两种模式:
      - start(role_id) : 角色已知(凭据失效续期),扫码成功后直接签发。
      - start()        : 角色未知(群友首次登录),扫码成功后回调 on_roles
                         取该账号下的燕云角色,交由调用方选择后再
                         issue_for(role_id) 签发。
    """

    def __init__(
        self,
        server: str = "1",
        on_success: Callable[[dict, list[dict]], Any] | None = None,
        on_roles: Callable[[list[dict]], Any] | None = None,
        mirror: str = "",
        auto_install: bool = True,
    ):
        self._server = server or "1"
        self._mirror = mirror
        self._auto_install = auto_install
        self._on_success = on_success  # 回调签名:(result, cookies)
        self._on_roles = on_roles  # 回调签名:(cookies) -> list[dict]
        self._state = "idle"
        self._message = "待机"
        self._qr_b64 = ""
        self._result: dict | None = None
        self._saved = False
        self._roles: list[dict] = []
        self._cookies: list[dict] = []
        self._task: asyncio.Task | None = None
        self._browser_lock = asyncio.Lock()
        self._pw = None
        self._browser = None

    # ------------------------------------------------------------------ 对外

    def status(self) -> dict:
        return {
            "state": self._state,
            "message": self._message,
            "qr_b64": self._qr_b64,
            "result": self._result,
            "saved": self._saved,
            "roles": self._roles,
        }

    async def start(self, role_id: str | None = None) -> dict:
        if not PLAYWRIGHT_OK:
            return {"started": False, "message": "未安装 playwright,请先 pip install playwright && playwright install chromium"}
        if self._task and not self._task.done():
            return {"started": False, "message": "已有登录任务进行中,请先取消或等待完成"}
        role_id = (role_id or "").strip()
        self._reset(role_id)
        self._task = asyncio.create_task(self._run(role_id))
        return {"started": True, "message": "已启动"}

    async def issue_for(self, role_id: str) -> dict:
        """用本次扫码的登录态给指定角色签发 token(角色选择后调用)。"""
        role_id = str(role_id or "").strip()
        if not self._cookies:
            return {"ok": False, "message": "登录态已失效,请重新发「燕云登录」"}
        if self._roles and role_id not in {r["role_id"] for r in self._roles}:
            return {"ok": False, "message": f"角色 {role_id} 不在本次登录的账号里,请重新选择"}
        if not role_id:
            return {"ok": False, "message": "缺少角色编号"}
        try:
            await self._finish_issue(role_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[燕云查询] 签发失败(角色 {role_id}): {e}")
            self._state, self._message = "failed", f"签发失败:{e}"
        return {"ok": self._state == "done", "message": self._message, "result": self._result}

    async def cancel(self) -> dict:
        had_task = bool(self._task and not self._task.done())
        if had_task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        # 无论任务是否已结束都清掉登录态与待选角色,避免旧 cookie 被继续使用
        self._state, self._message = "idle", "已取消"
        self._roles, self._cookies = [], []
        self._qr_b64 = ""
        return {"cancelled": had_task}

    async def close(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception as e:
            logger.warning(f"[燕云查询] 浏览器清理异常: {e}")
        finally:
            self._browser = None
            self._pw = None

    # ------------------------------------------------------------------ 内部

    def _reset(self, role_id: str):
        if role_id:
            self._state, self._message = "starting", f"正在打开登录页(角色 {role_id})..."
        else:
            self._state, self._message = "starting", "正在打开登录页..."
        self._qr_b64 = ""
        self._result = None
        self._saved = False
        self._roles = []
        self._cookies = []

    async def _ensure_browser(self):
        """返回可用 browser;环境缺失时自动安装(进度写入 state.message,面板可见)。"""
        async with self._browser_lock:
            if self._browser and self._browser.is_connected():
                return self._browser
            self._message = "正在检查浏览器环境…"
            ok, msg, kwargs = await ensure_browser_ready(
                mirror=self._mirror,
                auto_install=self._auto_install,
                progress=lambda m: setattr(self, "_message", m),
            )
            if not ok:
                raise RuntimeError(msg)
            global async_playwright
            if not PLAYWRIGHT_OK or async_playwright is None:
                ok2, msg2, kwargs2 = await _probe_launch()
                if not ok2:
                    raise RuntimeError(msg2)
                kwargs = kwargs2 or kwargs
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=True, **kwargs)
            logger.info(f"[燕云查询] 浏览器就绪({msg})")
            return self._browser

    async def _run(self, role_id: str | None):
        context = None
        try:
            browser = await self._ensure_browser()
            context = await browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            self._message = "正在打开大神网页版..."
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # 点登录入口(启发式)
            clicked = False
            for sel in ("text=登录", "a:has-text('登录')", "button:has-text('登录')"):
                try:
                    loc = page.locator(sel).first
                    await loc.wait_for(state="visible", timeout=4000)
                    await loc.click()
                    clicked = True
                    break
                except Exception:
                    continue
            if not clicked:
                raise RuntimeError("未找到登录入口,可能页面改版,请使用 README 手动方式")
            await page.wait_for_timeout(3000)

            # 全页截图 → 裁剪二维码 → base64 + 点阵日志
            self._state, self._message = "waiting", "二维码已就绪,请用手机大神 App 扫码登录"
            shot = await page.screenshot(full_page=False)
            qr_png = await self._crop_qr(shot)
            self._qr_b64 = base64.b64encode(qr_png).decode("ascii")
            try:
                logger.info(
                    "[燕云查询] 登录二维码点阵(也可在面板查看图片):\n" + qr_png_to_ascii(qr_png)
                )
            except Exception:
                pass

            # 轮询登录 cookie
            deadline = time.monotonic() + SCAN_TIMEOUT_SECONDS
            cookies = None
            while time.monotonic() < deadline:
                await asyncio.sleep(3)
                jar = {c["name"]: c["value"] for c in await context.cookies(HOME_URL)}
                if jar.get("GL-XSRF-TOKEN") and jar.get("GOD_UUID"):
                    cookies = await context.cookies(HOME_URL)
                    break
            if not cookies:
                self._state, self._message = "timeout", "扫码超时,请重新获取"
                return
            logger.info(f"[燕云查询] 登录成功,共 {len(cookies)} 条 cookie")
            await context.close()
            context = None
            self._cookies = cookies
            self._qr_b64 = ""

            if role_id:
                await self._finish_issue(role_id)
                return

            # 角色未知:拉取该账号下的燕云角色,交给调用方选择后再 issue_for
            self._state, self._message = "issuing", "登录成功,正在读取账号下的燕云角色..."
            try:
                roles = await self._on_roles(cookies) if self._on_roles else []
            except Exception as e:
                logger.error(f"[燕云查询] 读取角色列表失败: {e}")
                self._state, self._message = "failed", f"读取角色列表失败:{e}"
                return
            self._roles = roles or []
            if not self._roles:
                self._state = "failed"
                self._message = "登录成功,但这个账号下没有燕云角色,请先在游戏里创建角色后再试"
                return
            if len(self._roles) == 1:
                await self._finish_issue(self._roles[0]["role_id"])
                return
            self._state = "selecting"
            self._message = f"检测到 {len(self._roles)} 个燕云角色,请选择要绑定的角色"
        except asyncio.CancelledError:
            self._state, self._message = "idle", "已取消"
            raise
        except Exception as e:
            logger.error(f"[燕云查询] 登录流程失败: {e}")
            self._state, self._message = "failed", f"{type(e).__name__}: {e}"
        finally:
            if context:
                try:
                    await context.close()
                except Exception:
                    pass

    async def _crop_qr(self, shot: bytes) -> bytes:
        if not PIL_OK:
            return shot
        img = Image.open(io.BytesIO(shot))
        crop = img.crop(QR_CROP_BOX)
        crop = crop.resize((crop.width * 3, crop.height * 3), Image.LANCZOS)
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        return buf.getvalue()

    async def _finish_issue(self, role_id: str):
        """签发 token 并回调落库(角色已确定时调用)。"""
        self._state, self._message = "issuing", f"正在为角色 {role_id} 签发 token ..."
        result = await self._issue_token(self._cookies, role_id)
        self._result = result
        self._qr_b64 = ""
        if self._on_success:
            try:
                await self._on_success(result, self._cookies)
                self._saved = True
                self._message = f"token 已写入插件配置(角色 {result['role_id']}),登录 cookie 已保存"
            except Exception as e:
                self._message = f"token 签发成功但写入配置失败: {e};请手动填入 token={result['token']}"
            self._state = "done"
        else:
            self._saved = False
            self._message = f"token 签发成功: {result['token']}"
            self._state = "done"
        logger.info(f"[燕云查询] 签发完成: {self._message}")

    async def _issue_token(self, cookies: list[dict], role_id: str) -> dict:
        jar = {c["name"]: c["value"] for c in cookies}
        xsrf = jar["GL-XSRF-TOKEN"]
        uid = jar["GOD_UUID"]
        body = {"appKey": "h72", "roleId": role_id, "server": self._server, "source": 1, "type": "yearly"}
        body_str = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        sign = hashlib.sha1((body_str + xsrf).encode("utf-8")).hexdigest()
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "GL-ClientType": "61",
            "GL-CheckSum": sign,
            "GL-Nonce": str(int(time.time() * 1000)),
            "GL-X-XSRF-TOKEN": xsrf,
            "GL-Uid": uid,
            "GL-DeviceId": str(uuid.uuid4()).split("-")[0],
            "Cookie": "; ".join(f"{k}={v}" for k, v in jar.items()),
            "Origin": "https://ds.163.com",
            "Referer": "https://ds.163.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 app/df_client dfVersion/100111"
            ),
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(ISSUE_URL, data=body_str.encode("utf-8"), headers=headers) as resp:
                data = await resp.json(content_type=None)
        if data.get("code") != 200:
            raise RuntimeError(f"签发失败: {data.get('errmsg') or data}")
        r = data["result"]
        return {
            "role_id": str(r.get("roleId") or role_id),
            "token": r["token"],
            "dts": str(r.get("day") or time.strftime("%Y")),
        }
