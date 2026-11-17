"""流派养成对标引擎:用打包的流派推荐文档(gamedata/liupai_docs/)对标角色实时数据。

产出结构化报告(供文本块与图片卡共用):
- 词条数量对标:参考行(神力合并)/ 定音行(按部位定音推荐判定)/ 未收录行(带部位)
- 战力模块对标:roleInfo 面板值 vs 文档参考值
- 养成方向 + AI 提示词(规则生成;线上大模型只做润色,非必需)

备注规则(依据 五维属性说明.md):
- 流派参考词条:缺N条(优先补足)/ 可保留 / 可保留(多N条可洗)
- 五维:流派核心可保留;劲=过渡词条可优化;御/体=不需要优先更换;势/敏与流派不符=优先更换
- 定音:达标 / 建议洗成;其他未收录:可更换
"""

from __future__ import annotations

import json
import re
from pathlib import Path

try:  # 插件包内相对导入;独立回归平铺导入
    from .render import _collect_attrs, _get, _load_mappings, _map_name
except ImportError:
    from render import _collect_attrs, _get, _load_mappings, _map_name  # type: ignore[no-redef]

DOCS_DIR = Path(__file__).resolve().parent / "gamedata" / "liupai_docs"

AFFIX_ALIAS = {
    "小外": "最小外功攻击", "大外": "最大外功攻击", "会心": "会心率",
    "精准": "精准率", "会意": "会意率", "劲": "劲", "敏": "敏", "势": "势",
    "体": "体", "御": "御",
}
SLOT_ALIAS = {"主武器": "武器", "副武器": "武器", "腔甲": "胫甲"}
PANEL_FIELD = {
    "最小外功": ("MIN_W_ATK", ""), "最大外功": ("MAX_W_ATK", ""),
    "精准率": ("REAL_ACR_PROB", "%"), "会心率": ("REAL_CRI_PROB", "%"), "会意率": ("REAL_BASH_PROB", "%"),
}
TONE_ALIAS = {"外攻穿透": "外功穿透"}  # 文档定音名 → 存档词条名
# 五维判定(依据 五维属性说明.md):流派类型 → 核心五维
WUXEI_CORE = {"会心": "敏", "会意": "势"}
WUXEI_NOTE = {
    "劲": "过渡词条,后续可优化成最小外功攻击/敏",
    "御": "防御向,当前流派不需要,优先更换",
    "体": "生存向,当前流派不需要,优先更换",
}


def load_doc(name: str) -> str:
    return (DOCS_DIR / f"{name}.md").read_text("utf-8")


def parse_tables(text: str, section: str) -> list[list[str]]:
    """取指定标题下表格的行(按 | 切列),自动跳过表头与分隔行。"""
    m = re.search(rf"###?[^\n]*{section}[^\n]*\n(.*?)(?=\n## |\n### |\Z)", text, re.S)
    if not m:
        return []
    rows = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if line.startswith("|") and not re.match(r"^\|[\s:\-|]+\|$", line):
            rows.append([c.strip() for c in line.strip("|").split("|")])
    return rows


def identify_liupai(main_id, sub_id) -> tuple[str, str]:
    """主副功法 ID → 流派名(键双向匹配,存档主副顺序与文档键可能相反)。"""
    lp = _load_mappings().get("liupai") or {}
    for key in (f"{main_id}-{sub_id}", f"{sub_id}-{main_id}"):
        if key in lp:
            return lp[key]["name"], key
    return "未知流派", ""


def parse_tone_rec(doc: str) -> dict[str, str]:
    """解析部位推荐表的定音推荐列:槽位名 → 文档原始定音名。"""
    out = {}
    for row in parse_tables(doc, "部位推荐词条"):
        if row[0] == "部位" or len(row) < 4:
            continue
        for part in row[0].split("/"):
            out[SLOT_ALIAS.get(part.strip(), part.strip())] = row[3]
    return out


def _tone_by_slot(proxy: dict) -> dict[str, str]:
    """各槽位的定音词条名(存档 ex.tone_determin → affix 名称),键为槽位名。"""
    affix_map = _load_mappings().get("affix") or {}
    equips = _get(proxy, "combat_plan.wear_equips") or {}
    out: dict[str, str] = {}
    for slot_no, item in equips.items():
        ex = (item or {}).get("ex") or {}
        tid = ex.get("tone_determin")
        if tid is None:
            continue
        info = affix_map.get(str(tid)) or {}
        out[_map_name("slot", slot_no) or f"槽{slot_no}"] = info.get("name") or f"定音{tid}"
    return out


