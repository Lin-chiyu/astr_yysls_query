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


RARITY_NAME = {1: "白装", 2: "绿装", 3: "蓝装", 4: "紫装", 5: "金装"}
GOLD_AFFIX_RARITY = 3      # 词条品阶:3=金、2=紫、1=蓝
AFFIX_RARITY_NAME = {0: "灰", 1: "蓝", 2: "紫", 3: "金", 4: "金"}
FULL_AFFIX_COUNT = 6       # 满配词条条数


def _suit_ok(report: dict) -> bool:
    """套装是否凑齐流派推荐(推荐里的每套都要 >=4 件)。"""
    names = re.findall(r"[一-龥]{2,4}(?=四件套)", report.get("suit_rec") or "")
    if not names:
        return True
    return all((report.get("suits") or {}).get(n, 0) >= 4 for n in names)


def _equip_audit(view: dict, suit_rec: str = "") -> dict:
    """装备体检:只挑有问题的装备(等级 / 首词条 / 流派套装)。

    返回 {"rows": [{"slot","name","level","notes"}], "extra": [str], "affix_total": dict|None}。
    rows 只包含有问题的装备;affix_total 只在总词条不满时给出。
    """
    equips = view.get("per_equip") or []
    top_level = max((e.get("level") or 0) for e in equips) if equips else 0
    rec_suits = re.findall(r"[一-龥]{2,4}(?=四件套)", suit_rec or "")
    rows: list[dict] = []
    extra: list[str] = []
    for eq in equips:
        notes: list[str] = []
        lv = eq.get("level")
        if lv and top_level and lv < top_level:
            notes.append(f"更换成{top_level}装备")
        affs = eq.get("affixes") or []
        if affs:
            try:
                first_rarity = int(affs[0][2] or 0)
            except (TypeError, ValueError):
                first_rarity = 0
            if first_rarity < GOLD_AFFIX_RARITY:
                notes.append(f"首词条为{AFFIX_RARITY_NAME.get(first_rarity, '低')} 建议更换装备")
        suit = (eq.get("suit") or "").strip()
        if rec_suits and suit and not any(s in suit or suit in s for s in rec_suits):
            notes.append(f"与推荐流派不符,优先更换成{rec_suits[0]}套")
        if notes:
            rows.append({"slot": eq.get("slot") or "装备", "name": eq.get("name") or "",
                         "level": lv, "notes": notes})

    # 套装件数不够(但每件都没穿错套)时,表格下面补一句
    for s in rec_suits:
        n = sum(1 for e in equips if s and s in (e.get("suit") or ""))
        if n and n < 4:
            extra.append(f"{s}套只有 {n} 件,凑不齐四件套")

    total_have = sum(len(e.get("affixes") or []) for e in equips)
    total_ref = len(equips) * FULL_AFFIX_COUNT
    affix_total = None
    if equips and total_have < total_ref:
        missing = list(dict.fromkeys(
            e.get("slot") or "装备" for e in equips
            if len(e.get("affixes") or []) < FULL_AFFIX_COUNT))
        affix_total = {"have": total_have, "ref": total_ref,
                       "note": f"{'/'.join(missing)}词条未满(优先调律)"}
    return {"rows": rows, "extra": extra, "affix_total": affix_total}



