"""「属性一览」HTML 卡片渲染:存档数据 → 燕云风格暗色卡片 PNG。

生产环境(插件内)复用 yanyun_login.ensure_browser_ready 的浏览器探测/自动安装链;
本模块自身不依赖 astrbot,方便独立回归。渲染失败一律返回 None,由调用方回退纯文本。
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

try:  # 插件环境用 AstrBot 日志;独立运行降级到标准 logging
    from astrbot.api import logger
except ImportError:  # pragma: no cover - 本地回归路径
    import logging

    logger = logging.getLogger("attrs_image")

try:
    from playwright.async_api import async_playwright

    _PW_OK = True
except ImportError:  # 与 yanyun_login 同策略:缺包不炸,回退文本
    async_playwright = None  # type: ignore[assignment]
    _PW_OK = False

try:  # 插件包内相对导入;本地回归平铺导入
    from .render import (AFFIX_SUMMARY_ORDER, _collect_attrs, _explore_tier, _fmt_duration,
                         _fmt_ts, _get, _load_mappings, _map_name, _summary_order)
except ImportError:
    from render import (  # type: ignore[no-redef]
        AFFIX_SUMMARY_ORDER, _collect_attrs, _explore_tier, _fmt_duration, _fmt_ts, _get,
        _load_mappings, _map_name, _summary_order)

# 装备栏排布(用户指定,弓/射决不展示):上排 主武器/副武器|冠胄/胸甲,下排 环/佩|胫甲/腕甲,
# 两组之间留大间隔(轨道 3 为间隔列)
_PANEL_ORDER = ("1", "2", "3", "4", "10", "11", "5", "8")
_CELL_COL = {"1": "1", "2": "2", "3": "4", "4": "5",
             "10": "1", "11": "2", "5": "4", "8": "5"}


def _slot_label(slot_no: str) -> str:
    return _map_name("slot", slot_no) or f"槽{slot_no}"

_FONT_STACK = (
    '"Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", '
    '"PingFang SC", "WenQuanYi Micro Hei", sans-serif'
)

_STYLE = f"""
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: transparent; font-family: {_FONT_STACK}; }}
  #card {{
    width: 980px; padding: 28px 30px 20px;
    background: linear-gradient(160deg, #1a1e28 0%, #12151d 58%, #0e1117 100%);
    border: 1px solid rgba(216,178,106,.38); border-radius: 18px;
    color: #e8e4d8; font-family: {_FONT_STACK};
  }}
  .head {{ display: flex; align-items: baseline; justify-content: space-between;
           padding-bottom: 14px; border-bottom: 1px solid rgba(216,178,106,.25); }}
  .title {{ font-size: 28px; font-weight: 700; color: #d8b26a; letter-spacing: 2px; }}
  .who {{ font-size: 17px; color: #9aa2b1; }}
  .who b {{ color: #e8e4d8; font-weight: 600; }}
  .sec {{ margin-top: 18px; }}
  .sec-title {{ display: flex; align-items: center; gap: 8px;
                font-size: 17px; color: #d8b26a; letter-spacing: 1px; margin-bottom: 10px; }}
  .sec-title::before {{ content: ""; width: 3px; height: 14px;
                        background: #d8b26a; border-radius: 2px; }}
  .sec-title .n {{ color: #767f8f; font-size: 15px; letter-spacing: 0; }}
  .wuwei {{ display: flex; justify-content: space-between; gap: 8px;
            background: rgba(216,178,106,.07); border: 1px solid rgba(216,178,106,.16);
            border-radius: 10px; padding: 10px 16px; }}
  .wuwei .cell {{ text-align: center; flex: 1; }}
  .wuwei .k {{ font-size: 15px; color: #9aa2b1; margin-bottom: 2px; }}
  .wuwei .v {{ font-size: 21px; font-weight: 600; color: #f0ead8;
               font-variant-numeric: tabular-nums; }}
  .equips {{ display: grid;
             grid-template-columns: repeat(2, 1fr) 24px repeat(2, 1fr);
             gap: 10px; }}
  .equip {{ background: rgba(255,255,255,.045); border: 1px solid rgba(255,255,255,.06);
            border-radius: 10px; padding: 10px 12px; }}
  .equip.empty {{ background: rgba(255,255,255,.015); border-style: dashed; }}
  .eq-name {{ display: flex; align-items: baseline; gap: 6px; margin-bottom: 4px; }}
  .eq-name .slot {{ color: #d8b26a; font-size: 13.5px; white-space: nowrap; }}
  .eq-name .nm {{ color: #f0ead8; font-weight: 600; font-size: 15px; }}
  .eq-tags {{ display: flex; gap: 5px; margin-bottom: 7px; }}
  .tag {{ font-size: 12.5px; line-height: 1; padding: 3px 7px; border-radius: 8px;
          white-space: nowrap; }}
  .tag.suit {{ color: #e3c27e; background: rgba(216,178,106,.14);
               border: 1px solid rgba(216,178,106,.30); }}
  .tag.lv {{ color: #9aa2b1; background: rgba(255,255,255,.05);
             border: 1px solid rgba(255,255,255,.10); }}
  .aff {{ display: flex; justify-content: space-between; gap: 8px;
          font-size: 14.5px; line-height: 1.85; font-variant-numeric: tabular-nums; }}
  .aff .an {{ color: #a8b0bf; }}
  .aff .av {{ color: #e8e4d8; white-space: nowrap; }}
  .aff .av.pct {{ color: #7cc7e8; }}
  .anote {{ color: #5c6472; font-size: 13.5px; margin-top: 2px; }}
  .totals {{ display: grid; grid-template-columns: 1fr 1fr; gap: 6px 22px; }}
  .trow {{ display: flex; align-items: baseline; gap: 8px;
           font-size: 15.5px; line-height: 1.9; font-variant-numeric: tabular-nums; }}
  .trow .tn {{ color: #a8b0bf; flex: 1; }}
  .trow .tv {{ color: #d8b26a; font-weight: 600; white-space: nowrap; }}
  .trow .tc {{ color: #767f8f; font-size: 13.5px; white-space: nowrap;
               border: 1px solid rgba(118,127,143,.4); border-radius: 8px;
               padding: 0 7px; }}
  .foot {{ margin-top: 16px; padding-top: 10px; text-align: center;
           font-size: 13.5px; color: #5c6472;
           border-top: 1px dashed rgba(255,255,255,.09); }}
  .pstats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 4px; }}
  .pstats .pcell {{ background: rgba(255,255,255,.045); border: 1px solid rgba(255,255,255,.07);
                    border-radius: 9px; padding: 7px 12px; display: flex;
                    justify-content: space-between; align-items: baseline; }}
  .pstats .pk {{ font-size: 14px; color: #9aa2b1; }}
  .pstats .pv {{ font-size: 15.5px; font-weight: 600; color: #f0ead8;
                 font-variant-numeric: tabular-nums; }}
"""


def _esc(v: Any) -> str:
    return html.escape(str(v), quote=False)


# roleInfo 面板最终属性的展示顺序(与文本版 _render_panel_attrs 一致)
# 展示顺序(用户指定清单:外功→破竹→无相→气血→外防→精准→会心/实际→会意/实际)
_PANEL_ATTR_ORDER = [
    ("MIN_W_ATK", None), ("MAX_W_ATK", None),
    ("MIN_PRO_ATK_A", None), ("MAX_PRO_ATK_A", None),
    ("MIN_PRO_ATK_B", None), ("MAX_PRO_ATK_B", None),
    ("MIN_PRO_ATK_C", None), ("MAX_PRO_ATK_C", None),
    ("MIN_PRO_ATK_E", None), ("MAX_PRO_ATK_E", None),
    ("MIN_ACTIVE_PRO_ATK", None), ("MAX_ACTIVE_PRO_ATK", None),
    ("hpMax", "气血最大值"),
    ("W_DEF", None),
    ("ACR_PROB", None),
    ("CRI_PROB", None), ("REAL_CRI_PROB", None),
    ("BASH_PROB", None), ("REAL_BASH_PROB", None),
]


# 面板最小/最大成对的属性(合并为「名称 lo~hi」一行)
_PANEL_PAIRS = [
    ("MIN_W_ATK", "MAX_W_ATK"),
    ("MIN_PRO_ATK_A", "MAX_PRO_ATK_A"),
    ("MIN_PRO_ATK_B", "MAX_PRO_ATK_B"),
    ("MIN_PRO_ATK_C", "MAX_PRO_ATK_C"),
    ("MIN_PRO_ATK_E", "MAX_PRO_ATK_E"),
    ("MIN_ACTIVE_PRO_ATK", "MAX_ACTIVE_PRO_ATK"),
]


def _collect_panel_attrs(info: dict | None) -> list[tuple[str, str]]:
    """roleInfo 面板最终属性:最小/最大合并为区间行,顺序按用户清单,概率加 %。"""
    if not isinstance(info, dict):
        return []

    def val(key: str) -> float | None:
        v = info.get(key)
        if v in (None, "", "0", 0):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def fmt(f: float) -> str:
        return str(int(f)) if f.is_integer() else str(round(f, 1))

    def attr_name(key: str) -> str:
        name = _map_name("attr", key)
        if name is None and key.startswith("REAL_"):
            base = _map_name("attr", key[5:])
            if base:
                return "实际" + base
        return name or key

    def label_order(label: str) -> int:
        o = _summary_order(label)
        if o < len(AFFIX_SUMMARY_ORDER):
            return o
        for pre in ("最小", "最大"):  # 合并后的「外功攻击」按「最小外功攻击」的位置排
            o2 = _summary_order(pre + label)
            if o2 < len(AFFIX_SUMMARY_ORDER):
                return o2
        return o

    rows: list[tuple[str, str, int]] = []
    v = val("hpMax")
    if v is not None:
        rows.append(("气血最大值", fmt(v), label_order("气血最大值")))
    for lo_k, hi_k in _PANEL_PAIRS:
        lo, hi = val(lo_k), val(hi_k)
        if lo is None and hi is None:
            continue
        label = attr_name(lo_k)
        for pre in ("最小", "最大"):
            if label.startswith(pre):
                label = label[len(pre):]
                break
        order = label_order(label)
        if lo is not None and hi is not None:
            rows.append((label, f"{fmt(lo)}~{fmt(hi)}", order))
        elif lo is not None:
            rows.append((f"最小{label}", fmt(lo), order))
        else:
            rows.append((f"最大{label}", fmt(hi), order))
    v = val("W_DEF")
    if v is not None:
        rows.append(("外功防御", fmt(v), label_order("外功防御")))
    for k in ("ACR_PROB", "CRI_PROB", "REAL_CRI_PROB", "BASH_PROB", "REAL_BASH_PROB"):
        v = val(k)
        if v is not None:
            rows.append((attr_name(k), f"{fmt(v)}%", label_order(attr_name(k))))
    rows.sort(key=lambda r: r[2])
    return [(k, v) for k, v, _ in rows]


# 与 yanyun_login._LAUNCH_TARGETS 同款探测顺序(兜底路径用)
_FALLBACK_TARGETS = (
    ("chromium(内置)", {}),
    ("系统 Chrome", {"channel": "chrome"}),
    ("系统 Edge", {"channel": "msedge"}),
)


async def _probe_fallback_browser() -> dict:
    """无 yanyun_login 环境(本地回归)时探测可用浏览器,返回 launch kwargs。"""
    pw = await async_playwright().start()
    try:
        for _label, kwargs in _FALLBACK_TARGETS:
            try:
                b = await pw.chromium.launch(headless=True, **kwargs)
                await b.close()
                return kwargs
            except Exception:
                continue
    finally:
        try:
            await pw.stop()
        except Exception:
            pass
    raise RuntimeError("无可用无头浏览器(内置 chromium / Chrome / Edge 均失败)")


def _is_pct(value_disp: str) -> bool:
    return value_disp.endswith("%")


def _tags_html(eq: dict) -> str:
    """装备头部右侧徽章:套装(金) + 等级(灰),缺啥省啥。"""
    bits = []
    if eq.get("suit"):
        bits.append(f'<span class="tag suit">{_esc(eq["suit"])}</span>')
    if eq.get("level") not in (None, ""):
        bits.append(f'<span class="tag lv">{_esc(eq["level"])}级</span>')
    return "".join(bits)


def _attrs_sections(view: dict, panel_attrs: list | None = None, include_wuwei: bool = True) -> list[str]:
    """属性一览各区块(不含卡片包装/头部/尾注)。include_wuwei=False 用于合并总览卡(五维条另行展示)。"""
    parts: list[str] = []

    if include_wuwei and view.get("wuwei"):
        cells = "".join(
            f'<div class="cell"><div class="k">{_esc(n)}</div>'
            f'<div class="v">{_esc(v)}</div></div>'
            for n, v in view["wuwei"]
        )
        parts.append(f'<div class="sec"><div class="wuwei">{cells}</div></div>')

    if panel_attrs:
        cells = "".join(
            f'<div class="pcell"><div class="pk">{_esc(k)}</div>'
            f'<div class="pv">{_esc(v)}</div></div>'
            for k, v in panel_attrs
        )
        parts.append(f'<div class="sec"><div class="sec-title">面板属性</div>'
                     f'<div class="pstats">{cells}</div></div>')

    per_equip = view.get("per_equip") or []
    if per_equip:
        by_slot = {eq["slot_no"]: eq for eq in per_equip}
        cards = []
        for i, slot_no in enumerate(_PANEL_ORDER):
            row = 1 + i // 4  # 每行 4 格(两组各 2 格)
            col = _CELL_COL.get(slot_no)
            style = f' style="grid-column:{col};grid-row:{row}"' if col else ""
            eq = by_slot.pop(slot_no, None)
            if eq is None:
                cards.append(
                    f'<div class="equip empty"{style}><div class="eq-name">'
                    f'<span class="slot">{_esc(_slot_label(slot_no))}</span></div>'
                    f'<div class="anote">未装备</div></div>'
                )
                continue
            rows = "".join(
                f'<div class="aff"><span class="an">{_esc(n)}</span>'
                f'<span class="av{" pct" if _is_pct(d) else ""}">{_esc(d)}</span></div>'
                for n, d in eq["affixes"]
            )
            head = (
                f'<div class="eq-name"><span class="slot">{_esc(eq["slot"])}</span>'
                f'<span class="nm">{_esc(eq["name"])}</span></div>'
                f'<div class="eq-tags">{_tags_html(eq)}</div>'
            )
            cards.append(f'<div class="equip"{style}>{head}{rows}</div>')
        for eq in by_slot.values():  # 防御:未在排布表里的槽位追加在末尾
            rows = "".join(
                f'<div class="aff"><span class="an">{_esc(n)}</span>'
                f'<span class="av{" pct" if _is_pct(d) else ""}">{_esc(d)}</span></div>'
                for n, d in eq.get("affixes") or []
            )
            head = (
                f'<div class="eq-name"><span class="slot">{_esc(eq["slot"])}</span>'
                f'<span class="nm">{_esc(eq["name"])}</span></div>'
                f'<div class="eq-tags">{_tags_html(eq)}</div>'
            )
            cards.append(f'<div class="equip"{style}>{head}{rows}</div>')
        parts.append(
            f'<div class="sec"><div class="sec-title">装备'
            f'<span class="n">共{_esc(view.get("affix_total", 0))}条</span></div>'
            f'<div class="equips">{"".join(cards)}</div></div>'
        )

    totals = sorted(view.get("totals") or [], key=lambda t: _summary_order(t[0]))
    if totals:
        rows = "".join(
            f'<div class="trow"><span class="tn">{_esc(n)}</span>'
            f'<span class="tv{" pct" if _is_pct(d) else ""}">{_esc(d)}</span>'
            f'<span class="tc">{c}条</span></div>'
            for n, d, c in totals
        )
        parts.append(
            f'<div class="sec"><div class="sec-title">装备词条 · 合并汇总'
            f'<span class="n">不含弓箭类</span></div>'
            f'<div class="totals">{rows}</div></div>'
        )

    return parts


def build_attrs_html(view: dict, panel_attrs: list | None = None) -> str:
    """把 _collect_attrs 的视图数据渲染成完整 HTML(属性一览卡)。"""
    from datetime import datetime

    who_bits = []
    if view.get("nickname"):
        who_bits.append(f"<b>{_esc(view['nickname'])}</b>")
    if view.get("level") not in (None, ""):
        who_bits.append(f"Lv.{_esc(view['level'])}")
    who = " · ".join(who_bits)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>{_STYLE}</style></head><body><div id="card">
<div class="head"><div class="title">属性一览</div>
<div class="who">{who}</div></div>
{"".join(_attrs_sections(view, panel_attrs))}
<div class="foot">数据来源:网易大神 · 生成于 {stamp}</div>
</div></body></html>"""


async def render_attrs_image(
    data: dict, mirror: str = "", auto_install: bool = True, info_data: dict | None = None
) -> bytes | None:
    """渲染属性一览 PNG。成功返回图片字节,任何失败返回 None(调用方回退纯文本)。

    info_data: roleInfo 的 data(可选),提供时卡片附加「面板属性」区。
    先走 yanyun_login.ensure_browser_ready(探测链+自动安装);该模块不可用时
    (如本地回归无 astrbot)退化为直接启动内置 chromium。
    """
    view = _collect_attrs(data)
    if not (view["wuwei"] or view["per_equip"] or view["totals"]):
        return None  # 没有可展示的数据
    page_html = build_attrs_html(view, _collect_panel_attrs(info_data))

    if not _PW_OK:
        logger.warning("[燕云图片] playwright 未安装,回退文本输出")
        return None

    launch_kwargs: dict = {}
    pw = None
    browser = None
    try:
        try:
            try:
                from .yanyun_login import ensure_browser_ready
            except ImportError:
                from yanyun_login import ensure_browser_ready  # type: ignore[no-redef]

            ok, msg, launch_kwargs = await ensure_browser_ready(
                mirror=mirror, auto_install=auto_install
            )
            if not ok:
                logger.warning(f"[燕云图片] 无可用浏览器: {msg}")
                return None
        except ImportError:
            # 本地回归/独立运行(astrbot 缺失):按 yanyun_login 同款顺序探测浏览器
            launch_kwargs = await _probe_fallback_browser()

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True, **launch_kwargs)
        page = await browser.new_page(
            viewport={"width": 1040, "height": 700}, device_scale_factor=2
        )
        await page.set_content(page_html, wait_until="load")
        card = page.locator("#card")
        png = await card.screenshot(type="png")
        logger.info(f"[燕云图片] 属性一览渲染成功({len(png) // 1024}KB)")
        return png
    except Exception as e:
        logger.warning(f"[燕云图片] 渲染失败,将回退文本: {type(e).__name__}: {e}")
        return None
    finally:
        for closer in ((lambda: browser and browser.close()), (lambda: pw and pw.stop())):
            try:
                r = closer()
                if r is not None:
                    await r
            except Exception:
                pass


# ============================ 养成分析卡片 ============================

_ADVISOR_CSS = """
  #card { width: max-content; min-width: 640px; max-width: 760px; }
  .meta { font-size: 13.5px; color: #9aa2b1; margin-top: 12px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .badge { font-size: 12px; padding: 3px 9px; border-radius: 9px; border: 1px solid rgba(216,178,106,.3);
           color: #e3c27e; background: rgba(216,178,106,.1); }
  .badge.ok { color: #8fc98f; border-color: rgba(143,201,143,.35); background: rgba(143,201,143,.08); }
  .table { display: grid; grid-template-columns: max-content 64px 64px max-content;
           column-gap: 16px; align-items: baseline; margin-top: 4px; }
  .arow { display: contents; }
  .arow > span { border-bottom: 1px dashed rgba(255,255,255,.06); padding: 3px 0; }
  .arow.hrow > span { border-bottom: 1px solid rgba(255,255,255,.12); color: #767f8f; font-size: 12px; }
  .c1 { color: #d5d0c2; font-size: 14px; }
  .hrow .c1 { color: #767f8f; }
  .c2, .c3 { text-align: right; color: #e8e4d8; font-variant-numeric: tabular-nums; font-size: 14px; }
  .hrow .c2, .hrow .c3 { color: #767f8f; }
  .c4 { color: #a8b0bf; font-size: 13.5px; }
  .st-miss { color: #e08585; } .st-keep { color: #8fc98f; } .st-trans { color: #7cc7e8; }
  .st-gold { color: #d8b26a; } .st-mute { color: #8a93a3; }
  ol.dirs { margin: 4px 0 0 0; padding-left: 20px; }
  ol.dirs li { font-size: 13.5px; line-height: 1.9; color: #d5d0c2; }
  ol.dirs li::marker { color: #d8b26a; }
"""


def _st_class(st: str) -> str:
    if st.startswith("缺") or "优先更换" in st or "建议洗成" in st or st.startswith("差"):
        return "st-miss"
    if "可保留" in st or st == "达标":
        return "st-keep"
    if "过渡" in st:
        return "st-trans"
    if "洗成" in st:
        return "st-gold"
    return "st-mute"


def _arow(cells: list[str], head: bool = False) -> str:
    cls = "arow hrow" if head else "arow"
    return f'<div class="{cls}"><span class="c1">{cells[0]}</span><span class="c2">{cells[1]}</span>' \
           f'<span class="c3">{cells[2]}</span><span class="c4">{cells[3]}</span></div>'


def build_advisor_html(report: dict, dirs: list[str]) -> str:
    """把 build_advisor 的报告渲染成完整 HTML(养成分析卡,不含 AI 段落——AI 由机器人侧生成)。"""
    import html as _html
    from datetime import datetime

    def esc(v) -> str:
        return _html.escape(str(v), quote=False)

    suit_ok = bool(report["suit_rec"]) and all(
        report["suits"].get(n, 0) >= 4 for n in re.findall(r"[一-龥]{2,4}(?=四件套)", report["suit_rec"]))
    suits = "+".join(f"{k}{v}件" for k, v in report["suits"].items())
    who = " · ".join(x for x in (
        f"<b>{esc(report['nickname'])}</b>" if report.get("nickname") else "",
        f"Lv.{esc(report['level'])}" if report.get("level") not in (None, "") else "") if x)

    h = [f'''<!DOCTYPE html><html><head><meta charset="utf-8"><style>{_STYLE}{_ADVISOR_CSS}
</style></head><body><div id="card">
<div class="head"><div class="title">养成分析 · {esc(report["liupai"])}</div><div class="who">{who}</div></div>
<div class="meta"><span>功法: 主 {esc(report["kongfu_main"])} · 副 {esc(report["kongfu_sub"])}</span>
<span class="badge{' ok' if suit_ok else ''}">套装 {esc(suits)}{' ✓ 与推荐一致' if suit_ok else ' · 推荐: ' + esc(report["suit_rec"])}</span></div>''']

    if report["affix_rows"] or report["panel_rows"]:
        # 两张表共用同一套列宽:首列 = 全部条目名称的最大显示宽度(CJK 按 2 倍计),保证垂直对齐
        import unicodedata

        def dw(x) -> float:
            return sum(2 if unicodedata.east_asian_width(ch) in "FW" else 1 for ch in str(x))

        names = [r["name"] for r in report["affix_rows"]] + [r["mod"] for r in report["panel_rows"]]
        col1 = max((dw(n) for n in names), default=10) * 7.2 + 6
        grid_cols = f"{col1:.0f}px 76px 76px max-content"

    if report["affix_rows"]:
        rows = [_arow(["词条", "面板", "参考", "备注"], head=True)]
        for r in report["affix_rows"]:
            note = f'<span class="{_st_class(r["status"])}">{esc(r["status"])}</span>'
            rows.append(_arow([esc(r["name"]), esc(r["have"]), esc(r["ref"]), note]))
        h.append(f'<div class="sec"><div class="sec-title">词条数量对标</div>'
                 f'<div class="table" style="grid-template-columns:{grid_cols};">{"".join(rows)}</div></div>')

    if report["panel_rows"]:
        rows = [_arow(["模块", "面板", "参考", "状态 · 备注"], head=True)]
        for r in report["panel_rows"]:
            cls = "st-keep" if (r["status"].startswith("+") or r["status"] == "达标") else "st-miss"
            note = f'<span class="{cls}">{esc(r["status"])}</span> · {esc(r["note"])}'
            rows.append(_arow([esc(r["mod"]), esc(r["panel"]), esc(r["ref"]), note]))
        h.append(f'<div class="sec"><div class="sec-title">战力模块对标</div>'
                 f'<div class="table" style="grid-template-columns:{grid_cols};">{"".join(rows)}</div></div>')

    if dirs:
        lis = "".join(f"<li>{esc(d)}</li>" for d in dirs)
        h.append(f'<div class="sec"><div class="sec-title">养成方向</div><ol class="dirs">{lis}</ol></div>')

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    h.append(f'<div class="foot">数据来源:网易大神 · 流派推荐文档 · 生成于 {stamp}</div>')
    h.append("</div></body></html>")
    return "".join(h)


async def render_advisor_image(report: dict, mirror: str = "", auto_install: bool = True) -> bytes | None:
    """渲染养成分析卡 PNG(不含 AI 段落)。失败返回 None,调用方回退文本块。"""
    if not _PW_OK:
        logger.warning("[燕云图片] playwright 未安装,养成分析回退文本输出")
        return None
    try:
        from .build_advisor import make_directions
    except ImportError:
        from build_advisor import make_directions  # type: ignore[no-redef]

    if not report.get("affix_rows") and not report.get("panel_rows"):
        return None
    page_html = build_advisor_html(report, make_directions(report))

    launch_kwargs: dict = {}
    pw = None
    browser = None
    try:
        try:
            try:
                from .yanyun_login import ensure_browser_ready
            except ImportError:
                from yanyun_login import ensure_browser_ready  # type: ignore[no-redef]
            ok, msg, launch_kwargs = await ensure_browser_ready(mirror=mirror, auto_install=auto_install)
            if not ok:
                logger.warning(f"[燕云图片] 无可用浏览器: {msg}")
                return None
        except ImportError:
            launch_kwargs = await _probe_fallback_browser()

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True, **launch_kwargs)
        page = await browser.new_page(viewport={"width": 820, "height": 700}, device_scale_factor=2)
        await page.set_content(page_html, wait_until="load")
        png = await page.locator("#card").screenshot(type="png")
        logger.info(f"[燕云图片] 养成分析渲染成功({len(png) // 1024}KB)")
        return png
    except Exception as e:
        logger.warning(f"[燕云图片] 养成分析渲染失败,将回退文本: {type(e).__name__}: {e}")
        return None
    finally:
        for closer in ((lambda: browser and browser.close()), (lambda: pw and pw.stop())):
            try:
                r = closer()
                if r is not None:
                    await r
            except Exception:
                pass


# ============================ 燕云我的 · 角色档案卡 ============================

_PROFILE_CSS = """
  #pcard { width: 700px; padding: 26px 30px 18px;
           background: linear-gradient(160deg, #1a1e28 0%, #12151d 58%, #0e1117 100%);
           border: 1px solid rgba(216,178,106,.38); border-radius: 18px;
           color: #e8e4d8; font-family: "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei",
           "PingFang SC", "WenQuanYi Micro Hei", sans-serif; }
  #pcard .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  #pcard .stat { background: rgba(216,178,106,.07); border: 1px solid rgba(216,178,106,.16);
                 border-radius: 10px; padding: 9px 6px; text-align: center; }
  #pcard .stat .k { font-size: 13.5px; color: #9aa2b1; margin-bottom: 2px; }
  #pcard .stat .v { font-size: 18.5px; font-weight: 600; color: #f0ead8;
                    font-variant-numeric: tabular-nums; }
  #pcard .stat .v.gold { color: #d8b26a; }
  #pcard .mutline { margin-top: 9px; font-size: 13.5px; color: #767f8f; }
  #pcard .guise { margin-top: 5px; font-size: 13.5px; color: #8a93a3; }
  #pcard .xrow { margin: 9px 0; }
  #pcard .xtop { display: flex; align-items: baseline; gap: 8px; font-size: 15px; }
  #pcard .xtop .xn { color: #f0ead8; font-weight: 600; }
  #pcard .xtop .xs { color: #d8b26a; font-variant-numeric: tabular-nums; }
  #pcard .xtop .xt { font-size: 13.5px; color: #9aa2b1; }
  #pcard .xtop .xc { flex: 1; text-align: right; font-size: 13.5px; color: #8a93a3;
                     font-variant-numeric: tabular-nums; }
  #pcard .bar { margin-top: 5px; height: 6px; border-radius: 3px; background: rgba(255,255,255,.08); }
  #pcard .bar > i { display: block; height: 100%; border-radius: 3px;
                    background: linear-gradient(90deg, #b8934e, #e3c27e); }
  #pcard .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
  #pcard .chip { font-size: 13.5px; padding: 3px 9px; border-radius: 9px;
                 background: rgba(255,255,255,.05); border: 1px solid rgba(255,255,255,.09);
                 color: #cfc9b8; }
  #pcard .chip.suit { color: #e3c27e; border-color: rgba(216,178,106,.3); background: rgba(216,178,106,.12); }
  #pcard .chip.xf { color: #7cc7e8; border-color: rgba(124,199,232,.25); background: rgba(124,199,232,.08); }
  #pcard .eqgrid { display: grid; grid-template-columns: 1fr 1fr; gap: 4px 14px; margin-top: 6px; }
  #pcard .eqgrid span { font-size: 13.5px; color: #a8b0bf; line-height: 1.7; }
  #pcard .eqgrid span b { color: #cfc9b8; font-weight: 600; }
  #pcard .xgrid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 14px; margin-top: 4px; }
  #pcard .xcard { background: rgba(255,255,255,.045); border: 1px solid rgba(255,255,255,.07);
                  border-radius: 10px; padding: 9px 12px; }
  #pcard .xcard .xtop { display: flex; align-items: baseline; gap: 8px; font-size: 15px; }
  #pcard .xcard .xn { color: #f0ead8; font-weight: 600; }
  #pcard .xcard .xs { color: #d8b26a; font-variant-numeric: tabular-nums; }
  #pcard .xcard .xt { font-size: 13.5px; color: #9aa2b1; }
  #pcard .xcard .bar { margin-top: 6px; height: 6px; border-radius: 3px; background: rgba(255,255,255,.08); }
  #pcard .xcard .bar > i { display: block; height: 100%; border-radius: 3px;
                           background: linear-gradient(90deg, #b8934e, #e3c27e); }
  #pcard .xcard .xc { margin-top: 6px; font-size: 13px; color: #8a93a3;
                      font-variant-numeric: tabular-nums; }
  #pcard .stats4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-top: 4px; }
  #pcard .ecard { background: rgba(255,255,255,.045); border: 1px solid rgba(255,255,255,.06);
                  border-radius: 10px; padding: 9px 6px; text-align: center; }
  #pcard .ecard .k { font-size: 13.5px; color: #9aa2b1; margin-bottom: 3px; }
  #pcard .ecard .v { font-size: 15.5px; font-weight: 600; color: #f0ead8; }
"""


def _collect_profile(data: dict, info_data: dict | None) -> dict:
    """收集「燕云我的」卡片所需的结构化数据(文本渲染仍是兜底来源,互不影响)。"""
    p: dict[str, Any] = {}

    def g(key, default=None):
        return _get(data, key, default)

    p["nickname"] = str(g("base.nickname") or "").strip()
    p["level"] = g("base.level")
    p["number_id"] = g("base.number_id")
    p["power"] = g("base.max_xiuwei_kungfu")
    p["fashion_score"] = g("fashion.score")
    school_id = g("base.school")
    p["school"] = _map_name("school", school_id) if school_id is not None else None
    p["create"] = _fmt_ts(g("base.create_time")) if g("base.create_time") else ""
    p["login"] = _fmt_ts(g("base.login_time")) if g("base.login_time") else ""
    p["online"] = _fmt_duration(g("base.online_time")) if g("base.online_time") else ""

    ach = g("achievement.quantity")
    if isinstance(ach, dict) and ach:
        try:
            p["ach_total"] = sum(int(v) for v in ach.values() if str(v).isdigit())
        except (TypeError, ValueError):
            p["ach_total"] = None
        p["ach_detail"] = ", ".join(f"{k}档×{v}" for k, v in sorted(ach.items()))
    else:
        p["ach_total"] = None
        p["ach_detail"] = ""
    p["title_count"] = None
    titles = g("title_prop.unlock_titles")
    if isinstance(titles, dict):
        for v in titles.values():
            if isinstance(v, list):
                p["title_count"] = len(v)
                break

    guise = g("guise.collection_progress")
    p["guise"] = []
    if isinstance(guise, dict):
        for k, v in guise.items():
            if isinstance(v, dict) and "all" in v and "cnt" in v:
                p["guise"].append((k, f"{v['cnt']}/{v['all']}"))
            else:
                p["guise"].append((k, str(v)))

    # 探索(区域名/分数/称号 + 宝箱/蹊跷)
    scores = data.get("common_score_data.scores")
    eml = _load_mappings().get("data_report").get("exploreMapList") if isinstance(_load_mappings().get("data_report"), dict) else []
    chests, qiqiao = {}, {}
    info_data = info_data or {}
    for x in info_data.get("chestByRegion") or []:
        if isinstance(x, dict):
            chests[str(x.get("region"))] = x.get("count")
    for x in info_data.get("qiQiaoByRegion") or []:
        if isinstance(x, dict):
            qiqiao[str(x.get("region"))] = x.get("count")
    p["explore"] = []
    if isinstance(scores, dict):
        for item in eml:
            if not isinstance(item, dict):
                continue
            rid = str(item.get("id"))
            score = scores.get(rid)
            if score is None:
                continue
            try:
                score = int(score)
            except (TypeError, ValueError):
                continue
            tname, lv = _explore_tier(score)
            p["explore"].append({
                "name": item.get("name") or f"区域{rid}", "score": score,
                "tier": tname, "lv": lv,
                "chest": chests.get(rid), "qiqiao": qiqiao.get(rid),
            })

    # 流派配置(套装/功法/心法/装备/奇术)
    suits: dict[str, int] = {}
    equips: list[str] = []
    equip_map = _load_mappings().get("equip") or {}
    equips_raw = data.get("combat_plan.wear_equips") or {}
    for slot_no, item in sorted(equips_raw.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 999):
        if not isinstance(item, dict):
            continue
        ex = item.get("ex") or {}
        if not ex:
            continue  # 占位
        eq_info = equip_map.get(str(item.get("No"))) or {}
        eq_name = eq_info.get("name") if isinstance(eq_info, dict) else None
        lv = eq_info.get("level") if isinstance(eq_info, dict) else None
        sid = ex.get("suffix")
        suit = _map_name("suffix", sid) if sid is not None else None
        if suit:
            suits[suit] = suits.get(suit, 0) + 1
        slot_label = _map_name("slot", slot_no) or f"槽{slot_no}"
        bits = [f"{slot_label}·{eq_name or '装备' + str(item.get('No'))}"]
        if lv:
            bits.append(f"{lv}级")
        if suit:
            bits.append(suit)
        equips.append(" · ".join(bits))
    p["suits"] = sorted(suits.items(), key=lambda kv: -kv[1])
    p["equips"] = equips
    main_id = g("combat_plan.kongfu.kongfu_main")
    sub_id = g("combat_plan.kongfu.kongfu_sub")
    p["kongfu_main"] = f"{_map_name('kongfu', main_id) or main_id}{g('combat_plan.kongfu_level.kongfu_main.level') or ''}级" if main_id else ""
    p["kongfu_sub"] = f"{_map_name('kongfu', sub_id) or sub_id}{g('combat_plan.kongfu_level.kongfu_sub.level') or ''}级" if sub_id else ""
    xinfo = data.get("combat_plan.xinfa_info") or {}
    p["xinfa"] = []
    for xid in data.get("combat_plan.xinfa.passive_slots") or []:
        rank = (xinfo.get(str(xid)) or {}).get("rank")
        xname = _map_name("xinfa", xid) or f"心法{xid}"
        p["xinfa"].append(f"{xname}·{rank}重" if rank else xname)
    qs_level = data.get("combat_plan.battle_qs_level") or {}
    p["qs"] = []
    for slot, qid in (data.get("combat_plan.battle_qs") or {}).items():
        rank = qs_level.get(str(qid))
        p["qs"].append((_map_name("qs", qid) or f"奇术{qid}", rank))
    return p


def _esc_html(v) -> str:
    return html.escape(str(v), quote=False)


def _profile_who(p: dict) -> str:
    bits = [f"<b>{_esc_html(p['nickname'])}</b>" if p["nickname"] else ""]
    if p.get("school"):
        bits.append(_esc_html(p["school"]))
    if p.get("level") not in (None, ""):
        bits.append(f"Lv.{_esc_html(p['level'])}")
    return " · ".join(x for x in bits if x)


def _profile_sections(p: dict) -> list[str]:
    """档案卡各区块(不含卡片包装/头部/尾注)。"""
    from datetime import datetime

    def esc(v) -> str:
        return _esc_html(v)

    def stat(k, v, gold=False):
        return (f'<div class="stat"><div class="k">{esc(k)}</div>'
                f'<div class="v{" gold" if gold else ""}">{esc(v)}</div></div>')

    stats = []
    if p.get("power") is not None:
        stats.append(stat("最高功力", p["power"], gold=True))
    if p.get("level") not in (None, ""):
        stats.append(stat("等级", f"Lv.{p['level']}"))
    if p.get("online"):
        stats.append(stat("累计在线", p["online"]))
    if p.get("ach_total") is not None:
        stats.append(stat("成就", p["ach_total"]))
    if p.get("title_count") is not None:
        stats.append(stat("已解锁称号", p["title_count"]))
    if p.get("fashion_score") is not None:
        stats.append(stat("时装收集分", p["fashion_score"]))

    h: list[str] = []
    h.append(f'<div class="stats">{"".join(stats)}</div>')
    mut = []
    if p.get("number_id"):
        mut.append(f"编号 {esc(p['number_id'])}")
    if p.get("ach_detail"):
        mut.append(f"成就 {esc(p['ach_detail'])}")
    if p.get("create"):
        mut.append(f"建号 {esc(p['create'])}")
    if p.get("login"):
        mut.append(f"最近登录 {esc(p['login'])}")
    if mut:
        h.append('<div class="mutline">' + " · ".join(mut) + "</div>")
    if p["guise"]:
        h.append('<div class="guise">外观收集: ' + "、".join(
            f"{esc(k)} {esc(v)}" for k, v in p["guise"]) + "</div>")

    if p["explore"]:
        cards = []
        for x in p["explore"]:
            pct = min(100, round(x["score"] / 9000 * 100))
            bits = []
            if x.get("chest") is not None:
                bits.append(f"宝箱 {x['chest']}")
            if x.get("qiqiao") is not None:
                bits.append(f"蹊跷 {x['qiqiao']}")
            counts = " · ".join(bits)
            cards.append(
                f'<div class="xcard"><div class="xtop"><span class="xn">{esc(x["name"])}</span>'
                f'<span class="xs">{x["score"]}</span><span class="xt">{esc(x["tier"])} {x["lv"]}级</span></div>'
                f'<div class="bar"><i style="width:{pct}%"></i></div>'
                + (f'<div class="xc">{esc(counts)}</div>' if counts else "")
                + "</div>")
        h.append('<div class="sec"><div class="sec-title">区域探索</div>'
                 '<div class="xgrid">' + "".join(cards) + "</div></div>")

    def ecard(k, v, flip=False):
        """装备底色大卡:k=小字说明,v=大字主体;flip=True 时大字在上、小字在下。"""
        if flip:
            return (f'<div class="ecard"><div class="v">{esc(v)}</div>'
                    f'<div class="k" style="margin-bottom:0;margin-top:3px;">{esc(k)}</div></div>')
        return (f'<div class="ecard"><div class="k">{esc(k)}</div>'
                f'<div class="v">{esc(v)}</div></div>')

    if p["suits"] or p["kongfu_main"] or p["kongfu_sub"]:
        cells = [ecard(f"{n}套装 · {c}件", n) for n, c in p["suits"]]
        if p["kongfu_main"]:
            cells.append(ecard("主功法", p["kongfu_main"]))
        if p["kongfu_sub"]:
            cells.append(ecard("副功法", p["kongfu_sub"]))
        h.append('<div class="sec"><div class="sec-title">流派配置</div>'
                 f'<div class="stats">{"".join(cells)}</div></div>')
    if p["xinfa"]:
        cells = []
        for x in p["xinfa"]:
            n, _, rank = x.partition("·")
            cells.append(ecard(rank or "心法", n, flip=True))
        h.append('<div class="sec"><div class="sec-title">心法</div>'
                 f'<div class="stats4">{"".join(cells)}</div></div>')
    if p["qs"]:
        cells = [ecard(f"{r}级" if r else "奇术", n, flip=True) for n, r in p["qs"]]
        h.append('<div class="sec"><div class="sec-title">奇术</div>'
                 f'<div class="stats4">{"".join(cells)}</div></div>')

    return h


def build_profile_html(p: dict) -> str:
    """把 _collect_profile 的数据渲染成完整 HTML(角色档案卡)。"""
    from datetime import datetime

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (f'''<!DOCTYPE html><html><head><meta charset="utf-8"><style>{_STYLE}{_PROFILE_CSS}
</style></head><body><div id="pcard">
<div class="head"><div class="title">角色档案</div><div class="who">{_profile_who(p)}</div></div>
{"".join(_profile_sections(p))}
<div class="foot">数据来源:网易大神 · 生成于 {stamp}</div>
</div></body></html>''')


async def render_profile_image(proxy_data: dict, info_data: dict | None = None,
                               mirror: str = "", auto_install: bool = True) -> bytes | None:
    """渲染「燕云我的」角色档案卡 PNG。失败返回 None,调用方回退文本块。"""
    if not _PW_OK:
        return None
    try:
        p = _collect_profile(proxy_data, info_data)
        if not (p.get("nickname") or p.get("power") is not None):
            return None
        page_html = build_profile_html(p)
    except Exception as e:
        logger.warning(f"[燕云图片] 档案数据收集失败: {type(e).__name__}: {e}")
        return None

    launch_kwargs: dict = {}
    pw = None
    browser = None
    try:
        try:
            try:
                from .yanyun_login import ensure_browser_ready
            except ImportError:
                from yanyun_login import ensure_browser_ready  # type: ignore[no-redef]
            ok, msg, launch_kwargs = await ensure_browser_ready(mirror=mirror, auto_install=auto_install)
            if not ok:
                logger.warning(f"[燕云图片] 无可用浏览器: {msg}")
                return None
        except ImportError:
            launch_kwargs = await _probe_fallback_browser()

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True, **launch_kwargs)
        page = await browser.new_page(viewport={"width": 760, "height": 700}, device_scale_factor=2)
        await page.set_content(page_html, wait_until="load")
        png = await page.locator("#pcard").screenshot(type="png")
        logger.info(f"[燕云图片] 档案卡渲染成功({len(png) // 1024}KB)")
        return png
    except Exception as e:
        logger.warning(f"[燕云图片] 档案卡渲染失败,将回退文本: {type(e).__name__}: {e}")
        return None
    finally:
        for closer in ((lambda: browser and browser.close()), (lambda: pw and pw.stop())):
            try:
                r = closer()
                if r is not None:
                    await r
            except Exception:
                pass


# ============================ 角色档案 + 属性一览 合并卡 ============================

_COMBINED_SPLIT_CSS = """
  #card .sec-split { height: 1px; background: rgba(216,178,106,.3); margin: 20px 0 2px; }
"""


def build_combined_html(p: dict, view: dict, panel_attrs: list | None = None) -> str:
    """角色档案 + 属性一览 合并为单卡(上=档案/探索/流派,下=五维/面板/词条)。"""
    from datetime import datetime

    prof_secs = _profile_sections(p)
    attr_secs = _attrs_sections(view, panel_attrs, include_wuwei=False)
    css = _PROFILE_CSS.replace("#pcard", "#card") + _STYLE + _COMBINED_SPLIT_CSS
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (f'''<!DOCTYPE html><html><head><meta charset="utf-8"><style>{css}
</style></head><body><div id="card">
<div class="head"><div class="title">角色档案</div><div class="who">{_profile_who(p)}</div></div>
{"".join(prof_secs)}
<div class="sec-split"></div>
{"".join(attr_secs)}
<div class="foot">数据来源:网易大神 · 流派推荐文档 · 生成于 {stamp}</div>
</div></body></html>''')


async def render_combined_image(proxy_data: dict, info_data: dict | None = None,
                                mirror: str = "", auto_install: bool = True) -> bytes | None:
    """渲染「档案+属性」合并单卡 PNG。失败返回 None。"""
    if not _PW_OK:
        return None
    try:
        p = _collect_profile(proxy_data, info_data)
        view = _collect_attrs(proxy_data)
        pa = _collect_panel_attrs(info_data)
        if not (p.get("nickname") or view.get("per_equip")):
            return None
        page_html = build_combined_html(p, view, pa)
    except Exception as e:
        logger.warning(f"[燕云图片] 合并卡数据收集失败: {type(e).__name__}: {e}")
        return None

    launch_kwargs: dict = {}
    pw = None
    browser = None
    try:
        try:
            try:
                from .yanyun_login import ensure_browser_ready
            except ImportError:
                from yanyun_login import ensure_browser_ready  # type: ignore[no-redef]
            ok, msg, launch_kwargs = await ensure_browser_ready(mirror=mirror, auto_install=auto_install)
            if not ok:
                logger.warning(f"[燕云图片] 无可用浏览器: {msg}")
                return None
        except ImportError:
            launch_kwargs = await _probe_fallback_browser()

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True, **launch_kwargs)
        page = await browser.new_page(viewport={"width": 760, "height": 900}, device_scale_factor=2)
        await page.set_content(page_html, wait_until="load")
        png = await page.locator("#card").screenshot(type="png")
        logger.info(f"[燕云图片] 合并卡渲染成功({len(png) // 1024}KB)")
        return png
    except Exception as e:
        logger.warning(f"[燕云图片] 合并卡渲染失败: {type(e).__name__}: {e}")
        return None
    finally:
        for closer in ((lambda: browser and browser.close()), (lambda: pw and pw.stop())):
            try:
                r = closer()
                if r is not None:
                    await r
            except Exception:
                pass
