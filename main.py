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
- 燕云扫码(私聊):扫码登录并自动绑定自己的角色 —— 推荐方式
- 燕云绑定 <角色编号>:绑定管理员已添加到凭据池的角色
- 燕云解绑:解除绑定
- 燕云我的:查询自己绑定的角色档案(角色名片图 + 探索度进度条)
- 燕云查询 <角色编号>:查询凭据池内的角色(同「燕云我的」卡片)
- 燕云详情 <角色编号>:家园日常 + 面板属性 + 区域收集(属性一览以图片发送,失败自动回退文本)
- 燕云面板 <角色编号>:调号助手数据(属性面板/流派/全服基准对比)
- 燕云分析 <角色编号>:流派养成对标报告(词条/战力对标 + 带部位的洗练方向,图片卡)
- 燕云战绩 <角色编号>:PVP 数据
- 燕云状态:凭据自检(仅管理员)
- 燕云帮助:本说明

凭据获取:私聊发「燕云扫码」自助完成;管理员也可用 WebUI 面板或按 README 手动添加。
自然语言:机器人配置了大模型后,直接说"看看我的燕云角色""查探索度"等也能触发查询。
⚠ 调用的是网易非公开接口,token 年度有效,仅供学习交流。"""


@register("astrbot_plugin_yanyun_query", "林迟羽", "燕云十六声角色查询(角色档案/探索度/流派配置/面板属性)", "v1.0.6")
class YanyunQueryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = BindingStore(Path(StarTools.get_data_dir(PLUGIN_NAME)))
        self._build_client()
        self.login_svc = LoginService(
            server=config.get("server") or "1",
            on_success=self._on_token_issued,
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
        """注册 WebUI 面板后端 API(需要 AstrBot >= 4.27.5)。"""
        if not WEB_API_OK:
            logger.warning("[燕云查询] 当前 AstrBot 版本缺少 astrbot.api.web,面板功能不可用,请升级到 4.27.5+")
        else:
            await self._register_web_apis()

        # 首次安装自愈:后台预装浏览器,避免用户首次扫码干等
        if self.config.get("preinstall_browser", True):
            asyncio.create_task(self._prewarm_browser())

    async def _register_web_apis(self):
        try:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/login/start", self._api_login_start, ["POST"], "开始扫码登录并签发 token"
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/login/status", self._api_login_status, ["GET"], "查询登录/签发状态"
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
        role_id = str(body.get("role_id") or "").strip()
        result = await self.login_svc.start(role_id)
        return json_response({"status": self.login_svc.status(), **result})

    async def _api_login_status(self) -> Any:
        return json_response(self.login_svc.status())

    async def _api_login_cancel(self) -> Any:
        result = await self.login_svc.cancel()
        return json_response({"status": self.login_svc.status(), **result})

    # ------------------------------------------------------------------ 自然语言(LLM 工具)

    async def _resolve_role_for_llm(self, event: AstrMessageEvent, role_id: str) -> tuple[str | None, str]:
        role_id = (role_id or "").strip()
        if role_id:
            return role_id, ""
        bind = await self.store.get(event.get_sender_id())
        if not bind:
            return None, "该用户还没有绑定燕云角色,请引导其私聊发送「燕云扫码」完成登录绑定,或发送「燕云绑定 <角色编号>」"
        return bind.get("identity"), ""

    @staticmethod
    def _stop_after_delivery(event: AstrMessageEvent) -> str:
        """图卡/文本已在工具内直接发送给用户时调用。

        把本轮 Agent 标记为停止,避免大模型再拿工具返回值写一段总结;
        同时返回空字符串,不再把数据回灌给模型。
        """
        event.set_extra("agent_stop_requested", True)
        return ""

    @filter.llm_tool(name="yanyun_query_profile")
    async def yanyun_query_profile(self, event: AstrMessageEvent, role_id: str = "") -> str:
        """查询燕云十六声游戏角色的完整档案,包括等级、门派、功力、成就、外观收集、各区域探索度、区域收集(宝箱/蹊跷数量)、流派配置(套装、功法、心法、装备、奇术)。当用户想查看燕云十六声的角色数据、角色信息、探索度、流派、装备、心法等内容时调用此工具。
        Args:
            role_id(string): 要查询的燕云角色编号(纯数字)。如果用户没有明确指定角色编号,请传空字符串,将自动查询该用户绑定的角色
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
            role_id(string): 要查询的燕云角色编号(纯数字)。如果用户没有明确指定角色编号,请传空字符串,将自动查询该用户绑定的角色
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
            role_id(string): 要查询的燕云角色编号(纯数字)。如果用户没有明确指定角色编号,请传空字符串,将自动查询该用户绑定的角色
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
            role_id(string): 要分析的燕云角色编号(纯数字)。如果用户没有明确指定角色编号,请传空字符串,将自动使用该用户绑定的角色
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
        return f"{advisor_text}\n\n―― 属性面板与全服基准 ――\n{panel_text}\n\n―― 你的分析素材(报告已发用户) ――\n{make_ai_prompt(report)}"[:3500]

    @filter.command("燕云分析", alias={"yy分析"})
    async def yy_analyze(self, event: AstrMessageEvent, identity: str = ""):
        identity = identity.strip()
        if not identity:
            bind = await self.store.get(event.get_sender_id())
            identity, err = self._resolve_role("", bind)
            if err:
                yield event.plain_result("用法:燕云分析 <角色编号>(或先「燕云绑定」)")
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
        identity = identity.strip()
        if not identity:
            bind = await self.store.get(event.get_sender_id())
            identity, err = self._resolve_role("", bind)
            if err:
                yield event.plain_result("用法:燕云面板 <角色编号>(或先「燕云绑定」)")
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
                acc["enabled"] = True
                matched = True
                break
        if not matched:
            accounts.append({
                "name": f"角色{result['role_id']}",
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

    # ------------------------------------------------------------------ 帮助

    @filter.command("燕云帮助", alias={"yy帮助"})
    async def yy_help(self, event: AstrMessageEvent):
        yield event.plain_result(HELP_TEXT)

    # ------------------------------------------------------------------ 绑定

    @filter.command("燕云绑定", alias={"yy绑定"})
    async def yy_bind(self, event: AstrMessageEvent, identity: str = ""):
        if not identity:
            yield event.plain_result(
                "请回复你的燕云角色编号(60秒内有效,游戏内个人信息页可查):"
            )

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
                yield event.plain_result("绑定超时,下次请直接发:燕云绑定 <角色编号>")
                return
            except Exception as e:
                logger.error(f"[燕云查询] 绑定交互异常: {e}")
                return
            if not identity:
                return

        rid = identity.strip()
        async for r in self._do_bind(event, rid):
            yield r

    @filter.regex(r"^(?:燕云绑定|yy绑定)(?![ \t])[^0-9]*[0-9]")
    async def yy_bind_loose(self, event: AstrMessageEvent):
        """兜底:兼容「燕云绑定0455681870」「燕云绑定<0455681870>」等无空格写法。"""
        text = event.get_message_str().strip()
        logger.info(f"[燕云查询] 兜底匹配绑定指令:{text!r}")
        async for r in self._do_bind(event, text):
            yield r

    @staticmethod
    def _parse_role_id(text: str) -> tuple[str, str]:
        """从输入里提取角色编号,返回 (编号, 错误提示)。

        规则:取第一个数字到最后一个数字之间的内容;该段若含非数字字符
        则视为格式错误(如「12345是是是555」),不自动拼接,直接提示用户。
        """
        s = (text or "").strip()
        if not s:
            return "", "没识别到角色编号,请重发:燕云绑定 <角色编号>"
        hits = list(re.finditer(r"[0-9]", s))
        if not hits:
            return "", f"没识别到角色编号:「{s}」里没有数字,请重发:燕云绑定 <角色编号>"
        inner = s[hits[0].start() : hits[-1].end()]
        if not re.fullmatch(r"[0-9]+", inner):
            return "", (
                f"角色编号格式不对:「{s}」里编号中间夹了非数字字符。\n"
                "角色编号必须是连续数字,请重发:燕云绑定 <角色编号>"
            )
        return inner, ""

    async def _do_bind(self, event: AstrMessageEvent, rid: str):
        """绑定 + 凭据校验 + 必要时扫码(yy_bind / yy_bind_loose 共用)。"""
        rid, err = self._parse_role_id(rid)
        if err:
            yield event.plain_result(err)
            return
        user_id = event.get_sender_id()
        is_group = bool(event.get_group_id())
        await self.store.set(user_id, rid)

        cred = self.client._pool.by_role(rid)
        if cred is not None:
            yield event.plain_result(f"角色 {rid} 已有凭据,正在校验是否有效…")
            ok, reason = await self.client.verify_credential(rid)
            if ok:
                yield event.plain_result(f"绑定成功!凭据有效,以后「燕云我的」查询的是:{rid}")
                return
            if reason != "invalid":
                yield event.plain_result(
                    f"已记录绑定:{rid}\n凭据校验异常({reason}),已按有效处理;"
                    "若查询报错请发「燕云扫码」重新登录。"
                )
                return
            yield event.plain_result("原凭据已失效,正在为你发起扫码登录…")

        pool_ids = [c.role_id for c in self.client._pool._creds if c.enabled]

        if not is_group:
            # 私聊场景:自动为该角色发起扫码登录
            started = await self.login_svc.start(rid)
            if not started.get("started"):
                yield event.plain_result(
                    f"已记录绑定:{rid}\n该角色还没有凭据,自动扫码启动失败:{started.get('message')}\n"
                    f"当前已收录角色:{', '.join(pool_ids) or '无'}"
                )
                return
            qr_path = Path(StarTools.get_data_dir(PLUGIN_NAME)) / "login_qr_tmp.png"
            for _ in range(100):
                await asyncio.sleep(1.5)
                st = self.login_svc.status()
                if st["qr_b64"]:
                    qr_path.write_bytes(base64.b64decode(st["qr_b64"]))
                    yield event.chain_result([
                        Comp.Plain(
                            f"已记录绑定:{rid}\n现在为你发起扫码登录:\n"
                            "请用手机「大神 App」→ 首页右上角扫一扫(约2分钟有效),\n"
                            "成功后会自动签发 token 并完成绑定。"
                        ),
                        Comp.Image.fromFileSystem(str(qr_path)),
                    ])
                    break
                if st["state"] in ("failed", "timeout"):
                    yield event.plain_result(f"扫码流程失败:{st['message']}")
                    return
            else:
                st = self.login_svc.status()
                yield event.plain_result(
                    f"二维码生成超时({st.get('message', '')}),请稍后重新发「燕云绑定 <角色编号>」"
                )
                return
            asyncio.create_task(self._scan_notify(event.unified_msg_origin, user_id))
        else:
            yield event.plain_result(
                f"已记录绑定:{rid}\n"
                f"该角色的 token 还没添加(群聊里无法扫码,请私聊我发「燕云绑定 <角色编号>」自助扫码,\n"
                f"或让管理员在面板添加。当前已收录角色:{', '.join(pool_ids) or '无'})"
            )

    @filter.command("燕云解绑", alias={"yy解绑"})
    async def yy_unbind(self, event: AstrMessageEvent):
        ok = await self.store.remove(event.get_sender_id())
        yield event.plain_result("已解绑。" if ok else "你还没有绑定角色,先发「燕云绑定 <角色编号>」")

    # ------------------------------------------------------------------ 查询

    def _resolve_role(self, identity: str, bind: dict | None) -> tuple[str | None, str | None]:
        """返回 (roleId, 错误提示)。"""
        if identity.strip():
            return identity.strip(), None
        if not bind:
            return None, "你还没有绑定角色,先发「燕云绑定 <角色编号>」"
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
        identity = identity.strip()
        if not identity:
            bind = await self.store.get(event.get_sender_id())
            role_id, err = self._resolve_role("", bind)
            if err:
                yield event.plain_result("用法:燕云查询 <角色编号>")
                return
        else:
            role_id = identity
        async for r in self._send_profile(event, role_id):
            yield r

    @filter.command("燕云详情", alias={"yy详情"})
    async def yy_detail(self, event: AstrMessageEvent, identity: str = ""):
        identity = identity.strip()
        if not identity:
            bind = await self.store.get(event.get_sender_id())
            identity, err = self._resolve_role("", bind)
            if err:
                yield event.plain_result("用法:燕云详情 <角色编号>")
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
        identity = identity.strip()
        if not identity:
            bind = await self.store.get(event.get_sender_id())
            identity, err = self._resolve_role("", bind)
            if err:
                yield event.plain_result("用法:燕云战绩 <角色编号>")
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

    # ------------------------------------------------------------------ 自助扫码

    @filter.command("燕云扫码", alias={"yy扫码"})
    async def yy_scan(self, event: AstrMessageEvent):
        """私聊自助扫码:群友扫码登录 → 自动签发 token + 写配置 + 绑定。"""
        if event.get_group_id():
            yield event.plain_result("扫码登录涉及个人账号,请私聊我使用「燕云扫码」")
            return

        yield event.plain_result("请回复你的燕云角色编号(60秒内,游戏内个人信息页可查):")

        identity = ""

        @session_waiter(timeout=60, record_history_chains=False)
        async def waiter(controller: SessionController, ev: AstrMessageEvent):
            nonlocal identity
            text = ev.message_str.strip()
            if not text:
                return  # 空消息安静忽略
            identity = text
            controller.stop()

        try:
            await waiter(event)
        except TimeoutError:
            yield event.plain_result("超时了,重新发「燕云扫码」再来")
            return
        except Exception as e:
            logger.error(f"[燕云查询] 扫码交互异常: {e}")
            return
        if not identity:
            return
        if not identity.isdigit():
            yield event.plain_result("角色编号是纯数字哦,重新发「燕云扫码」再试一次")
            return

        started = await self.login_svc.start(identity)
        if not started.get("started"):
            yield event.plain_result(f"启动失败:{started.get('message')}")
            return

        # 等二维码就绪后以图片发出
        qr_path = Path(StarTools.get_data_dir(PLUGIN_NAME)) / "login_qr_tmp.png"
        for _ in range(100):
            await asyncio.sleep(1.5)
            st = self.login_svc.status()
            if st["qr_b64"]:
                qr_path.write_bytes(base64.b64decode(st["qr_b64"]))
                yield event.chain_result([
                    Comp.Plain(
                        "请用手机「大神 App」→ 首页右上角扫一扫,扫描登录\n"
                        "二维码约 2 分钟有效;扫码成功后会自动绑定并通知你\n"
                        f"(绑定角色:{identity})"
                    ),
                    Comp.Image.fromFileSystem(str(qr_path)),
                ])
                break
            if st["state"] in ("failed", "timeout"):
                yield event.plain_result(f"登录流程失败:{st['message']}")
                return
        else:
            yield event.plain_result("二维码生成超时,请重新发「燕云扫码」")
            return

        # 后台轮询结果并推送
        asyncio.create_task(self._scan_notify(event.unified_msg_origin, event.get_sender_id()))

    async def _scan_notify(self, umo: str, qq: str):
        """扫码任务后台轮询:完成后推送结果并自动绑定。"""
        for _ in range(70):  # 最多约 210 秒
            await asyncio.sleep(3)
            st = self.login_svc.status()
            if st["state"] == "done" and st["result"]:
                result = st["result"]
                await self.store.set(qq, result["role_id"])
                try:
                    await self.context.send_message(
                        umo, MessageChain().message(
                            f"✓ 登录成功!角色 {result['role_id']}({result['dts']} 年度)已签发并绑定,\n"
                            "现在可以直接发「燕云我的」查询了"
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