def build_advisor(proxy: dict, info: dict | None = None) -> dict:
    """构造养成分析报告(结构化,供文本块与图片卡共用)。"""
    main_id = _get(proxy, "combat_plan.kongfu.kongfu_main")
    sub_id = _get(proxy, "combat_plan.kongfu.kongfu_sub")
    liupai, lp_key = identify_liupai(main_id, sub_id)
    view = _collect_attrs(proxy)
    counts = {n: c for n, _, c in view["totals"]}
    slots_by: dict[str, list[str]] = {}
    for eq in view["per_equip"]:
        for a in eq["affixes"]:
            lst = slots_by.setdefault(a[0], [])
            if eq["slot"] not in lst:
                lst.append(eq["slot"])

    report = {
        "liupai": liupai, "lp_key": lp_key,
        "nickname": view["nickname"], "level": view["level"],
        "kongfu_main": f"{_map_name('kongfu', main_id)}{_get(proxy, 'combat_plan.kongfu_level.kongfu_main.level')}级",
        "kongfu_sub": f"{_map_name('kongfu', sub_id)}{_get(proxy, 'combat_plan.kongfu_level.kongfu_sub.level')}级",
"suits": {}, "suit_rec": "", "affix_rows": [], "panel_rows": [], "bad_first": [],
        "equip_rows": [], "equip_extra": [], "affix_total_row": None,
    }
    for eq in view["per_equip"]:
        if eq.get("suit"):
            report["suits"][eq["suit"]] = report["suits"].get(eq["suit"], 0) + 1

    doc = "" if liupai == "未知流派" else load_doc(liupai)
    m = re.search(r"\*\*推荐方案\*\*[:：]\s*(.+)", doc)
    if m:
        # 去掉文档里的套装简称注释,例如「倾觞四件套 & 盈盏四件套(套装:「觞」「盈」)」
        report["suit_rec"] = re.sub(r"[（(]套装[:：][^)）]*[)）]", "", m.group(1)).strip()
    report["suit_ok"] = _suit_ok(report)
    audit = _equip_audit(view, report["suit_rec"])
    report["equip_rows"] = audit["rows"]
    report["equip_extra"] = audit["extra"]
    report["affix_total_row"] = audit["affix_total"]
    if liupai == "未知流派":
        return report
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
    non_gold_by_name: dict[str, tuple[int, list[str]]] = {}
    for _eq in view["per_equip"]:
        _slot = _eq.get("slot") or "装备"
        for _a in _eq["affixes"]:
            try:
                if int(_a[2] or 0) < GOLD_AFFIX_RARITY:
                    _n, _slots = non_gold_by_name.get(_a[0], (0, []))
                    if _slot not in _slots:
                        _slots.append(_slot)
                    non_gold_by_name[_a[0]] = (_n + 1, _slots)
            except (TypeError, ValueError):
                pass
    for _r in rows:
        _r.setdefault("slots", slots_by.get(_r["name"], []))
        if not _r["name"].startswith("定音·"):
            _n, _slots = non_gold_by_name.get(_r["name"], (0, []))
            _r["non_gold"] = _n
            _r["non_gold_slots"] = _slots
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


def make_summary(report: dict) -> list[str]:
    """把对标结果写成 2~4 段自然语言(卡片底部的「总结」)。

    只陈述数据结论,不编造;每段一个主题,方便直接读。
    """
    if report.get("liupai") == "未知流派":
        return ["暂时没认出这个流派,先把功法和套装截图发我看看。"]

    paras: list[str] = []

    # ① 装备
    bad = report.get("equip_rows") or []
    extra = report.get("equip_extra") or []
    if bad:
        names = [f"{r['slot']}·{r['name']}" for r in bad[:3]]
        tail = f"等 {len(bad)} 件" if len(bad) > 3 else ""
        notes = {n for r in bad for n in r["notes"]}
        what = []
        if any("级" in n and "更换成" in n for n in notes):
            what.append("等级没跟上")
        if any(n.startswith("首词条为") for n in notes):
            what.append("首词条不是金")
        if any("流派" in n for n in notes):
            what.append("套装配错流派")
        detail = f"({','.join(what)})" if what else ""
        paras.append(f"装备这边{detail}:{'、'.join(names)}{tail}需要换。"
                     + (f"{'；'.join(extra)}。" if extra else ""))
    elif not extra:
        paras.append("装备本身没问题,金装、满词条、等级也齐,不用动。")

    # ② 词条缺口
    gaps = [r for r in report["affix_rows"]
            if isinstance(r.get("ref"), int) and r["ref"] - r["have"] > 0]
    gaps.sort(key=lambda r: -(r["ref"] - r["have"]))
    if gaps:
        seg = "、".join(f"{_short(r['name'])}({r['have']}/{r['ref']})" for r in gaps[:3])
        slots: list[str] = []
        for r in gaps[:6]:
            for s in r.get("slots") or []:
                if s not in slots:
                    slots.append(s)
        where = f",优先动{'/'.join(slots[:4])}" if slots else ""
        paras.append(f"词条最缺的是 {seg},整体还差 {sum(r['ref'] - r['have'] for r in gaps)} 条{where}。")

    # ③ 溢出/达标项
    keep = [r for r in report["panel_rows"] if r["mod"].endswith("率") and str(r["status"]).startswith("+")]
    if keep:
        paras.append("、".join(f"{r['mod']}{r['panel']}(比参考高 {r['status'].lstrip('+')})" for r in keep)
                     + " 已经够了,这几个词条可以拿去换别的。")

    # ④ 非金词条 / 洗练
    non_gold = [r for r in report["affix_rows"] if r.get("non_gold")]
    if non_gold:
        parts = [f"{_short(r['name'])}有 {r['non_gold']} 条不是金(在{'/'.join(r.get('non_gold_slots') or [])})"
                 for r in non_gold[:3]]
        paras.append("；".join(parts) + ",先洗成金的,面板才吃得满。")

    # ⑤ 收尾
    if not bad and not gaps:
        paras.append("面板和词条都在参考线上,后面就是慢慢把词条洗满、等更好的装备。")
    return paras


