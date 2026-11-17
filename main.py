"""AstrBot 插件:燕云十六声角色查询(datamsapi 实测协议版)。

数据源:网易大神 datamsapi.ds.163.com/v1/h72roletool/*(详见 api.py 顶部说明)。
凭据获取:tools/get_token.js(浏览器控制台,见 README)。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.utils.session_waiter import SessionController, session_waiter

from .api import DashenAPIError, DashenClient, TokenInvalidError
from .attrs_image import render_advisor_image, render_combined_image
from .build_advisor import build_advisor, make_ai_prompt, render_text as render_advisor_text, rule_summary
from .render import _render_attrs, render_generic, render_pvp, render_proxy_role, render_role_info
from .storage import BindingStore
from .yanyun_login import LoginService, ensure_browser_ready

try:  # AstrBot >= 4.27 提供的 web 辅助层
    from astrbot.api.web import error_response, json_response, request

    WEB_API_OK = True
except ImportError:
    WEB_API_OK = False

PLUGIN_NAME = "astrbot_plugin_yanyun_query"

HELP_TEXT = """【燕云十六声查询】指令列表
- 燕云登录(私聊):扫码登录后选择要绑定的角色 —— 推荐方式(旧名「燕云扫码」仍可用)
- 燕云绑定 <角色名>:把你自己的角色绑给这个 QQ(支持模糊匹配,重名会让你选)
- 燕云解绑:解除绑定
- 燕云我的:查询自己绑定的角色档案(角色名片图 + 探索度进度条)
- 燕云查询 <角色名>:查询凭据池内的角色(同「燕云我的」卡片)
- 燕云详情 <角色名>:家园日常 + 面板属性 + 区域收集(属性一览以图片发送,失败自动回退文本)
- 燕云面板 <角色名>:调号助手数据(属性面板/流派/全服基准对比)
- 燕云分析 <角色名>:流派养成对标报告(词条/战力对标 + 带部位的洗练方向,图片卡)
- 燕云战绩 <角色名>:PVP 数据
- 燕云状态:凭据自检(仅管理员)
- 燕云帮助:本说明