def build_advisor(proxy: dict, info: dict | None = None) -> dict:
    """构造养成分析报告(结构化,供文本块与图片卡共用)。"""
    main_id = _get(proxy, "combat_plan.kongfu.kongfu_main")
    sub_id = _get(proxy, "combat_plan.kongfu.kongfu_sub")
    liupai, lp_key = identify_liupai(main_id, sub_id)
    view = _collect_attrs(proxy)
    counts = {n: c for n, _, c in view["totals"]}
    slots_by: dict[str, list[str]] = {}
    for eq in view["per_equip"]:
        for an, _ in eq["affixes"]:
            lst = slots_by.setdefault(an, [])
            if eq["slot"] not in lst:
                lst.append(eq["slot"])

    report = {
        "liupai": liupai, "lp_key": lp_key,
        "nickname": view["nickname"], "level": view["level"],
        "kongfu_main": f"{_map_name('kongfu', main_id)}{_get(proxy, 'combat_plan.kongfu_level.kongfu_main.level')}级",
        "kongfu_sub": f"{_map_name('kongfu', sub_id)}{_get(proxy, 'combat_plan.kongfu_level.kongfu_sub.level')}级",
        "suits": {}, "suit_rec": "", "affix_rows": [], "panel_rows": [], "bad_first": [],
    }
    for eq in view["per_equip"]:
        if eq.get("suit"):
            report["suits"][eq["suit"]] = report["suits"].get(eq["suit"], 0) + 1

    if liupai == "未知流派":
        return report
    doc = load_doc(liupai)
    m = re.search(r"\*\*推荐方案\*\*[:：]\s*(.+)", doc)
    if m:
        report["suit_rec"] = m.group(1).strip()
    wuxei_type = "会意" if "会意流派" in doc else "会心"  # 会意流=鸣金系(文档明示),其余按会心

    # 部位首词条(不在报告单独展示,仅用于养成方向提示)
    slot_first = {eq["slot"]: eq["affixes"][0][0] for eq in view["per_equip"] if eq["affixes"]}
    for row in parse_tables(doc, "部位推荐词条"):
        if row[0] == "部位" or len(row) < 2:
            continue
        for part in row[0].split("/"):
            slot = SLOT_ALIAS.get(part.strip(), part.strip())
            cur = slot_first.get(slot)
            rec = [AFFIX_ALIAS.get(x.strip(), x.strip()) for x in row[1].split("/")]
            if cur is not None and cur not in rec:
                report["bad_first"].append({"slot": slot, "rec": row[1], "cur": cur})

    # ---- 词条数量对标 ----
    rows: list[dict] = []
    matched: set[str] = set()
    parsed = []
    for row in parse_tables(doc, "词条数量参考"):
        if row[0] == "词条属性":
            continue
        parsed.append((row[0], int(re.search(r"\d+", row[1]).group())))

    def fuzzy_have(kw: str) -> int:
        return sum(c for n, c in counts.items() if kw in n or n in kw)

    have_of = {}
    for name_raw, want in parsed:
        mm = re.match(r"(.+?)\((.+)\)$", name_raw)
        have_of[name_raw] = (sum(fuzzy_have(x.strip()) for x in mm.group(2).split("/")) if mm
                             else counts.get(name_raw, 0))
    max_gap = max((w - have_of[n] for n, w in parsed if w - have_of[n] > 0), default=0)

    for name_raw, want in parsed:
        mm = re.match(r"(.+?)\((.+)\)$", name_raw)
        if mm:  # 神力聚合 → 合并一行
            label, subs = mm.group(1), [x.strip() for x in mm.group(2).split("/")]
            have = sum(fuzzy_have(x) for x in subs)
            for sub in subs:
                for n in counts:
                    if sub in n or n in sub:
                        matched.add(n)
            gap = want - have
            missing = [s for s in subs if fuzzy_have(s) == 0]
            note = f"差{gap},缺{'/'.join(missing)}" if gap > 0 and missing else (f"差{gap}" if gap > 0 else "达标")
            rows.append({"name": label, "have": have, "ref": want, "status": note, "kind": "ref"})
            continue
        have = counts.get(name_raw, 0)
        matched.add(name_raw)
        gap = want - have
        if gap > 0:
            status = f"缺{gap}条" + ("(优先补足)" if gap == max_gap else "")
        elif gap < 0:
            status = f"可保留(多{-gap}条可洗)"
        else:
            status = "可保留"
        rows.append({"name": name_raw, "have": have, "ref": want, "status": status, "kind": "ref"})

    # 定音词条行:判断各部位定音是否符合推荐
    tone_rec = parse_tone_rec(doc)
    tone_by_slot = _tone_by_slot(proxy)
    tone_names: dict[str, list[str]] = {}
    for slot, name in tone_by_slot.items():
        tone_names.setdefault(name, []).append(slot)
    for name, slots in sorted(tone_names.items(), key=lambda kv: -len(kv[1])):
        have = counts.get(name, 0)
        bad = []
        for s in slots:
            rec = tone_rec.get(s)
            if not rec:
                continue  # 文档未覆盖该部位的定音,不评判
            rec = TONE_ALIAS.get(rec, rec)
            if rec != name and rec not in name and name not in rec:
                bad.append((s, tone_rec[s]))
        note = ("达标" if not bad
                else "建议洗成:" + "、".join(f"{s}→{rec}" for s, rec in bad))
        rows.append({"name": f"定音·{name}", "have": have, "ref": "—",
                     "status": note, "kind": "dingyin_ok" if not bad else "dingyin_bad"})

    # 未收录词条(身上有、参考清单没有,且非定音):五维规则 + 简短备注(带部位)
    extra = [(n, c) for n, c in counts.items() if n not in matched and n not in tone_names]
    core = WUXEI_CORE[wuxei_type]
    for n, c in sorted(extra, key=lambda kv: -kv[1]):
        if n == core:
            status = "流派核心属性,可保留"
        elif n in WUXEI_NOTE:
            status = WUXEI_NOTE[n]
        elif wuxei_type == "会心" and n == "势":
            status = "会意流核心,当前流派不需要,优先更换"
        elif wuxei_type == "会意" and n == "敏":
            status = "会心流核心,当前流派不需要,优先更换"
        elif n == "最大外功攻击" and fuzzy_have("小外") and "小外" in doc:
            status = "流派走小外,优先洗成最小外功攻击"
        else:
            status = "可更换"
        slots = slots_by.get(n, [])
        if slots:
            status += f"({'/'.join(slots)})"
        rows.append({"name": n, "have": c, "ref": "—", "status": status, "kind": "extra", "slots": slots})
    report["affix_rows"] = rows

    # ---- 战力模块 ----
    info = info or {}
    for row in parse_tables(doc, "战力模块参考"):
        if row[0] == "战力模块":
            continue
        mod, ref = row[0], row[1]
        if mod == "属性攻击":
            got = sorted(
                (float(info.get(f"MIN_PRO_ATK_{e}") or 0), float(info.get(f"MAX_PRO_ATK_{e}") or 0))
                for e in "ABCE" if info.get(f"MAX_PRO_ATK_{e}")
            )
            if got:
                lo, hi = got[-1]
                mm = re.match(r"(\d+)~(\d+)", ref)
                rlo, rhi = int(mm.group(1)), int(mm.group(2))
                mark = "达标" if lo >= rlo and hi >= rhi else (
                    f"上限差{rhi - hi:g}" if lo >= rlo else f"下限差{rlo - lo:g},上限差{rhi - hi:g}")
                note = "达标" if mark == "达标" else "随强化/定音提升"
                report["panel_rows"].append({"mod": mod, "panel": f"{lo:g}~{hi:g}", "ref": ref, "status": mark, "note": note})
        elif mod in PANEL_FIELD:
            field, unit = PANEL_FIELD[mod]
            cur = float(info.get(field) or 0)
            refv = float(re.search(r"[\d.]+", ref).group())
            diff = cur - refv
            mark = f"+{diff:g}" if diff >= 0 else f"差{-diff:g}"
            note = ("已溢出,词条可让位" if diff > 15 and mod.endswith("率") else "达标") if diff >= 0 else "接近达标,词条调整后可到位"
            report["panel_rows"].append({"mod": mod, "panel": f"{cur:g}{unit}", "ref": ref, "status": mark, "note": note})
        # 其余模块(如特殊增伤)接口无对应字段,不生成行
    return report