def _short(name: str) -> str:
    """把长词条名压短,方便塞进句子。"""
    return (name.replace("最小外功攻击", "小外攻击")
                .replace("最大外功攻击", "大外攻击")
                .replace("最小破竹攻击", "小破竹")
                .replace("最大破竹攻击", "大破竹"))


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
        + (f"(该词条非金{r['non_gold']}条,在{'/'.join(r.get('non_gold_slots') or [])})"
           if r.get("non_gold") else "")
        for r in report["affix_rows"])
    tot = report.get("affix_total_row")
    if tot:
        rows = f"总词条 {tot['have']}/{tot['ref']}:{tot['note']}\n" + rows
    panels = "\n".join(f"{r['mod']} 面板{r['panel']}/参考{r['ref']}:{r['status']}({r['note']})"
                       for r in report["panel_rows"])
    suits = "+".join(f"{k}{v}件" for k, v in report["suits"].items())
    bad_equips = report.get("equip_rows") or []
    if bad_equips:
        equips = "\n".join(f"{r['slot']}·{r['name']} {r['level']}级 " + "；".join(r["notes"])
                           for r in bad_equips)
    else:
        equips = "无问题(等级/首词条/流派都合格)"
    if report.get("equip_extra"):
        equips += "\n" + "\n".join(report["equip_extra"])
    return (
        "你是燕云十六声养成顾问。请根据以下对标数据,自行分析并写一段简短的中文养成推荐,"
        "不要编造数据:\n"
        f"流派:{report['liupai']}(当前套装:{suits};流派推荐配装:{report.get('suit_rec') or '无'})\n"
        "如果套装和流派推荐不搭,可以在建议里直接说该凑哪套,或者建议换成哪个更合适的流派。\n"
        f"装备体检:\n{equips}\n"
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

    import unicodedata

    def dw(x):
        return sum(2 if unicodedata.east_asian_width(ch) in "FW" else 1 for ch in str(x))

    def pad(s, width, right=False):
        s = str(s)
        gap = width - dw(s)
        return (s if gap <= 0 else s + " " * gap) if not right else ((" " * gap + s) if gap > 0 else s)
    bad_equips = report.get("equip_rows") or []
    equip_extra = report.get("equip_extra") or []
    if bad_equips or equip_extra:
        title = f"―― 装备体检 · {len(bad_equips)} 件要注意 ――" if bad_equips else "―― 装备体检 ――"
        b = [title, f"{pad('装备', 22)}{pad('等级', 8, True)}  备注"]
        for r in bad_equips:
            label = f"{r['slot']}·{r['name']}"
            b.append(f"{pad(label, 22)}{pad(r['level'] or '—', 8, True)}  {'；'.join(r['notes'])}")
        for s in equip_extra:
            b.append(f"· {s}")
        blocks.append(b)

    if report["affix_rows"]:
        b = ["―― 词条对标 ――", f"{pad('词条', 28)}{pad('面板', 4, True)}{pad('参考', 6, True)}  备注"]
        tot = report.get("affix_total_row")
        if tot:
            b.append(pad("总词条", 28) + pad(tot["have"], 4, True) + pad(str(tot["ref"]), 6, True)
                     + "  " + tot["note"])
        for r in report["affix_rows"]:
            note = r["status"]
            if r.get("non_gold"):
                note += f"(非金{r['non_gold']}条:{'/'.join(r.get('non_gold_slots') or [])})"
            b.append(pad(r["name"], 28) + pad(r["have"], 4, True) + pad(str(r["ref"]), 6, True) + "  " + note)
        blocks.append(b)

    if report["panel_rows"]:
        b = ["―― 战力模块对标 ――", f"{pad('模块', 10)}{pad('面板', 12, True)}{pad('参考', 12, True)}  状态·备注"]
        for r in report["panel_rows"]:
            mark = "+" if r["status"].startswith("+") else ("—" if r["status"] == "—" else ("[NG]" if r["status"] != "达标" else "[OK]"))
            b.append(pad(f"{mark}{r['mod']}", 10) + pad(r["panel"], 12, True) + pad(r["ref"], 12, True) + f"  {r['status']}·{r['note']}")
        blocks.append(b)

    summary = make_summary(report)
    if summary:
        blocks.append(["―― 养成方向 ――"] + list(summary))
    blocks.append(["―― 小结 ――", rule_summary(report)])
    return ["\n".join(b) for b in blocks if b]
