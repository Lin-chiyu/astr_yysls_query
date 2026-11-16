"""网易大神 datamsapi 燕云(h72)生涯接口客户端 — 实测协议版。

协议(2026-11-17 用真实凭据实测打通):
  POST https://datamsapi.ds.163.com/v1/h72roletool/{proxyGameRole|roleInfo|queryPvp}
  Headers:
    GL-ClientType: 60
    GL-Uid:        任意 UUID(实测自制可用)
    GL-DeviceId:   任意字符串
    GL-X-XSRF-TOKEN: 任意 UUID(实测自制可用)
    GL-CheckSum:   sha1(body字符串 + GL-X-XSRF-TOKEN 的值)
  Body: {"server":"1","roleId":"<角色编号>","token":"<年度token>","dts":"2026"}
  响应: {"code":0,"data":{...},"success":true,...};401 {"code":401,"errMsg":"Token Invalid"}

token 获取:tools/get_token.js(浏览器控制台,基于大神网页版登录态签发,年度有效)。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import aiohttp

BASE_URL = "https://datamsapi.ds.163.com"

EP_PROXY_ROLE = "/v1/h72roletool/proxyGameRole"  # 角色全量档案(推荐,数据最全)
EP_ROLE_INFO = "/v1/h72roletool/roleInfo"        # 家园/突破/功法日常等
EP_QUERY_PVP = "/v1/h72roletool/queryPvp"        # PVP 数据

BIZ_OK = 0  # 响应里 code==0 为成功


class DashenAPIError(Exception):
    """接口调用失败,message 面向最终用户可读。"""


class TokenInvalidError(DashenAPIError):
    """token 失效(401),需要重新签发。"""


@dataclass
class Credential:
    """一条凭据 = 一个可查询的燕云角色。

    cookies: 扫码登录保存的全量 cookie(可选)。有 cookie 时可调用
    inf/inf-act 域的登录态接口(role-data/benchmark/getBindList 等)。
    """

    name: str
    role_id: str
    token: str
    dts: str = "2026"
    uid: str = ""
    cookies: list[dict] = field(default_factory=list)
    enabled: bool = True
    _cooldown_until: float = field(default=0.0, repr=False)

    def in_cooldown(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def cool_down(self, seconds: int) -> None:
        self._cooldown_until = time.monotonic() + seconds

    def cookie_header(self) -> str:
        return "; ".join(f"{c['name']}={c['value']}" for c in self.cookies if c.get("name"))

    def cookie_value(self, name: str) -> str:
        return next((c["value"] for c in self.cookies if c.get("name") == name), "")


class CredentialPool:
    """凭据池:按角色编号索引,401 时冷却。"""

    def __init__(self, accounts: list[dict], cooldown_seconds: int, uid_cookies: dict[str, list[dict]] | None = None):
        self._cooldown_seconds = cooldown_seconds
        self._creds: list[Credential] = []
        uid_cookies = uid_cookies or {}
        for acc in accounts or []:
            token = str(acc.get("token") or "").strip()
            role_id = str(acc.get("role_id") or "").strip()
            if not token or not role_id:
                continue
            uid = str(acc.get("uid") or "").strip()
            self._creds.append(
                Credential(
                    name=acc.get("name") or f"角色{role_id}",
                    role_id=role_id,
                    token=token,
                    dts=str(acc.get("dts") or "2026").strip(),
                    uid=uid,
                    cookies=uid_cookies.get(uid, []),
                    enabled=bool(acc.get("enabled", True)),
                )
            )

    def __len__(self) -> int:
        return len(self._creds)

    def describe(self) -> str:
        if not self._creds:
            return "凭据池为空,请先在插件配置中添加 dashen_accounts(获取方式见 README)"
        lines = []
        for c in self._creds:
            status = "停用" if not c.enabled else ("冷却中(疑似token过期)" if c.in_cooldown() else "可用")
            extra = "(含登录cookie)" if c.cookies else "(无cookie,面板数据不可用)"
            lines.append(f"- {c.name}(角色{c.role_id},dts={c.dts}):{status} {extra}")
        return "\n".join(lines)

    def by_role(self, role_id: str) -> Credential | None:
        for c in self._creds:
            if c.role_id == str(role_id).strip() and c.enabled:
                return c
        return None

    def any_usable(self) -> Credential | None:
        usable = [c for c in self._creds if c.enabled and not c.in_cooldown()]
        return usable[0] if usable else None

    def mark_auth_failed(self, cred: Credential) -> None:
        cred.cool_down(self._cooldown_seconds)


class DashenClient:
    """datamsapi 燕云接口客户端(单例,由 main.py 持有)。"""

    def __init__(self, accounts: list[dict], server: str, timeout: int, cooldown_seconds: int,
                 uid_cookies: dict[str, list[dict]] | None = None):
        self._pool = CredentialPool(accounts, cooldown_seconds, uid_cookies)
        self._server = server or "1"
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    # -- 生命周期 -----------------------------------------------------------

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # -- 请求 ---------------------------------------------------------------

    @staticmethod
    def _checksum(body_str: str, xsrf: str) -> str:
        return hashlib.sha1((body_str + xsrf).encode("utf-8")).hexdigest()

    async def post(self, endpoint: str, cred: Credential) -> dict[str, Any]:
        """POST 一个端点,body 由凭据构造;401 冷却并抛 TokenInvalidError。"""
        session = self._session
        if session is None or session.closed:
            session = self._session = aiohttp.ClientSession(timeout=self._timeout)

        body: dict[str, Any] = {
            "server": self._server,
            "roleId": cred.role_id,
            "token": cred.token,
            "dts": cred.dts,
        }
        body_str = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        xsrf = str(uuid.uuid4())
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "GL-ClientType": "60",
            "GL-Uid": str(uuid.uuid4()),
            "GL-DeviceId": str(uuid.uuid4()).split("-")[0],
            "GL-X-XSRF-TOKEN": xsrf,
            "GL-CheckSum": self._checksum(body_str, xsrf),
            "Origin": "https://act.ds.163.com",
            "Referer": "https://act.ds.163.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 "
                "app/df_client dfVersion/100111"
            ),
        }
        url = BASE_URL + endpoint
        try:
            async with session.post(url, data=body_str.encode("utf-8"), headers=headers) as resp:
                if resp.status in (401, 403):
                    self._pool.mark_auth_failed(cred)
                    raise TokenInvalidError(
                        f"凭据「{cred.name}」的 token 已失效(HTTP {resp.status}),"
                        "请管理员按 README 重新获取并更新配置。"
                    )
                if resp.status != 200:
                    raise DashenAPIError(f"接口返回 HTTP {resp.status}({endpoint})")
                data = await resp.json(content_type=None)
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            raise DashenAPIError(f"网络请求失败:{type(e).__name__}") from e
        except (TokenInvalidError, DashenAPIError):
            raise
        except Exception as e:
            raise DashenAPIError(f"响应解析失败:{type(e).__name__}") from e

        code = data.get("code") if isinstance(data, dict) else None
        if code != BIZ_OK:
            msg = data.get("errMsg") or data.get("errmsg") or data.get("message") or "无详细信息"
            if code in (401, "401"):
                self._pool.mark_auth_failed(cred)
                raise TokenInvalidError(f"token 失效({msg}),请重新获取凭据。")
            raise DashenAPIError(f"接口业务码 {code}:{msg}")
        return data

    # -- 登录 cookie 鉴权请求(inf / inf-act 域) ------------------------------

    async def post_with_cookies(self, url: str, body: dict[str, Any], cred: Credential) -> dict[str, Any]:
        """带登录 cookie 的 POST(用于 role-data/benchmark/getBindList 等接口)。

        校验体系:GL-CheckSum = sha1(body字符串 + 真实 GL-XSRF-TOKEN cookie 值),
        Origin/Referer 必须是 ds.163.com(实测其他域会被 csrf 拒绝)。
        """
        if not cred.cookies:
            raise DashenAPIError(
                f"凭据「{cred.name}」没有登录 cookie,该功能需要扫码登录获取"
                "(面板一键获取或「燕云扫码」都会自动保存)。"
            )
        session = self._session
        if session is None or session.closed:
            session = self._session = aiohttp.ClientSession(timeout=self._timeout)
        body_str = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        xsrf = cred.cookie_value("GL-XSRF-TOKEN")
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "GL-ClientType": "60",
            "GL-Uid": cred.cookie_value("GOD_UUID"),
            "GL-DeviceId": "astrbot-plugin",
            "GL-X-XSRF-TOKEN": xsrf,
            "GL-CheckSum": self._checksum(body_str, xsrf),
            "Cookie": cred.cookie_header(),
            "Origin": "https://ds.163.com",
            "Referer": "https://ds.163.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 app/df_client dfVersion/100111"
            ),
        }
        try:
            async with session.post(url, data=body_str.encode("utf-8"), headers=headers) as resp:
                if resp.status in (401, 403):
                    self._pool.mark_auth_failed(cred)
                    raise TokenInvalidError(f"登录态失效(HTTP {resp.status}),请重新扫码登录。")
                if resp.status != 200:
                    raise DashenAPIError(f"接口返回 HTTP {resp.status}({url.split('?')[0]})")
                data = await resp.json(content_type=None)
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            raise DashenAPIError(f"网络请求失败:{type(e).__name__}") from e
        except (TokenInvalidError, DashenAPIError):
            raise
        except Exception as e:
            raise DashenAPIError(f"响应解析失败:{type(e).__name__}") from e

        code = data.get("code") if isinstance(data, dict) else None
        if code not in (None, 200, 0):
            msg = data.get("errmsg") or data.get("message") or "无详细信息"
            raise DashenAPIError(f"接口业务码 {code}:{msg}")
        return data

    # -- 业务方法 ------------------------------------------------------------

    async def query_role_data(self, role_id: str) -> dict[str, Any]:
        """调号助手数据(attributePanel/combatPanel/role 结构化),cookie 鉴权。"""
        cred = self._pool.by_role(role_id)
        if cred is None:
            raise DashenAPIError(f"凭据池里没有角色 {role_id}。")
        url = "https://inf-act.ds.163.com/v1/act-web/act-play/h72-role-tool/role-data"
        return await self.post_with_cookies(url, {"roleId": role_id}, cred)

    async def query_benchmark(self, role_id: str) -> dict[str, Any]:
        """全服分档基准(等级段×流派),cookie 鉴权。"""
        cred = self._pool.by_role(role_id)
        if cred is None:
            raise DashenAPIError(f"凭据池里没有角色 {role_id}。")
        url = "https://inf-act.ds.163.com/v1/act-web/act-play/h72-role-tool/benchmark"
        return await self.post_with_cookies(url, {}, cred)

    async def health_check(self) -> list[str]:
        """逐个凭据真实调一次接口,返回自检结果。"""
        results = []
        for cred in self._pool._creds:
            if not cred.enabled:
                results.append(f"[停用] {cred.name}(角色{cred.role_id})")
                continue
            try:
                await self.post(EP_PROXY_ROLE, cred)
                results.append(f"[OK] {cred.name}(角色{cred.role_id}):凭据有效")
            except TokenInvalidError as e:
                results.append(f"[失效] {cred.name}:{e}")
            except DashenAPIError as e:
                results.append(f"[异常] {cred.name}:{e}")
        return results