def make_directions(report: dict) -> list[str]:
    """规则化生成养成方向(按优先级)。"""
    dirs: list[str] = []
    for r in report.get("bad_first", []):
        dirs.append(f"{r['slot']}位首词条换成 {r['rec']}(现为 {r['cur']})")
    gaps = [(("神力增伤类" if r["name"].startswith("神力") else r["name"]), r["ref"] - r["have"])
            for r in report["affix_rows"] if isinstance(r["ref"], int) and r["ref"] - r["have"] > 0]
    gaps.sort(key=lambda kv: -kv[1])
    if gaps:
        top = "、".join(f"{n}差{g}" for n, g in gaps[:3])
        dirs.append(f"词条优先补:{top}" + ("等" if len(gaps) > 3 else ""))
    # 位置化调整建议(哪些部位的哪些词条要动)
    slot_issues: dict[str, list[str]] = {}
    for r in report["affix_rows"]:
        if r["kind"] != "extra" or "过渡词条" in r["status"]:
            continue
        for s in r.get("slots", []):
            lst = slot_issues.setdefault(s, [])
            if r["name"] not in lst:
                lst.append(r["name"])
    if slot_issues:
        items = "、".join(f"{s}的{'/'.join(ns)}" for s, ns in slot_issues.items())
        dirs.append(f"优先调整:{items},换成流派推荐词条")
    overflow = [f"{r['name']}多{r['have'] - r['ref']}条" for r in report["affix_rows"]
                if isinstance(r["ref"], int) and r["ref"] - r["have"] < 0 and r["kind"] == "ref"]
    washable = [f"{r['name']}{r['have']}条" for r in report["affix_rows"]
                if r["kind"] == "extra" and any(k in r["status"] for k in ("优先更换", "可更换", "洗成", "过渡词条"))]
    parts = overflow + washable
    if parts:
        need = sum(g for _, g in gaps)
        have_total = sum(int("".join(filter(str.isdigit, p.split("条")[0]))) for p in parts if "条" in p)
        verdict = "足够覆盖缺口" if have_total >= need else f"可洗{have_total}条<缺口{need}条,部分需重新调律"
        dirs.append(f"可洗来源:{'、'.join(parts[:5])}{'等' if len(parts) > 5 else ''},{verdict}")
    for r in report["affix_rows"]:
        if r["name"] == "神力词条" and isinstance(r["ref"], int) and r["ref"] - r["have"] > 0:
            dirs.append("补神力增伤类:优先首领增伤(现0条)")
    for r in report["panel_rows"]:
        if r["status"] not in ("达标", "—") and not r["status"].startswith("+"):
            dirs.append(f"{r['mod']}{r['status']},{r['note']}")
    return dirs