凭据获取:私聊发「燕云登录」自助完成;管理员也可用 WebUI 面板或按 README 手动添加。
自然语言:机器人配置了大模型后,直接说"看看我的燕云角色""查探索度"等也能触发查询。
⚠ 调用的是网易非公开接口,token 年度有效,仅供学习交流。"""


@register("astrbot_plugin_yanyun_query", "林迟羽", "燕云十六声角色查询(角色档案/探索度/流派配置/面板属性)", "v1.0.0")
class YanyunQueryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = BindingStore(Path(StarTools.get_data_dir(PLUGIN_NAME)))
        self._build_client()
        self.login_svc = LoginService(
            server=config.get("server") or "1",
            on_success=self._on_token_issued,
            on_roles=self._fetch_roles,
            mirror=str(config.get("browser_download_mirror") or ""),
            auto_install=bool(config.get("auto_install_browser", True)),
        )

    def _build_client(self):
        self.client = DashenClient(
            accounts=self.config.get("dashen_accounts") or [],
            server=self.config.get("server") or "1",
            timeout=int(self.config.get("timeout") or 15),
            cooldown_seconds=int(self.config.get("cooldown_seconds") or 3600),
            uid_cookies=self._load_uid_cookies(),
        )
        logger.info(f"[燕云查询] 已加载,凭据数量:{len(self.client._pool)}")

    def _cookies_path(self) -> Path:
        return Path(StarTools.get_data_dir(PLUGIN_NAME)) / "uid_cookies.json"

    def _load_uid_cookies(self) -> dict[str, list[dict]]:
        """uid → 全量 cookie 列表(扫码登录保存,用于 inf/inf-act 登录态接口)。"""
        p = self._cookies_path()
        if p.exists():
            try:
                return json.loads(p.read_text("utf-8"))
            except Exception:
                return {}
        return {}

    def _save_uid_cookie(self, cookies: list[dict]) -> str:
        """保存扫码 cookie,返回 uid。"""
        jar = {c.get("name"): c.get("value") for c in cookies if c.get("name")}
        uid = jar.get("GOD_UUID", "")
        if not uid:
            return ""
        store = self._load_uid_cookies()
        store[uid] = cookies
        p = self._cookies_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(store, ensure_ascii=False, indent=1), "utf-8")
        return uid

    async def initialize(self):
        """注册 WebUI 面板后端 API(需要 AstrBot >= 4.27.5),并清理失效绑定。"""
        if not WEB_API_OK:
            logger.warning("[燕云查询] 当前 AstrBot 版本缺少 astrbot.api.web,面板功能不可用,请升级到 4.27.5+")
        else:
            await self._register_web_apis()

        # 凭据被删除/替换后,清掉那些绑到「已经没有凭据的角色」上的记录
        await self._prune_orphan_bindings()

        # 老配置里没有角色昵称,后台补一次,否则按名字查不到
        asyncio.create_task(self._backfill_nicks())

        # 首次安装自愈:后台预装浏览器,避免用户首次扫码干等
        if self.config.get("preinstall_browser", True):
            asyncio.create_task(self._prewarm_browser())

    async def _prune_orphan_bindings(self):
        """剔除绑到「不在凭据池里的角色」的绑定,避免用户查到一半才发现凭据没了。"""
        try:
            keep = self.client._pool.role_ids()
            removed = await self.store.prune_identities(keep)
            if removed:
                logger.info(
                    f"[燕云查询] 已清理 {len(removed)} 条失效绑定(对应角色已无凭据):{', '.join(removed)}"
                )
        except Exception as e:
            logger.warning(f"[燕云查询] 清理失效绑定失败: {e}")

    async def _register_web_apis(self):
        try:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/login/start", self._api_login_start, ["POST"], "开始扫码登录并签发 token"
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/login/status", self._api_login_status, ["GET"], "查询登录/签发状态"
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/login/issue", self._api_login_issue, ["POST"], "选择角色并签发 token"
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/login/cancel", self._api_login_cancel, ["POST"], "取消扫码登录"
            )
            logger.info("[燕云查询] 面板 API 已注册")
        except Exception as e:
            logger.warning(f"[燕云查询] 注册面板 API 失败(功能不可用): {e}")

    async def _prewarm_browser(self):
        """后台检查/安装无头浏览器(失败只记日志,不影响查询功能)。"""
        try:
            ok, msg, _ = await ensure_browser_ready(
                mirror=str(self.config.get("browser_download_mirror") or ""),
                auto_install=bool(self.config.get("auto_install_browser", True)),
            )
            if ok:
                logger.info(f"[燕云查询] 浏览器环境就绪:{msg}")
            else:
                logger.warning(f"[燕云查询] 浏览器环境未就绪(扫码/面板功能不可用):{msg}")
        except Exception as e:
            logger.warning(f"[燕云查询] 浏览器预装检查异常:{e}")

    async def terminate(self):
        await self.login_svc.close()
        await self.client.close()

    # ------------------------------------------------------------------ 面板 API

    async def _api_login_start(self) -> Any:
        body = await request.json(default={}) or {}
        raw = str(body.get("role_id") or "").strip()
        role_id = ""
        if raw:
            if raw.isdigit():
                role_id = raw
            else:
                hits = self.client._pool.find_by_keyword(raw)
                if not hits:
                    return json_response({
                        "status": self.login_svc.status(),
                        "started": False,
                        "message": f"没找到叫「{raw}」的角色。留空扫码,登录后再选就行。",
                    })
                if len(hits) > 1:
                    names = "、".join(self._role_label(c) for c in hits)
                    return json_response({
                        "status": self.login_svc.status(),
                        "started": False,
                        "message": f"「{raw}」匹配到多个角色({names})。留空扫码,登录后再选吧。",
                    })
                role_id = hits[0].role_id
        # 留空表示扫码后列出角色再选
        result = await self.login_svc.start(role_id or None)
        return json_response({"status": self.login_svc.status(), **result})

    async def _api_login_status(self) -> Any:
        return json_response(self.login_svc.status())

    async def _api_login_issue(self) -> Any:
        body = await request.json(default={}) or {}
        role_id = str(body.get("role_id") or "").strip()
        result = await self.login_svc.issue_for(role_id)
        return json_response({"status": self.login_svc.status(), **result})

    async def _api_login_cancel(self) -> Any:
        result = await self.login_svc.cancel()
        return json_response({"status": self.login_svc.status(), **result})

    # ------------------------------------------------------------------ 自然语言(LLM 工具)

    async def _resolve_role_for_llm(self, event: AstrMessageEvent, role_id: str) -> tuple[str | None, str]:
        role_id = (role_id or "").strip()
        explicit = bool(role_id)
        if not role_id:
            bind = await self.store.get(event.get_sender_id())
            if not bind:
                return None, self._login_hint(event)
            role_id = str(bind.get("identity") or "").strip()
        if not role_id:
            return None, self._login_hint(event)
        if explicit:
            resolved, denied = await self._ensure_queryable(event, role_id)
            if denied:
                return None, denied
            role_id = resolved or role_id
        if self.client._pool.by_role(role_id) is None:
            logger.info(f"[燕云查询] 角色 {role_id} 没有可用凭据,引导用户重新登录")
            return None, self._login_hint(event, role_id)
        return role_id, ""

    @staticmethod
    def _login_hint(event: AstrMessageEvent, role_id: str = "") -> str:
        """给 LLM 的软提示:让它用自己的口吻劝用户去登录,而不是复述技术报错。"""
        where = "先私聊找我发「燕云登录」" if event.get_group_id() else "先发「燕云登录」"
        reason = f"(角色 {role_id} 还没登录过)" if role_id else "(还没绑定角色)"
        return (
            f"【情况】这个用户查不到数据{reason}。\n"
            f"【请这样回】用你平时聊天的口吻,简短跟对方说一句:{where}登录绑定一下,再回来查。"
            "别提「凭据」「接口」「系统」「错误码」这类词,也别长篇解释或道歉套话。"
        )

    @staticmethod
    def _role_label(cred) -> str:
        """给用户看的角色标识:昵称优先,没有昵称退回配置名。"""
        if cred is None:
            return "该角色"
        return (getattr(cred, "nick", "") or getattr(cred, "name", "") or "").strip() or "该角色"

    async def _choose_role(self, event: AstrMessageEvent, keyword: str, purpose: str):
        """把用户说的角色名(或编号)解析成唯一角色。

        返回 (cred, 错误文案);错误文案非空表示不能继续。
        同一个名字匹配到多个角色时,列出候选让用户回复序号。
        """
        text_in = (keyword or "").strip().strip("<>【】").strip()
        if not text_in:
            bind = await self.store.get(event.get_sender_id())
            own = str((bind or {}).get("identity") or "").strip()
            cred = self.client._pool.by_role(own) if own else None
            if cred is None:
                return None, "你还没绑定角色。先私聊发「燕云登录」扫码登录,把自己绑上。"
            return cred, ""
        if text_in.isdigit():
            cred = self.client._pool.by_role(text_in)
            if cred is None:
                return None, "这个角色还没有登录凭据。先私聊发「燕云登录」把你自己的角色加进来。"
            return cred, ""
        hits = self.client._pool.find_by_keyword(text_in)
        if not hits:
            return None, (
                f"没找到叫「{text_in}」的角色。我只能查到我自己账号下的角色,查不了别人的。\n"
                "如果这是你自己的号,先私聊发「燕云登录」扫码登录,把角色加进来。"
            )
        if len(hits) == 1:
            return hits[0], ""
        lines = [f"和「{text_in}」相关的角色有 {len(hits)} 个,你要{purpose}哪个?回复序号:"]
        for i, cred in enumerate(hits, 1):
            lines.append(f"{i}. {self._role_label(cred)}")
        await event.send(event.plain_result("\n".join(lines)))
        picked: list[int] = []

        @session_waiter(timeout=60, record_history_chains=False)
        async def waiter(controller: SessionController, ev: AstrMessageEvent):
            raw = ev.message_str.strip()
            if not raw.isdigit():
                return
            idx = int(raw)
            if 1 <= idx <= len(hits):
                picked.append(idx)
                controller.stop()

        try:
            await waiter(event)
        except TimeoutError:
            return None, "等太久没选,先不处理了。要的话重新发一次。"
        except Exception as e:
            logger.error(f"[燕云查询] 选择角色失败: {e}")
            return None, "选角色的时候出了点问题,重发一次试试。"
        if not picked:
            return None, ""
        return hits[picked[0] - 1], ""

    async def _ensure_queryable(self, event: AstrMessageEvent, keyword: str) -> tuple[str, str]:
        """查询前的统一校验:解析角色名 → 确认是本人绑定的角色。返回 (role_id, 错误文案)。"""
        cred, err = await self._choose_role(event, keyword, "查询")
        if err or cred is None:
            return "", err
        bind = await self.store.get(event.get_sender_id())
        own = str((bind or {}).get("identity") or "").strip()
        if not own:
            return "", "你还没绑定角色。先私聊发「燕云登录」扫码登录,把自己绑上再来查。"
        if cred.role_id != own:
            mine = self.client._pool.by_role(own)
            mine_label = self._role_label(mine) if mine else "你绑定的角色"
            return "", (
                f"「{self._role_label(cred)}」不是你绑定的角色。\n"
                f"你绑定的是「{mine_label}」,只能查自己的角色。"
            )
        return cred.role_id, ""

    async def _backfill_nicks(self) -> None:
        """给没有昵称的老凭据补上昵称,这样用户能按角色名查询。"""
        accounts = list(self.config.get("dashen_accounts") or [])
        changed = False
        for acc in accounts:
            if str(acc.get("nick") or "").strip():
                continue
            rid = str(acc.get("role_id") or "").strip()
            if not rid or self.client._pool.by_role(rid) is None:
                continue
            try:
                resp = await self.client.query_proxy_role(rid)
                data = resp.get("data") or {}
                nick = str(data.get("base.nickname") or "").strip()
            except Exception as e:
                logger.warning(f"[燕云查询] 补角色昵称失败({rid}): {e}")
                continue
            if nick:
                acc["nick"] = nick
                changed = True
                logger.info(f"[燕云查询] 补到角色昵称:{nick}")
        if changed:
            self.config["dashen_accounts"] = accounts
            self.config.save_config()
            self._build_client()
            logger.info("[燕云查询] 角色昵称已写回配置")

    DELIVERED_NOTICE = "[已向用户发送图卡,数据已完整展示给用户,本轮不需要再用文字复述]"

    @staticmethod
    def _stop_after_delivery(event: AstrMessageEvent) -> str:
        """图卡/文本已在工具内直接发送给用户时调用。

        把本轮 Agent 标记为停止,避免大模型再拿工具返回值写一段总结。
        注意:返回值不能是空字符串 —— 模型会把「调用成功但返回为空」误判为
        数据拉取失败(表现为后续回复「数据拉不到」),所以这里返回一句明确说明。
        """
        event.set_extra("agent_stop_requested", True)
        return YanyunQueryPlugin.DELIVERED_NOTICE

    @filter.llm_tool(name="yanyun_query_profile")
    async def yanyun_query_profile(self, event: AstrMessageEvent, role_id: str = "") -> str:
        """查询燕云十六声游戏角色的完整档案,包括等级、门派、功力、成就、外观收集、各区域探索度、区域收集(宝箱/蹊跷数量)、流派配置(套装、功法、心法、装备、奇术)。当用户想查看燕云十六声的角色数据、角色信息、探索度、流派、装备、心法等内容时调用此工具。
        Args:
            role_id(string): 要查询的燕云角色名。如果用户没有明确指定角色,请传空字符串,将自动查询该用户绑定的角色
        """
        role_id, err = await self._resolve_role_for_llm(event, role_id)
        if err:
            return err
        try:
            resp_info, resp_proxy = await asyncio.gather(
                self.client.query_role_info(role_id),
                self.client.query_proxy_role(role_id),
            )
        except TokenInvalidError as e:
            return f"[燕云查询] {e}"
        except DashenAPIError as e:
            return f"[燕云查询] {e}"
        except Exception as e:
            logger.error(f"[燕云查询] 内部错误: {e}")
            return f"[燕云查询] 内部错误:{type(e).__name__}"
        # 档案卡主动发给用户(图片优先,失败回退文本卡片)
        img_path = await self._render_profile_image_file(resp_proxy.get("data") or {}, resp_info.get("data") or {})
        if img_path is not None:
            try:
                await event.send(event.chain_result([Comp.Image.fromFileSystem(str(img_path))]))
            except Exception as e:
                logger.warning(f"[燕云图片] 档案卡图片发送失败,回退文本: {e}")
                img_path = None
        blocks = render_proxy_role(resp_proxy, f"燕云角色档案 · {role_id}",
                                   info_data=resp_info.get("data") or {}, as_blocks=True)
        if img_path is None:
            await self._deliver_to_user(event, blocks)
        # 档案卡已直接送达用户,不再回灌数据给 LLM,避免追加总结
        return self._stop_after_delivery(event)

    @filter.llm_tool(name="yanyun_query_detail")
    async def yanyun_query_detail(self, event: AstrMessageEvent, role_id: str = "") -> str:
        """查询燕云十六声角色的家园信息、突破记录、功法日常成绩、游戏面板最终属性(气血/外功攻击/防御/会心率等)和区域收集明细。当用户想查看燕云角色的面板属性、战斗力数值、家园、突破、收集进度明细时调用此工具。
        Args:
            role_id(string): 要查询的燕云角色名。如果用户没有明确指定角色,请传空字符串,将自动查询该用户绑定的角色
        """
        role_id, err = await self._resolve_role_for_llm(event, role_id)
        if err:
            return err
        try:
            resp_info, resp_proxy = await asyncio.gather(
                self.client.query_role_info(role_id),
                self.client.query_proxy_role(role_id),
            )
            blocks = render_role_info(resp_info, f"燕云家园与日常 · {role_id}", as_blocks=True)
            attrs_data = resp_proxy.get("data") or {}
            attrs = _render_attrs(attrs_data, as_blocks=True)
            attrs_text = "\n".join(attrs) if attrs else ""
            img_path = await self._render_profile_image_file(attrs_data, resp_info.get("data") or {})
            if img_path is not None:
                try:
                    await event.send(event.chain_result([Comp.Image.fromFileSystem(str(img_path))]))
                except Exception as e:
                    logger.warning(f"[燕云图片] 图片发送失败,回退文本: {e}")
                    img_path = None
            if img_path is None:
                if attrs_text:
                    blocks.append(attrs_text)
                await self._deliver_to_user(event, blocks)
            # 图卡已直接送达用户,不再回灌数据给 LLM,避免追加总结
            return self._stop_after_delivery(event)
        except DashenAPIError as e:
            return f"[燕云查询] {e}"
        except Exception as e:
            logger.error(f"[燕云查询] 内部错误: {e}")
            return f"[燕云查询] 内部错误:{type(e).__name__}"

    @filter.llm_tool(name="yanyun_query_pvp")
    async def yanyun_query_pvp(self, event: AstrMessageEvent, role_id: str = "") -> str:
        """查询燕云十六声角色的 PVP 对战数据类型列表。当用户想查看燕云角色的 PVP 战绩、对战数据时调用此工具。
        Args:
            role_id(string): 要查询的燕云角色名。如果用户没有明确指定角色,请传空字符串,将自动查询该用户绑定的角色
        """
        role_id, err = await self._resolve_role_for_llm(event, role_id)
        if err:
            return err
        blocks = await self._fetch_blocks(role_id, "pvp", f"燕云PVP · {role_id}")
        await self._deliver_to_user(event, blocks)
        # PVP 图卡已直接送达用户,不再回灌数据给 LLM,避免追加总结
        return self._stop_after_delivery(event)

    @filter.llm_tool(name="yanyun_analyze_build")
    async def yanyun_analyze_build(self, event: AstrMessageEvent, role_id: str = "") -> str:
        """获取燕云十六声角色的养成对标报告:词条数量对标(带部位位置)、战力模块对标、养成方向、属性面板总值、全服基准对比。当用户想了解角色强度评价、培养方向、词条洗练建议、养成分析,或请求"调号/AI调号"类分析时调用此工具。对标报告卡片会主动发给用户;你只需基于返回的数据写一段简短的养成推荐回复,不要编造数据。
        Args:
            role_id(string): 要分析的燕云角色名。如果用户没有明确指定角色,请传空字符串,将自动使用该用户绑定的角色
        """
        role_id, err = await self._resolve_role_for_llm(event, role_id)
        if err:
            return err
        try:
            resp_info, resp_proxy = await asyncio.gather(
                self.client.query_role_info(role_id),
                self.client.query_proxy_role(role_id),
            )
            report = build_advisor(resp_proxy.get("data") or {}, resp_info.get("data") or {})
        except TokenInvalidError as e:
            return f"[燕云分析] {e}"
        except DashenAPIError as e:
            return f"[燕云分析] {e}"
        except Exception as e:
            logger.error(f"[燕云查询] 内部错误: {e}")
            return f"[燕云查询] 内部错误:{type(e).__name__}"
        if report["liupai"] == "未知流派":
            return f"[燕云分析] 角色 {role_id} 的主副功法组合暂未收录流派映射,无法生成对标报告。"
        # 对标报告卡片主动发给用户(图片优先,失败回退文本卡片)
        img_path = await self._render_advisor_image_file(report)
        if img_path is not None:
            try:
                await event.send(event.chain_result([Comp.Image.fromFileSystem(str(img_path))]))
            except Exception as e:
                logger.warning(f"[燕云图片] 养成分析图片发送失败,回退文本: {e}")
                img_path = None
        if img_path is None:
            await self._deliver_to_user(event, render_advisor_text(report))
        # 面板/全服基准数据包(cookie 可用时提供,失败不影响对标报告)
        panel_text = "(属性面板/全服基准数据不可用,缺少登录 cookie)"
        try:
            panel, bench = await asyncio.gather(
                self.client.query_role_data(role_id),
                self.client.query_benchmark(role_id),
            )
            panel_text = _render_panel_v2(panel.get("result") or {}, bench)
        except (DashenAPIError, TokenInvalidError) as e:
            logger.info(f"[燕云分析] 面板/基准数据跳过: {e}")
        except Exception as e:
            logger.error(f"[燕云查询] 内部错误: {e}")
        advisor_text = "\n".join(render_advisor_text(report))
        # 关键段优先:分析素材与面板基准完整保留,超长时只截与图片重复的文本报告
        MAX_CHARS = 5000
        panel_block = f"\n\n―― 属性面板与全服基准 ――\n{panel_text}"
        prompt_block = f"\n\n―― 你的分析素材(报告已发用户) ――\n{make_ai_prompt(report)}"
        fixed_len = len(panel_block) + len(prompt_block)
        if fixed_len >= MAX_CHARS:
            panel_block = panel_block[: max(0, MAX_CHARS - len(prompt_block))]
            payload = panel_block + prompt_block
        else:
            payload = advisor_text[: MAX_CHARS - fixed_len] + panel_block + prompt_block
        logger.info(
            f"[燕云分析] 素材 {len(payload)} 字符(advisor={len(advisor_text)} "
            f"panel={len(panel_block)} prompt={len(prompt_block)} 上限={MAX_CHARS})"
        )
        return payload

    @filter.command("燕云分析", alias={"yy分析"})
    async def yy_analyze(self, event: AstrMessageEvent, identity: str = ""):
        identity, err = await self._ensure_queryable(event, identity)
        if err:
            yield event.plain_result(err)
            return
        try:
            resp_info, resp_proxy = await asyncio.gather(
                self.client.query_role_info(identity),
                self.client.query_proxy_role(identity),
            )
            report = build_advisor(resp_proxy.get("data") or {}, resp_info.get("data") or {})
        except DashenAPIError as e:
            yield event.plain_result(f"[燕云分析] {e}")
            return
        except Exception as e:
            logger.error(f"[燕云查询] 内部错误: {e}")
            yield event.plain_result(f"[燕云分析] 内部错误:{type(e).__name__}")
            return
        if report["liupai"] == "未知流派":
            yield event.plain_result(
                f"[燕云分析] 主功法 {report['kongfu_main']} · 副功法 {report['kongfu_sub']} 的组合暂未收录流派映射,无法对标。")
            return
        img_path = await self._render_advisor_image_file(report)
        if img_path is not None:
            yield event.chain_result([Comp.Image.fromFileSystem(str(img_path))])
            yield event.plain_result(f"―― 小结 ――\n{rule_summary(report)}")
        else:
            async for r in self._send_blocks(event, render_advisor_text(report)):
                yield r

    @filter.command("燕云面板", alias={"yy面板"})
    async def yy_panel(self, event: AstrMessageEvent, identity: str = ""):
        identity, err = await self._ensure_queryable(event, identity)
        if err:
            yield event.plain_result(err)
            return
        try:
            panel, bench = await asyncio.gather(
                self.client.query_role_data(identity),
                self.client.query_benchmark(identity),
            )
        except DashenAPIError as e:
            yield event.plain_result(f"[燕云面板] {e}")
            return
        except Exception as e:
            logger.error(f"[燕云查询] 内部错误: {e}")
            yield event.plain_result(f"[燕云面板] 内部错误:{type(e).__name__}")
            return
        blocks = [b for b in _render_panel_v2(panel.get("result") or {}, bench).split("\n\n") if b.strip()]
        async for r in self._send_blocks(event, blocks):
            yield r

    async def _on_token_issued(self, result: dict, cookies: list[dict] | None = None):
        """签发成功回调:cookie 落盘 + 写入 dashen_accounts + 重建客户端。"""
        uid = ""
        if cookies:
            uid = self._save_uid_cookie(cookies)
        accounts = list(self.config.get("dashen_accounts") or [])
        matched = False
        for acc in accounts:
            if str(acc.get("role_id") or "").strip() == result["role_id"]:
                acc["token"] = result["token"]
                acc["dts"] = result["dts"]
                if uid:
                    acc["uid"] = uid
                if result.get("nick"):
                    acc["nick"] = result["nick"]
                acc["enabled"] = True
                matched = True
                break
        if not matched:
            accounts.append({
                "name": result.get("nick") or f"角色{result['role_id']}",
                "nick": result.get("nick") or "",
                "role_id": result["role_id"],
                "token": result["token"],
                "dts": result["dts"],
                "uid": uid,
                "enabled": True,
            })
        self.config["dashen_accounts"] = accounts
        self.config.save_config()
        self._build_client()
        logger.info(f"[燕云查询] 凭据已写入配置(角色 {result['role_id']},uid {uid or '?'}),客户端已刷新")

    async def _fetch_roles(self, cookies: list[dict]) -> list[dict]:
        """扫码登录后读取该账号下的燕云角色(用于让用户选择绑定哪一个)。"""
        return await self.client.query_bind_list(cookies)

    # ------------------------------------------------------------------ 帮助

    @filter.command("燕云帮助", alias={"yy帮助"})
    async def yy_help(self, event: AstrMessageEvent):
        yield event.plain_result(HELP_TEXT)

    # ------------------------------------------------------------------ 绑定

    @filter.command("燕云绑定", alias={"yy绑定"})
    async def yy_bind(self, event: AstrMessageEvent, identity: str = ""):
        if not identity:
            yield event.plain_result("要绑定哪个角色?把角色名回给我就行(例如:燕云绑定 林迟羽):")
            identity = ""

            @session_waiter(timeout=60, record_history_chains=False)
            async def waiter(controller: SessionController, ev: AstrMessageEvent):
                nonlocal identity
                text = ev.message_str.strip()
                if not text:
                    return  # 空消息安静忽略,不刷提示
                identity = text
                controller.stop()

            try:
                await waiter(event)
            except TimeoutError:
                yield event.plain_result("绑定超时,下次直接发:燕云绑定 <角色名>")
                return
            except Exception as e:
                logger.error(f"[燕云查询] 绑定交互异常: {e}")
                return
            if not identity:
                return

        async for r in self._do_bind(event, identity):
            yield r

    @filter.regex(r"^(?:燕云绑定|yy绑定)(?![ \t])\S+")
    async def yy_bind_loose(self, event: AstrMessageEvent):
        """兜底:兼容「燕云绑定林迟羽」「燕云绑定<林迟羽>」等不带空格的写法。"""
        text = event.get_message_str().strip()
        logger.info(f"[燕云查询] 兜底匹配绑定指令:{text!r}")
        async for r in self._do_bind(event, text):
            yield r

    async def _do_bind(self, event: AstrMessageEvent, raw: str):
        """绑定:按角色名找到角色 → 记下绑定 → 校验登录凭据,失效才走扫码。"""
        keyword = re.sub(r"^(?:燕云绑定|yy绑定)", "", (raw or "").strip()).strip()
        keyword = keyword.strip("<>【】").strip()
        cred, err = await self._choose_role(event, keyword, "绑定")
        if err:
            yield event.plain_result(err)
            return
        if cred is None:
            return

        rid = cred.role_id
        label = self._role_label(cred)
        user_id = event.get_sender_id()
        is_group = bool(event.get_group_id())
        await self.store.set(user_id, rid, {"nick": cred.nick})

        yield event.plain_result(f"「{label}」已有登录凭据,正在校验是否有效…")
        ok, reason = await self.client.verify_credential(rid)
        if ok:
            yield event.plain_result(f"绑定好了~ 以后查的就是「{label}」了")
            return
        if reason != "invalid":
            yield event.plain_result(
                f"已经记下「{label}」的绑定,不过凭据自检出了点问题,先按可用处理。\n"
                "如果查询报错,发「燕云登录」重新登录一下就行。"
            )
            return
        if is_group:
            yield event.plain_result(
                f"「{label}」的登录过期了。群聊里没法扫码,私聊我发「燕云登录」重新登录一下就行。"
            )
            return

        yield event.plain_result(f"「{label}」的登录过期了,正在为你重新扫码登录…")
        started = await self.login_svc.start(rid)
        if not started.get("started"):
            yield event.plain_result(
                f"自动扫码启动失败:{started.get('message')}\n直接私聊发「燕云登录」扫码也行。"
            )
            return
        qr_path = Path(StarTools.get_data_dir(PLUGIN_NAME)) / "login_qr_tmp.png"
        for _ in range(100):
            await asyncio.sleep(1.5)
            st = self.login_svc.status()
            if st["qr_b64"]:
                qr_path.write_bytes(base64.b64decode(st["qr_b64"]))
                yield event.chain_result([
                    Comp.Plain("请用手机「大神 App」→ 首页右上角扫一扫(约2分钟有效),扫完自动完成绑定。"),
                    Comp.Image.fromFileSystem(str(qr_path)),
                ])
                break
            if st["state"] in ("failed", "timeout"):
                yield event.plain_result(f"扫码流程失败:{st['message']}")
                return
        else:
            st = self.login_svc.status()
            yield event.plain_result(
                f"二维码生成超时({st.get('message', '')}),稍后重新发「燕云绑定 <角色名>」"
            )
            return
        asyncio.create_task(self._scan_notify(event.unified_msg_origin, user_id))

    @filter.command("燕云解绑", alias={"yy解绑"})
    async def yy_unbind(self, event: AstrMessageEvent):
        ok = await self.store.remove(event.get_sender_id())
        yield event.plain_result("已解绑。" if ok else "你还没有绑定角色,先发「燕云绑定 <角色名>」")

    # ------------------------------------------------------------------ 查询

    def _resolve_role(self, identity: str, bind: dict | None) -> tuple[str | None, str | None]:
        """返回 (roleId, 错误提示)。"""
        if identity.strip():
            return identity.strip(), None
        if not bind:
            return None, "你还没有绑定角色,先私聊发「燕云登录」扫码绑定。"
        return bind.get("identity"), None

    async def _fetch_rendered(self, role_id: str, kind: str, title: str) -> str:
        return "\n\n".join(await self._fetch_blocks(role_id, kind, title))

    def _save_attrs_png(self, png: bytes, prefix: str = "attrs") -> Path | None:
        """卡片图落盘到插件数据目录 images/,按前缀分类,各保留最新 10 张。"""
        try:
            d = Path(StarTools.get_data_dir(PLUGIN_NAME)) / "images"
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{prefix}_{int(time.time() * 1000)}.png"
            path.write_bytes(png)
            for old in sorted(d.glob(f"{prefix}_*.png"))[:-10]:
                try:
                    old.unlink()
                except OSError:
                    pass
            return path
        except Exception as e:
            logger.warning(f"[燕云图片] 图片落盘失败: {e}")
            return None

    async def _render_profile_image_file(self, proxy_data: dict, info_data: dict) -> Path | None:
        """渲染「总览」合并卡(受 profile_image 配置开关控制);失败/关闭返回 None。"""
        if not bool(self.config.get("profile_image", True)):
            return None
        try:
            png = await render_combined_image(
                proxy_data, info_data,
                mirror=str(self.config.get("browser_download_mirror") or ""),
                auto_install=bool(self.config.get("auto_install_browser", True)),
            )
        except Exception as e:
            logger.warning(f"[燕云图片] 档案卡渲染异常,回退文本: {type(e).__name__}: {e}")
            return None
        if not png:
            return None
        return self._save_attrs_png(png, prefix="advisor")

    async def _render_advisor_image_file(self, report: dict) -> Path | None:
        """渲染养成分析图片(受 advisor_image 配置开关控制);失败/关闭返回 None。"""
        if not bool(self.config.get("advisor_image", True)):
            return None
        try:
            png = await render_advisor_image(
                report,
                mirror=str(self.config.get("browser_download_mirror") or ""),
                auto_install=bool(self.config.get("auto_install_browser", True)),
            )
        except Exception as e:
            logger.warning(f"[燕云图片] 养成分析渲染异常,回退文本: {type(e).__name__}: {e}")
            return None
        if not png:
            return None
        return self._save_attrs_png(png)

    async def _send_blocks(self, event: AstrMessageEvent, blocks: list[str]):
        """发送文本块:QQ(aiocqhttp)用合并转发卡片,其他平台纯文本。

        仅作为图片卡发送失败时的兜底形式。"""
        norm = []
        for b in blocks:
            if isinstance(b, str):
                if b.strip():
                    norm.append(b)
            elif isinstance(b, list):  # 防御:嵌套列表展平
                norm.extend(str(x) for x in b if str(x).strip())
        blocks = norm
        if not blocks:
            return
        try:
            if event.get_platform_name() == "aiocqhttp" and len(blocks) > 1:
                try:
                    self_id = str(event.message_obj.self_id)
                except Exception:
                    self_id = "10000"
                nodes = [
                    Comp.Node(content=[Comp.Plain(b)], name="燕云查询", uin=self_id)
                    for b in blocks
                ]
                yield event.chain_result([Comp.Nodes(nodes=nodes)])
                return
        except Exception as e:
            logger.warning(f"[燕云查询] 转发卡片发送失败,回退纯文本: {e}")
        for b in blocks:
            yield event.plain_result(b)

    async def _deliver_to_user(self, event: AstrMessageEvent, blocks: list[str]):
        """把完整内容主动发给用户:QQ 平台多块发合并转发卡片,单块发普通消息。

        仅作为图片卡发送失败时的兜底形式。非 QQ 平台不发(LLM 回复会自然送达);
        失败只记日志,不影响 LLM。
        """
        blocks = [b for b in blocks if b and b.strip()]
        if not blocks:
            return
        try:
            if event.get_platform_name() != "aiocqhttp":
                return
            self_id = str(getattr(event.message_obj, "self_id", "10000") or "10000")
            if len(blocks) > 1:
                nodes = [
                    Comp.Node(content=[Comp.Plain(b)], name="燕云查询", uin=self_id)
                    for b in blocks
                ]
                await event.send(event.chain_result([Comp.Nodes(nodes=nodes)]))
            else:
                await event.send(event.chain_result([Comp.Plain(blocks[0])]))
        except Exception as e:
            logger.warning(f"[燕云查询] 主动发送卡片失败(不影响 LLM): {e}")

    async def _fetch_blocks(self, role_id: str, kind: str, title: str) -> list[str]:
        try:
            if kind == "proxy":
                # 档案 + 区域收集:两个接口并发
                resp_proxy, resp_info = await asyncio.gather(
                    self.client.query_proxy_role(role_id),
                    self.client.query_role_info(role_id),
                )
                return render_proxy_role(resp_proxy, title, info_data=resp_info.get("data") or {}, as_blocks=True)
            if kind == "info":
                resp = await self.client.query_role_info(role_id)
                return render_role_info(resp, title, as_blocks=True)
            if kind == "pvp":
                resp = await self.client.query_pvp(role_id)
                return [render_pvp(resp, title)]
            resp = await self.client.query_proxy_role(role_id)
            return [render_generic(resp, title)]
        except TokenInvalidError as e:
            return [f"[燕云查询] {e}"]
        except DashenAPIError as e:
            return [f"[燕云查询] {e}"]
        except Exception as e:
            logger.error(f"[燕云查询] 未预期异常: {e}")
            return [f"[燕云查询] 内部错误:{type(e).__name__}"]

    async def _send_profile(self, event: AstrMessageEvent, role_id: str):
        """「燕云我的/燕云查询」主流程:档案卡图片优先,失败回退文本卡片。"""
        try:
            resp_info, resp_proxy = await asyncio.gather(
                self.client.query_role_info(role_id),
                self.client.query_proxy_role(role_id),
            )
        except TokenInvalidError as e:
            yield event.plain_result(f"[燕云查询] {e}")
            return
        except DashenAPIError as e:
            yield event.plain_result(f"[燕云查询] {e}")
            return
        except Exception as e:
            logger.error(f"[燕云查询] 内部错误: {e}")
            yield event.plain_result(f"[燕云查询] 内部错误:{type(e).__name__}")
            return
        img_path = await self._render_profile_image_file(resp_proxy.get("data") or {}, resp_info.get("data") or {})
        if img_path is not None:
            yield event.chain_result([Comp.Image.fromFileSystem(str(img_path))])
            return
        blocks = render_proxy_role(resp_proxy, f"燕云角色档案 · {role_id}",
                                   info_data=resp_info.get("data") or {}, as_blocks=True)
        async for r in self._send_blocks(event, blocks):
            yield r

    @filter.command("燕云我的", alias={"yy我的"})
    async def yy_mine(self, event: AstrMessageEvent):
        bind = await self.store.get(event.get_sender_id())
        role_id, err = self._resolve_role("", bind)
        if err:
            yield event.plain_result(err)
            return
        async for r in self._send_profile(event, role_id):
            yield r

    @filter.command("燕云查询", alias={"yy查询"})
    async def yy_query(self, event: AstrMessageEvent, identity: str = ""):
        identity, err = await self._ensure_queryable(event, identity)
        if err:
            yield event.plain_result(err)
            return
        role_id = identity
        async for r in self._send_profile(event, role_id):
            yield r

    @filter.command("燕云详情", alias={"yy详情"})
    async def yy_detail(self, event: AstrMessageEvent, identity: str = ""):
        identity, err = await self._ensure_queryable(event, identity)
        if err:
            yield event.plain_result(err)
            return
        try:
            resp_info, resp_proxy = await asyncio.gather(
                self.client.query_role_info(identity),
                self.client.query_proxy_role(identity),
            )
            attrs_data = resp_proxy.get("data") or {}
            img_path = await self._render_profile_image_file(attrs_data, resp_info.get("data") or {})
            if img_path is None:
                blocks = render_role_info(resp_info, f"燕云家园与日常 · {identity}", as_blocks=True)
                attrs = _render_attrs(attrs_data, as_blocks=True)
                if attrs:
                    blocks.append(attrs)
                async for r in self._send_blocks(event, blocks):
                    yield r
            if img_path is not None:
                yield event.chain_result([Comp.Image.fromFileSystem(str(img_path))])
        except DashenAPIError as e:
            yield event.plain_result(f"[燕云查询] {e}")
        except Exception as e:
            logger.error(f"[燕云查询] 内部错误: {e}")
            yield event.plain_result(f"[燕云查询] 内部错误:{type(e).__name__}")

    @filter.command("燕云战绩", alias={"yy战绩"})
    async def yy_pvp(self, event: AstrMessageEvent, identity: str = ""):
        identity, err = await self._ensure_queryable(event, identity)
        if err:
            yield event.plain_result(err)
            return
        async for r in self._send_blocks(event, await self._fetch_blocks(identity, "pvp", f"燕云PVP · {identity}")):
            yield r

    # ------------------------------------------------------------------ 管理

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("燕云状态", alias={"yy状态"})
    async def yy_status(self, event: AstrMessageEvent):
        yield event.plain_result(f"【凭据池】\n{self.client._pool.describe()}\n\n正在自检……")
        try:
            results = await self.client.health_check()
        except DashenAPIError as e:
            yield event.plain_result(f"[燕云查询] {e}")
            return
        yield event.plain_result("【自检结果】\n" + "\n".join(results))

    # ------------------------------------------------------------------ 自助登录

    @filter.command("燕云登录", alias={"燕云扫码", "yy登录", "yy扫码"})
    async def yy_login(self, event: AstrMessageEvent):
        """私聊自助登录:扫码 → 选角色 → 签发 token + 写配置 + 绑定。"""
        if event.get_group_id():
            yield event.plain_result("登录涉及个人账号,请私聊我使用「燕云登录」")
            return

        started = await self.login_svc.start()
        if not started.get("started"):
            yield event.plain_result(f"启动失败:{started.get('message')}")
            return

        # 1) 等二维码就绪并发出
        qr_path = Path(StarTools.get_data_dir(PLUGIN_NAME)) / "login_qr_tmp.png"
        for _ in range(100):
            await asyncio.sleep(1.5)
            st = self.login_svc.status()
            if st["qr_b64"]:
                qr_path.write_bytes(base64.b64decode(st["qr_b64"]))
                yield event.chain_result([
                    Comp.Plain(
                        "请用手机「大神 App」→ 首页右上角扫一扫,扫码登录\n"
                        "二维码约 2 分钟有效;登录成功后会列出你账号下的燕云角色"
                    ),
                    Comp.Image.fromFileSystem(str(qr_path)),
                ])
                break
            if st["state"] in ("failed", "timeout"):
                yield event.plain_result(f"登录流程失败:{st['message']}")
                return
        else:
            await self.login_svc.cancel()
            yield event.plain_result("二维码生成超时,请重新发「燕云登录」")
            return

        # 2) 等扫码结果:selecting 需要选角色;done 单角色已自动签发
        roles: list[dict] = []
        for _ in range(60):  # 约 180 秒
            await asyncio.sleep(3)
            st = self.login_svc.status()
            if st["state"] == "selecting":
                roles = st["roles"] or []
                break
            if st["state"] == "done" and st["result"]:
                await self.store.set(
                    event.get_sender_id(),
                    st["result"]["role_id"],
                    {"nick": (st.get("result") or {}).get("nick") or ""},
                )
                yield event.plain_result(self._login_done_text(st))
                return
            if st["state"] in ("failed", "timeout"):
                yield event.plain_result(f"登录失败:{st['message']}")
                return
        else:
            await self.login_svc.cancel()
            yield event.plain_result("等待扫码超时,请重新发「燕云登录」")
            return

        if not roles:
            await self.login_svc.cancel()
            yield event.plain_result("没读到角色列表,请重新发「燕云登录」")
            return

        # 3) 列角色,等用户回复序号
        lines = [f"检测到你账号下有 {len(roles)} 个燕云角色:"]
        for i, r in enumerate(roles, 1):
            extra = " · ".join(
                x for x in (f"Lv.{r.get('level')}" if r.get("level") else "", r.get("server_name") or "") if x
            )
            lines.append(f"{i}. {r.get('nick') or '(未命名)'}" + (f"　{extra}" if extra else ""))
        lines.append("")
        lines.append(f"回复序号绑定(60 秒内,1~{len(roles)})")
        yield event.plain_result("\n".join(lines))

        roles_picked: dict | None = None
        for attempt in range(2):
            choice = ""

            @session_waiter(timeout=60, record_history_chains=False)
            async def waiter(controller: SessionController, ev: AstrMessageEvent):
                nonlocal choice
                text = ev.message_str.strip()
                if not text:
                    return
                choice = text
                controller.stop()

            try:
                await waiter(event)
            except TimeoutError:
                await self.login_svc.cancel()
                yield event.plain_result("超时了,重新发「燕云登录」再来")
                return
            except Exception as e:
                logger.error(f"[燕云查询] 选择角色交互异常: {e}")
                return
            if not choice:
                return
            idx = self._parse_choice(choice, len(roles))
            if idx is None:
                if attempt == 0:
                    yield event.plain_result(f"序号不对哦,请回 1~{len(roles)} 之间的数字(还能再试一次)")
                    continue
                await self.login_svc.cancel()
                yield event.plain_result("没选对,已取消本次登录,重新发「燕云登录」再来")
                return
            roles_picked = roles[idx]
            break

        if roles_picked is None:
            return

        # 4) 签发 + 绑定
        yield event.plain_result(f"正在为「{roles_picked.get('nick') or roles_picked['role_id']}」签发凭据…")
        res = await self.login_svc.issue_for(roles_picked["role_id"])
        if not res.get("ok"):
            yield event.plain_result(f"签发失败:{res.get('message')}")
            return
        await self.store.set(
            event.get_sender_id(),
            roles_picked["role_id"],
            {"nick": roles_picked.get("nick") or ""},
        )
        who = roles_picked.get("nick") or roles_picked["role_id"]
        yield event.plain_result(f"登录好了~ 已经把你和「{who}」绑上。可以直接查看角色属性了")

    @staticmethod
    def _parse_choice(text: str, n: int) -> int | None:
        """把回复解析成 0-based 序号;非数字或越界返回 None。"""
        try:
            i = int(str(text).strip())
        except (TypeError, ValueError):
            return None
        return i - 1 if 1 <= i <= n else None

    @staticmethod
    def _login_done_text(st: dict) -> str:
        r = st.get("result") or {}
        who = r.get("nick") or r.get("role_id") or ""
        return f"登录好了~ 已经把你和「{who}」绑上。可以直接查看角色属性了"

    async def _scan_notify(self, umo: str, qq: str):
        """扫码任务后台轮询:完成后推送结果并自动绑定。"""
        for _ in range(70):  # 最多约 210 秒
            await asyncio.sleep(3)
            st = self.login_svc.status()
            if st["state"] == "done" and st["result"]:
                result = st["result"]
                await self.store.set(qq, result["role_id"], {"nick": result.get("nick") or ""})
                try:
                    await self.context.send_message(
                        umo, MessageChain().message(
                            self._login_done_text(st)
                        )
                    )
                except Exception as e:
                    logger.error(f"[燕云查询] 扫码结果推送失败: {e}")
                return
            if st["state"] in ("failed", "timeout"):
                try:
                    await self.context.send_message(umo, MessageChain().message(f"✗ 登录失败:{st['message']}"))
                except Exception:
                    pass
                return