def rule_summary(report: dict) -> str:
    """规则版简短分析(无大模型时的兜底)。"""
    liupai = report["liupai"]
    suits = report["suits"]
    suit_ok = bool(report["suit_rec"]) and all(
        suits.get(n, 0) >= 4 for n in re.findall(r"[一-龥]{2,4}(?=四件套)", report["suit_rec"]))
    panel_ok = sum(1 for r in report["panel_rows"] if r["status"].startswith("+") or r["status"] == "达标")
    gaps = [(r["name"], r["ref"] - r["have"]) for r in report["affix_rows"]
            if isinstance(r["ref"], int) and r["ref"] - r["have"] > 0]
    gaps.sort(key=lambda kv: -kv[1])
    overflow = [(r["name"], r["have"] - r["ref"]) for r in report["affix_rows"]
                if isinstance(r["ref"], int) and r["ref"] - r["have"] < 0]
    wash_total = sum(r["have"] for r in report["affix_rows"] if r["kind"] == "extra") + sum(g for _, g in overflow)
    need = sum(g for _, g in gaps)
    if suit_ok:
        p1 = f"{liupai}基础功底{'扎实' if panel_ok >= 4 else '尚可'}:套装与推荐一致,双外攻面板双双超标,精准率封顶,定音全部对路。"
    else:
        p1 = f"{liupai}套装尚未符合推荐({report['suit_rec']}),优先凑齐套装再调词条。"
    if gaps:
        top = "、".join(f"{n}缺{g}条" for n, g in gaps[:2])
        p2 = f"短板集中在词条结构——{top}"
        if any(r["name"] == "神力词条" and r["ref"] - r["have"] > 0 for r in report["affix_rows"]):
            p2 += ",首领增伤空缺"
        p2 += f";{'好在' if wash_total >= need else '注意'}身上可洗词条共{wash_total}条,{'刚好够补缺口' if wash_total >= need else '不足以完全覆盖缺口'}。"
    else:
        p2 = "词条结构已全部达标。"
    bad_panel = [r for r in report["panel_rows"] if r["status"] not in ("达标", "—") and not r["status"].startswith("+")]
    p4 = ("会意率、属性攻击的剩余差距交给调律和强化慢慢追,整体成型度很高。" if bad_panel
          else "面板与词条已全面达标,可以挑战更高阶内容。")
    return p1 + p2 + p4


def make_ai_prompt(report: dict) -> str:
    """喂给大模型的提示词(线上用配置的 LLM 自由分析;规则版 rule_summary 为兜底)。"""
    rows = "\n".join(
        f"{r['name']}" + (f"(在{'/'.join(r['slots'])})" if r.get("slots") else "")
        + f" 面板{r['have']}/参考{r['ref']}:{r['status']}"
        for r in report["affix_rows"])
    panels = "\n".join(f"{r['mod']} 面板{r['panel']}/参考{r['ref']}:{r['status']}({r['note']})"
                       for r in report["panel_rows"])
    suits = "+".join(f"{k}{v}件" for k, v in report["suits"].items())
    return (
        "你是燕云十六声养成顾问。请根据以下对标数据,自行分析并写一段简短的中文养成推荐,"
        "不要编造数据:\n"
        f"流派:{report['liupai']}(套装:{suits})\n"
        f"词条对标:\n{rows}\n战力模块:\n{panels}"
    )


def render_text(report: dict) -> list[str]:
    """渲染成文本块(每块一条合并转发卡片;图片发送失败时的回退)。"""
    blocks: list[list[str]] = []
    suits = "+".join(f"{k}{v}件" for k, v in report["suits"].items())
    head = [f"―― 养成分析 · {report['liupai']} ――",
            f"功法: 主 {report['kongfu_main']} · 副 {report['kongfu_sub']}"]
    if report["suit_rec"]:
        ok = all(report["suits"].get(n, 0) >= 4 for n in re.findall(r"[一-龥]{2,4}(?=四件套)", report["suit_rec"]))
        head.append(f"套装: {suits}" + ("(与推荐一致)" if ok else f"(推荐: {report['suit_rec']})"))
    blocks.append(head)

    if report["affix_rows"]:
        import unicodedata

        def dw(x):
            return sum(2 if unicodedata.east_asian_width(ch) in "FW" else 1 for ch in str(x))

        def pad(s, width, right=False):
            s = str(s)
            gap = width - dw(s)
            return (s if gap <= 0 else s + " " * gap) if not right else ((" " * gap + s) if gap > 0 else s)

        b = ["―― 词条数量对标 ――", f"{pad('词条', 28)}{pad('面板', 4, True)}{pad('参考', 6, True)}  备注"]
        for r in report["affix_rows"]:
            b.append(pad(r["name"], 28) + pad(r["have"], 4, True) + pad(str(r["ref"]), 6, True) + "  " + r["status"])
        blocks.append(b)

    if report["panel_rows"]:
        b = ["―― 战力模块对标 ――", f"{pad('模块', 10)}{pad('面板', 12, True)}{pad('参考', 12, True)}  状态·备注"]
        for r in report["panel_rows"]:
            mark = "+" if r["status"].startswith("+") else ("—" if r["status"] == "—" else ("[NG]" if r["status"] != "达标" else "[OK]"))
            b.append(pad(f"{mark}{r['mod']}", 10) + pad(r["panel"], 12, True) + pad(r["ref"], 12, True) + f"  {r['status']}·{r['note']}")
        blocks.append(b)

    dirs = make_directions(report)
    if dirs:
        blocks.append(["―― 养成方向 ――"] + [f"{i}. {d}" for i, d in enumerate(dirs, 1)])
    blocks.append(["―― 小结 ――", rule_summary(report)])
    return ["\n".join(b) for b in blocks if b]
