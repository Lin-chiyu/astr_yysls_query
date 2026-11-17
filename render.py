"""纯文本渲染层 — 按 2026-11-17 实测的响应结构编写。

proxyGameRole 的 data 是扁平 dict(键名自带点号,如 "base.nickname");
roleInfo 的 data 是普通嵌套对象;queryPvp 的 data 是数组。
未知结构统一降级到通用摘要渲染,保证任何响应都有可读输出。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# 游戏配置映射表(来源:大神生涯页静态配置 s.166.net/config/ds_h72/,随插件打包)
GAMEDATA_DIR = Path(__file__).resolve().parent / "gamedata"
GAME_FILES = {
    "school": "school_data.json",
    "xinfa": "xinfa_data.json",
    "kongfu": "kongfu_data.json",
    "equip": "equip_data.json",
    "slot": "slot_data.json",
    "attr": "attr_data.json",
    "qs": "qs_data.json",
    "suffix": "suffix_data.json",
    "affix": "affix_data.json",
    "data_report": "data_report_h72.json",
    "liupai": "liupai_data.json",
}
_mappings_cache: dict[str, dict] | None = None


def _load_mappings() -> dict[str, dict]:
    global _mappings_cache
    if _mappings_cache is None:
        m: dict[str, dict] = {}
        if GAMEDATA_DIR.is_dir():
            for name, filename in GAME_FILES.items():
                p = GAMEDATA_DIR / filename
                if p.exists():
                    try:
                        m[name] = json.loads(p.read_text("utf-8"))
                    except Exception:
                        m[name] = {}
        _mappings_cache = m
    return _mappings_cache


def _map_name(table: str, key: Any) -> str | None:
    """从映射表取名称,键可能是 '701' 或 701。"""
    t = _load_mappings().get(table) or {}
    v = t.get(str(key)) or t.get(key) if not isinstance(t, dict) else t.get(str(key))
    if v is None and isinstance(key, int):
        v = t.get(str(key))
    if isinstance(v, dict) and "name" in v:
        return v["name"]
    return None


def _fmt_ts(value: Any) -> str:
    """unix 秒(可能带小数)→ 可读时间。"""
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value)


def _fmt_duration(seconds: Any) -> str:
    """秒 → 小时计数(保留一位小数)。"""
    try:
        return f"{float(seconds) / 3600:.1f} 小时"
    except (TypeError, ValueError):
        return str(seconds)


def _get(data: dict, dotted_key: str, default: Any = None) -> Any:
    """proxyGameRole 的键名自带点号,直接当字面键取;取不到再按路径逐层取。"""
    if dotted_key in data:
        return data[dotted_key]
    cur: Any = data
    for part in dotted_key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def render_proxy_role(resp: dict[str, Any], title: str, info_data: dict | None = None,
                      as_blocks: bool = False) -> str | list[str]:
    """渲染 proxyGameRole 的全量角色档案。

    info_data: roleInfo 接口的 data(可选),提供时附加区域收集(chestByRegion 等)。
    as_blocks: True 时返回区块列表(list[str],供合并转发卡片使用);False 返回完整文本。
    """
    data = resp.get("data") or {}
    blocks: list[list[str]] = []

    base = [f"【{title}】"]
    nickname = _get(data, "base.nickname")
    if nickname:
        base.append(f"角色名: {nickname}")

    pairs = [
        ("base.level", "等级"),
        ("base.max_xiuwei_kungfu", "最高功力"),
        ("fashion.score", "时装收集分"),
    ]
    for key, label in pairs:
        v = _get(data, key)
        if v not in (None, ""):
            base.append(f"{label}: {v}")

    school_id = _get(data, "base.school")
    school_name = _map_name("school", school_id) if school_id is not None else None
    base.append(f"门派: {school_name or school_id or '未知'}")

    create = _get(data, "base.create_time")
    if create:
        base.append(f"建号时间: {_fmt_ts(create)}")
    login = _get(data, "base.login_time")
    if login:
        base.append(f"最近登录: {_fmt_ts(login)}")
    online = _get(data, "base.online_time")
    if online:
        base.append(f"累计在线: {_fmt_duration(online)}")

    ach = _get(data, "achievement.quantity")
    if isinstance(ach, dict):
        parts = [f"{k}档×{v}" for k, v in sorted(ach.items())]
        base.append("成就: " + ", ".join(parts))

    titles = _get(data, "title_prop.unlock_titles")
    if isinstance(titles, dict):
        for v in titles.values():
            if isinstance(v, list):
                base.append(f"已解锁称号: {len(v)}个")
                break

    guise = _get(data, "guise.collection_progress")
    if isinstance(guise, dict) and guise:
        parts = []
        for k, v in guise.items():
            if isinstance(v, dict) and "all" in v and "cnt" in v:
                parts.append(f"{k}类 {v['cnt']}/{v['all']}")
            else:
                parts.append(f"{k}:{v}")
        base.append("外观收集: " + ", ".join(parts))
    blocks.append(base)

    explore = _render_explore(data, info_data)
    if explore:
        blocks.append(explore)

    # ---- 流派配置(套装/功法/心法/装备/奇术) ----
    build_blocks, _skipped = _render_build(data)
    blocks.extend(b for b in build_blocks if b)

    if as_blocks:
        return ["\n".join(b) for b in blocks if b]
    return "\n".join("\n".join(b) for b in blocks)


# 射决(槽9)/ 弓(槽21):弓箭类装备,明细与合并汇总均不展示(用户最终决定整块去掉)
ARCHER_SKIP_SLOTS = frozenset({"9", "21"})


# 「合并汇总」词条展示顺序(用户指定清单);清单外词条按数值降序排在清单之后
AFFIX_SUMMARY_ORDER = (
    "体", "劲", "御", "敏", "势",
    "最小外功攻击", "最大外功攻击", "最小破竹攻击", "最大破竹攻击",
    "最小裂石攻击", "最大裂石攻击", "最小鸣金攻击", "最大鸣金攻击",
    "最小牵丝攻击", "最大牵丝攻击", "最小无相攻击", "最大无相攻击",
    "气血最大值", "外功防御",
    "精准率", "会心率", "实际会心率", "会意率", "实际会意率",
    "剑增伤", "枪增伤", "伞增伤", "扇增伤", "鼓增伤",
    "手甲增伤", "双刀增伤", "绳镖增伤", "陌刀武学增伤", "横刀武学增伤",
    "全武学增伤", "对首领单位增伤", "对玩家单位增效", "全奇术增伤",
)


def _affix_norm(name: str) -> str:
    """归一化:去掉 增伤/增效/武学增伤 类尾缀,便于「手甲武学增伤↔手甲增伤」这类匹配。"""
    for suf in ("武学增伤", "武学增效", "增伤", "增效"):
        if name.endswith(suf) and len(name) > len(suf):
            return name[: -len(suf)]
    return name


def _summary_order(name: str) -> int:
    """清单内按位置返回,清单外返回清单长度(排最后)。"""
    if name in AFFIX_SUMMARY_ORDER:
        return AFFIX_SUMMARY_ORDER.index(name)
    n = _affix_norm(name)
    for i, kw in enumerate(AFFIX_SUMMARY_ORDER):
        k = _affix_norm(kw)
        if n == k or n.startswith(k) or k.startswith(n):
            return i
    return len(AFFIX_SUMMARY_ORDER)


def _collect_attrs(data: dict) -> dict:
    """把扁平存档整理成「属性一览」视图数据,文本渲染与图片渲染共用。

    词条按槽位号升序逐件列出,再按名称合并;合并项带出现条数
    (如 最小外功攻击 合计 1086.7 / 8条)。百分比词条按游戏 unit 格式串
    ({0:.1%})口径 ×100 显示(0.162 → +16.2%)。
    射决/弓(ARCHER_SKIP_SLOTS)明细与汇总均不展示。

    返回 {nickname, level(人物等级), wuwei, per_equip: [{slot, slot_no, name, level(装备等级), suit, affixes}],
          totals(不含弓箭类), affix_total(展示词条总条数)};无任何数据时各列表为空。
    """
    nickname = str(_get(data, "base.nickname") or "").strip()
    level = _get(data, "base.level")

    wuwei: list[tuple[str, Any]] = []
    for k, v in data.items():
        if isinstance(k, str) and k.startswith("real_attr."):
            sub = k.split(".", 1)[1]
            if sub == "XIUWEI_KUNGFU":
                continue  # 功力已在档案区单独展示
            try:
                fv = float(v)
                v = int(fv) if fv.is_integer() else round(fv, 1)
            except (TypeError, ValueError):
                pass
            wuwei.append((_map_name("attr", sub) or sub, v))

    affix_map = _load_mappings().get("affix") or {}
    equip_map = _load_mappings().get("equip") or {}

    totals: dict[tuple[str, bool], float] = {}
    counts: dict[tuple[str, bool], int] = {}
    per_equip: list[dict] = []
    equips = _get(data, "combat_plan.wear_equips")
    if isinstance(equips, dict):

        def _slot_order(kv: tuple[Any, Any]) -> tuple[int, Any]:
            s = str(kv[0])
            return (0, int(s)) if s.isdigit() else (1, s)

        for slot_no, item in sorted(equips.items(), key=_slot_order):
            if not isinstance(item, dict):
                continue
            ex = (item or {}).get("ex") or {}
            affs = ex.get("base_affixes") or []
            if not affs:
                continue  # 占位装备无词条,不计入
            eq_name = _map_name("equip", item.get("No")) or f"装备{item.get('No')}"
            slot_label = _map_name("slot", slot_no) or f"槽{slot_no}"
            eq_info = equip_map.get(str(item.get("No"))) or {}
            equip_level = eq_info.get("level") if isinstance(eq_info, dict) else None
            suffix_id = ex.get("suffix")
            suit = _map_name("suffix", suffix_id) if suffix_id is not None else None
            if str(slot_no) in ARCHER_SKIP_SLOTS:
                continue  # 弓箭类整块不展示(变量名勿用 level,避免遮蔽人物等级)
            item_affixes: list[tuple[str, str, int, float, bool]] = []
            for aff in affs:
                try:
                    aid, val = aff[0], float(aff[1])
                except (IndexError, TypeError, ValueError):
                    continue
                # base_affixes 结构:[词条ID, 数值, 洗练进度0~1, 品阶, 是否推荐]
                try:
                    rarity = int(aff[3])
                except (IndexError, TypeError, ValueError):
                    rarity = 1
                try:
                    progress = float(aff[2])
                except (IndexError, TypeError, ValueError):
                    progress = 1.0
                progress = min(1.0, max(0.0, progress))
                recommended = bool(aff[4]) if len(aff) > 4 else False
                info = affix_map.get(str(aid)) or {}
                name = info.get("name") or f"词条{aid}"
                unit = info.get("unit") or ""
                is_percent = "%" in unit
                key = (name, is_percent)
                totals[key] = totals.get(key, 0.0) + val
                counts[key] = counts.get(key, 0) + 1
                if is_percent:
                    disp = f"+{val * 100:.1f}%"
                else:
                    v = int(val) if float(val).is_integer() else round(val, 1)
                    disp = f"+{v}"
                item_affixes.append((name, disp, rarity, progress, recommended))
            if item_affixes:
                per_equip.append(
                    {"slot": slot_label, "slot_no": str(slot_no), "name": eq_name, "level": equip_level, "suit": suit, "affixes": item_affixes}
                )

    total_entries: list[tuple[str, str, int, float]] = []
    for (name, is_p), val in totals.items():
        if is_p:
            disp = f"+{val * 100:.1f}%"
        else:
            v = int(val) if float(val).is_integer() else round(val, 1)
            disp = f"+{v}"
        total_entries.append((name, disp, counts[(name, is_p)], abs(val)))
    total_entries.sort(key=lambda e: (_summary_order(e[0]), -e[3]))
    total_rows = [(n, d, c) for n, d, c, _ in total_entries]

    return {
        "nickname": nickname,
        "level": level,
        "wuwei": wuwei,
        "per_equip": per_equip,
        "totals": total_rows,
        "affix_total": sum(len(e["affixes"]) for e in per_equip),
    }


def _equip_tag(eq: dict) -> str:
    """装备头部的套装/等级标注,如 (倾觞·115级);缺啥省啥,全缺返回空串。"""
    parts = []
    if eq.get("suit"):
        parts.append(str(eq["suit"]))
    if eq.get("level") not in (None, ""):
        parts.append(f"{eq['level']}级")
    return f"({('·'.join(parts))})" if parts else ""


def _render_attrs(data: dict, as_blocks: bool = False) -> list[str] | str:
    """属性一览(文本版):五维 + 装备词条(按装备分列 + 合并汇总带条数)。

    数据来源见 _collect_attrs。as_blocks=True 时返回 [属性一览整块](供合并转发);
    False 返回行列表(兼容旧调用)。
    """
    view = _collect_attrs(data)

    lines: list[str] = []
    if view["wuwei"]:
        lines.append("五维: " + ", ".join(f"{n} {v}" for n, v in view["wuwei"]))

    if view["per_equip"]:
        lines.append(f"装备词条(按装备,共{view['affix_total']}条):")
        for eq in view["per_equip"]:
            lines.append(f"  {eq['slot']}·{eq['name']}{_equip_tag(eq)}")
            lines.extend(f"    {a[0]} {a[1]}" for a in eq["affixes"])

    if view["totals"]:
        lines.append("装备词条汇总(不含弓箭类):")
        lines.extend(f"  {n} {d} {c}条" for n, d, c in view["totals"])

    if as_blocks:
        if lines:
            return ["―― 属性一览 ――"] + lines
        return []
    return lines


def _render_build(data: dict) -> tuple[list[str], list[str]]:
    """渲染流派配置:套装汇总、主副功法、心法、佩戴装备、奇术。

    返回 (区块列表, 被过滤的占位装备数)。占位装备 = 物品编号不在 equip_data
    映射表且无任何词条数据(如槽105 的 No=2402000,游戏存档占位)。
    区块划分:[流派配置] [装备明细] [奇术]。
    """
    blocks: list[list[str]] = []
    skipped_placeholders = 0

    # 套装汇总(ex.suffix → suffix_data 名称,按件数降序)
    equips = _get(data, "combat_plan.wear_equips")
    equip_map = _load_mappings().get("equip") or {}
    suit_count: dict[str, list[str]] = {}
    if isinstance(equips, dict) and equips:
        for slot_no, item in equips.items():
            if not isinstance(item, dict):
                continue
            no = item.get("No")
            # 占位装备:映射表无此编号且无词条数据 → 不计入
            if str(no) not in equip_map and not (item.get("ex") or {}):
                skipped_placeholders += 1
                continue
            suffix_id = (item.get("ex") or {}).get("suffix")
            sname = _map_name("suffix", suffix_id) if suffix_id is not None else None
            suit_count.setdefault(sname or "暂未收录", []).append(str(slot_no))

    config_block: list[str] = []
    if suit_count:
        parts = []
        for sname, slots in sorted(suit_count.items(), key=lambda kv: -len(kv[1])):
            parts.append(f"{sname} {len(slots)}件")
        config_block.append("套装: " + " | ".join(parts))

    # 主副功法(名称 + 等级)
    for slot, label in (("kongfu_main", "主功法"), ("kongfu_sub", "副功法")):
        kid = _get(data, f"combat_plan.kongfu.{slot}")
        kname = _map_name("kongfu", kid) if kid is not None else None
        lv = _get(data, f"combat_plan.kongfu_level.{slot}.level")
        if kid is not None:
            parts = [kname or f"未知功法({kid})"]
            if lv not in (None, ""):
                parts.append(f"{lv}级")
            config_block.append(f"{label}: " + " · ".join(str(p) for p in parts))

    # 心法(passive_slots 的 ID 列表 + xinfa_info 的重数)
    xinfa_ids = _get(data, "combat_plan.xinfa.passive_slots")
    xinfa_info = _get(data, "combat_plan.xinfa_info")
    if isinstance(xinfa_ids, list) and xinfa_ids:
        config_block.append("心法:")
        for xid in xinfa_ids:
            xname = _map_name("xinfa", xid) or f"未知心法({xid})"
            rank = ""
            if isinstance(xinfa_info, dict):
                r = (xinfa_info.get(str(xid)) or {}).get("rank")
                if r:
                    rank = f" · {r}重"
            config_block.append(f"  {xname}{rank}")

    if config_block:
        blocks.append(["―― 流派配置 ――"] + config_block)

    # 佩戴装备(过滤占位;槽位号 → 部位名 + 装备名 + 稀有度/等级)
    if isinstance(equips, dict) and equips:
        parts = []
        for slot_no, item in equips.items():
            if not isinstance(item, dict):
                continue
            no = item.get("No")
            eq_info = equip_map.get(str(no))
            if eq_info is None and not (item.get("ex") or {}):
                continue  # 占位
            slot_name = _map_name("slot", slot_no) or f"槽{slot_no}"
            name = eq_info.get("name") if eq_info else f"装备{no}"
            level = f" {eq_info['level']}级" if isinstance(eq_info, dict) and eq_info.get("level") else ""
            rarity = eq_info.get("rarity") if isinstance(eq_info, dict) else None
            star = f"({'★' * rarity})" if isinstance(rarity, int) and rarity > 0 else ""
            parts.append(f"{slot_name}·{name}{level}{star}")
        if parts:
            blocks.append([f"―― 装备明细 ――", f"装备({len(parts)}件):"] + [f"  {p}" for p in parts])

    # 奇术(battle_qs = {槽位: 奇术ID},名称查值的 qs_data,等级查 battle_qs_level[值])
    qs = _get(data, "combat_plan.battle_qs")
    qs_level = _get(data, "combat_plan.battle_qs_level")
    if isinstance(qs, dict) and qs:
        qs_block = ["―― 奇术 ――"]
        for slot_no, qid in qs.items():
            qname = _map_name("qs", qid) or f"未知奇术({qid})"
            rank = ""
            if isinstance(qs_level, dict):
                r = qs_level.get(str(qid))
                if r:
                    rank = f" · {r}级"
            qs_block.append(f"  {qname}{rank}")
        blocks.append(qs_block)

    return blocks, skipped_placeholders


def _render_panel_attrs(info: dict) -> list[str]:
    """面板总属性(roleInfo 接口):气血/外功/属攻/会心会意精准等最终数值。"""
    def num(key: str) -> str | None:
        v = info.get(key)
        if v in (None, "", "0", 0):
            return None
        try:
            f = float(v)
            return str(int(f)) if f.is_integer() else str(round(f, 1))
        except (TypeError, ValueError):
            return str(v)

    def attr_name(key: str) -> str:
        name = _map_name("attr", key)
        if name is None and key.startswith("REAL_"):
            base = _map_name("attr", key[5:])
            if base:
                return "实际" + base
        return name or key

    lines = []
    ordered = [
        ("hpMax", "气血最大值"),
        ("MIN_W_ATK", None), ("MAX_W_ATK", None), ("W_DEF", None),
        ("MIN_PRO_ATK_A", None), ("MAX_PRO_ATK_A", None),
        ("MIN_PRO_ATK_B", None), ("MAX_PRO_ATK_B", None),
        ("MIN_PRO_ATK_C", None), ("MAX_PRO_ATK_C", None),
        ("MIN_PRO_ATK_E", None), ("MAX_PRO_ATK_E", None),
        ("MIN_ACTIVE_PRO_ATK", None), ("MAX_ACTIVE_PRO_ATK", None),
        ("CRI_PROB", None), ("REAL_CRI_PROB", None),
        ("ACR_PROB", None), ("BASH_PROB", None), ("REAL_BASH_PROB", None),
    ]
    panel = []
    for key, override in ordered:
        label = override or attr_name(key)
        v = num(key)
        if v is not None:
            is_prob = "PROB" in key
            suffix = "%" if is_prob else ""
            panel.append(f"{label} {v}{suffix}")
    if panel:
        lines.append("面板属性:")
        lines.extend(f"  {p}" for p in panel)
    return lines


# 区域探索度分档(照抄生涯页 y 函数的阈值)
EXPLORE_TIERS = [
    (9000, "名震一方", 6), (6000, "威名远扬", 5), (4000, "功绩显赫", 4),
    (3000, "登堂入室", 3), (2000, "初来乍到", 2), (1000, "声名鹊起", 1),
]


def _explore_tier(score: int) -> tuple[str, int]:
    for th, name, lv in EXPLORE_TIERS:
        if score >= th:
            return name, lv
    return ("默默无闻", 0)


def _render_explore(data: dict, info_data: dict | None = None) -> list[str]:
    """探索:每区域一行 = 探索度分数+称号等级(proxy)+ 宝箱/蹊跷数(roleInfo)。"""
    scores = data.get("common_score_data.scores")
    if not isinstance(scores, dict) or not scores:
        return []
    eml = (_load_mappings().get("data_report") or {}).get("exploreMapList") or []
    chests, qiqiao = {}, {}
    if isinstance(info_data, dict):
        for x in info_data.get("chestByRegion") or []:
            if isinstance(x, dict):
                chests[str(x.get("region"))] = x.get("count")
        for x in info_data.get("qiQiaoByRegion") or []:
            if isinstance(x, dict):
                qiqiao[str(x.get("region"))] = x.get("count")
    lines = ["探索:"]
    for item in eml:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("id"))
        name = item.get("name") or f"区域{rid}"
        score = scores.get(rid)
        if score is None:
            continue
        try:
            score = int(score)
        except (TypeError, ValueError):
            continue
        tname, lv = _explore_tier(score)
        bits = [f"{score} · {tname} {lv}级"]
        if rid in chests:
            bits.append(f"宝箱 {chests[rid]}")
        if rid in qiqiao:
            bits.append(f"蹊跷 {qiqiao[rid]}")
        lines.append(f"  {name} " + " · ".join(bits))
    return lines if len(lines) > 1 else []

def _flat_pairs(obj: Any, prefix: str = "", depth: int = 0, max_depth: int = 4):
    pairs: list[tuple[str, Any]] = []
    if depth > max_depth:
        return pairs
    if isinstance(obj, dict):
        for k, v in obj.items():
            pairs.extend(_flat_pairs(v, f"{prefix}.{k}" if prefix else str(k), depth + 1, max_depth))
    elif isinstance(obj, list):
        pairs.append((prefix, f"[{len(obj)} 项]"))
        if obj:
            pairs.extend(_flat_pairs(obj[0], f"{prefix}[0]", depth + 1, max_depth))
    else:
        pairs.append((prefix, obj))
    return pairs


def _generic_summary(payload: Any, limit: int = 25) -> list[str]:
    pairs = _flat_pairs(payload)
    lines = []
    for path, value in pairs[:limit]:
        if isinstance(value, float) and value > 1_000_000_000:
            value = _fmt_ts(value)
        lines.append(f"{path}: {value}")
    if len(pairs) > limit:
        lines.append(f"...还有 {len(pairs) - limit} 个字段")
    return lines


def render_role_info(resp: dict[str, Any], title: str, as_blocks: bool = False) -> str | list[str]:
    """渲染 roleInfo(家园/突破/功法日常/面板属性)。

    as_blocks=True 时返回区块列表(供合并转发卡片使用)。
    """
    data = resp.get("data") or {}
    blocks: list[list[str]] = []

    home = [f"【{title}】"]
    homeland = data.get("homelandInfoList")
    if isinstance(homeland, list):
        for h in homeland[:3]:
            if isinstance(h, dict):
                home.append(
                    f"家园(扩展{h.get('expansionId')}): 等级{h.get('level')},"
                    f"NPC{h.get('npcNums')}个,繁荣度{h.get('prosperousNum')}"
                )

    breaks = data.get("breakLog")
    if isinstance(breaks, list) and breaks:
        latest = breaks[-1]
        if isinstance(latest, dict):
            home.append(f"最近突破: {latest.get('dt')} → {latest.get('numW')}")

    kungfu = data.get("kungFuDayCountList")
    if isinstance(kungfu, list) and kungfu:
        latest = kungfu[-1]
        if isinstance(latest, dict):
            dt = str(latest.get("dt") or "")
            if len(dt) == 8 and dt.isdigit():
                dt = f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"
            home.append(f"功法日常: {dt} 最高分 {latest.get('maxScore')}")

    # 面板总属性(roleInfo 接口的真实最终数值)
    panel = _render_panel_attrs(data)
    if panel:
        panel_block = ["―― 面板属性 ――"] + panel
        blocks.append(panel_block)

    if len(home) == 1:
        home.append("(未识别出已知字段,以下是原始响应摘要)")
        home.extend(_generic_summary(data))
    blocks.insert(0, home)

    if as_blocks:
        return ["\n".join(b) for b in blocks if b]
    return "\n\n".join("\n".join(b) for b in blocks)


def render_pvp(resp: dict[str, Any], title: str) -> str:
    """渲染 queryPvp。"""
    data = resp.get("data")
    lines = [f"【{title}】"]
    if isinstance(data, list):
        types = [str(d.get("type")) for d in data if isinstance(d, dict)]
        lines.append(f"PVP 数据类型: {', '.join(types) or '(空)'}")
        lines.append("(type 含义待社区确认;后续版本会解析具体战绩)")
    else:
        lines.extend(_generic_summary(data))
    return "\n".join(lines)


def render_generic(resp: dict[str, Any], title: str) -> str:
    """兜底渲染:任何未知结构。"""
    lines = [f"【{title}】"]
    payload = resp.get("data", resp)
    if isinstance(payload, dict) and payload:
        lines.extend(_generic_summary(payload))
    else:
        raw = json.dumps(payload, ensure_ascii=False)
        lines.append(raw[:600] + ("..." if len(raw) > 600 else ""))
    return "\n".join(lines)


def _num_str(v: Any) -> str:
    try:
        f = float(v)
        return str(int(f)) if f.is_integer() else str(round(f, 1))
    except (TypeError, ValueError):
        return str(v)


def _render_panel_v2(data: dict, bench_data: dict | None = None) -> str:
    """「燕云面板」:调号助手 role-data 的结构化渲染(属性面板/战斗面板/流派配置)+ 全服基准对比。"""
    maps = _load_mappings()
    r = data.get("role") or {}
    ap, cp = data.get("attributePanel") or {}, data.get("combatPanel") or {}
    if not r and not ap:
        return "(role-data 响应为空)"

    lines = []
    nick = r.get("nickname") or "未知"
    school_name = (maps.get("school", {}).get(str(r.get("school"))) or {}).get("name", r.get("school"))
    lines.append(f"【燕云面板 · {nick}】")
    lines.append(f"等级: {r.get('level')} | 门派: {school_name} | 武林造诣: {r.get('wulinScore')}")

    lines.append("")
    lines.append("―― 属性面板(总值) ――")
    lines.append(f"外功攻击总值: {_num_str(ap.get('wAtk'))}")
    lines.append(f"属性攻击: {_num_str(ap.get('attributeAttack'))}")
    lines.append(f"实际精准率: {_num_str(ap.get('realAcrProb'))}% | 实际会心率: {_num_str(ap.get('realCriProb'))}% | 实际会意率: {_num_str(ap.get('realBashProb'))}%")
    lines.append(f"特殊词条数: {_num_str(ap.get('specialAffixCount'))}")

    lines.append("")
    lines.append("―― 战斗面板 ――")
    for key, label in (("minWAtk", "最小外功攻击"), ("maxWAtk", "最大外功攻击"),
                       ("minAttributeAttack", "最小属性攻击"), ("maxAttributeAttack", "最大属性攻击")):
        lines.append(f"  {label}: {_num_str(cp.get(key))}")
    lines.append(f"  实际精准率 {_num_str(cp.get('realAcrProb'))}% | 实际会心率 {_num_str(cp.get('realCriProb'))}% | 实际会意率 {_num_str(cp.get('realBashProb'))}%")

    wx = r.get("wuxue") or {}
    lines.append("")
    lines.append("―― 流派配置 ――")
    mk, sk = wx.get("mainKongfuId"), wx.get("subKongfuId")
    if mk:
        kname = (maps.get("kongfu", {}).get(str(mk)) or {}).get("name", mk)
        lines.append(f"主功法: {kname} · {wx.get('mainKongfuLevel')}级")
    if sk:
        kname = (maps.get("kongfu", {}).get(str(sk)) or {}).get("name", sk)
        lines.append(f"副功法: {kname} · {wx.get('subKongfuLevel')}级")
    liupai_name = (maps.get("liupai", {}).get(f"{mk}-{sk}") or {}).get("name")
    if liupai_name:
        lines.append(f"流派: {liupai_name}")

    xinfa_ids = r.get("xinfaList") or []
    if xinfa_ids:
        parts = []
        for x in xinfa_ids:
            xname = (maps.get("xinfa", {}).get(str(x.get("xinfaId"))) or {}).get("name", x.get("xinfaId"))
            parts.append(f"{xname} {x.get('rank')}重")
        lines.append("心法: " + " | ".join(parts))

    equip_list = r.get("equipList") or []
    equip_map = maps.get("equip", {})
    suffix_map = maps.get("suffix", {})
    slot_map = maps.get("slot", {})
    real_equips = [e for e in equip_list if str(e.get("equipNo")) in equip_map]
    if real_equips:
        lines.append(f"装备({len(real_equips)}件):")
        for e in real_equips:
            info = equip_map.get(str(e.get("equipNo"))) or {}
            sname = (suffix_map.get(str(e.get("suitId"))) or {}).get("name") or "暂未收录"
            slot_name = (slot_map.get(str(e.get("slotId"))) or {}).get("name", e.get("slotId"))
            star = "★" * int(info.get("rarity") or 0)
            lines.append(f"  {slot_name}·{info.get('name', e.get('equipNo'))} {info.get('level', '')}级{star} [{sname}]")

    qs_list = r.get("qishuList") or []
    if qs_list:
        lines.append("奇术:")
        for q in qs_list:
            qname = (maps.get("qs", {}).get(str(q.get("qsId"))) or {}).get("name", q.get("qsId"))
            lines.append(f"  {qname} · {q.get('level')}级")

    if bench_data:
        lines.extend(_render_benchmark_compare(bench_data, r, cp, liupai_name or ""))
    lines.append("")
    lines.append("⭐ 提示:数据来自《燕云十六声》官方接口,详细数据可能与游戏内实际略有差距。")
    return "\n".join(lines)


def _render_benchmark_compare(bench_data: dict, role: dict, combat_panel: dict, liupai_name: str) -> list[str]:
    """全服基准对比:同流派同等级段,用户值 vs 均值/极值。"""
    bench_list = ((bench_data.get("result") or {}).get("list")) or []
    if not bench_list:
        return []
    level = role.get("level") or 0
    candidates = [x for x in bench_list if x.get("liupai") == liupai_name] if liupai_name else []
    if not candidates:
        candidates = [x for x in bench_list if x.get("playerType") == "common"] or bench_list

    def group_range(item: dict) -> tuple[int, int]:
        group = str(item.get("levelGroup") or "")
        if "-" in group:
            try:
                lo, hi = map(int, group.split("-", 1))
                return lo, hi
            except ValueError:
                pass
        try:
            v = int(group)
            return v, v
        except ValueError:
            return 0, 99999

    matched = [x for x in candidates if group_range(x)[0] <= level <= group_range(x)[1]]
    if not matched and candidates:
        # 就近档位(等级差最小)
        matched = [min(candidates, key=lambda x: abs(level - group_range(x)[0]))]
    chosen = matched[0] if matched else None
    if not chosen:
        return []
    group_label = chosen.get("levelGroup") or "?"
    sample = chosen.get("sampleCount") or 0

    def fmt(val: Any, is_prob: bool) -> str:
        try:
            f = float(val)
            return f"{f * 100:.1f}%" if is_prob else (str(int(f)) if f.is_integer() else str(round(f, 1)))
        except (TypeError, ValueError):
            return str(val)

    lines = [""]

    def compare(user_val: Any, avg_key: str, max_key: str, label: str, is_prob: bool = False):
        avg = chosen.get(avg_key)
        mx = chosen.get(max_key)
        if avg is None or user_val in (None, ""):
            return
        try:
            u = float(user_val)
            a = float(avg)
            m = float(mx) if mx is not None else None
        except (TypeError, ValueError):
            return
        if is_prob:
            # 统一为 0-1 小数再格式化:role-data 面板值是百分数(67.5),benchmark 概率是小数(0.224)
            u = u / 100
        bits = [f"全服均值 {fmt(a, is_prob)}"]
        if m is not None:
            bits.append(f"极值 {fmt(m, is_prob)}")
        ratio = f"(均值的 {u / a * 100:.0f}%)" if a else ""
        over = " — 已超极值" if m and u >= m else ""
        lines.append(f"  {label}: 你的 {fmt(u, is_prob)} | " + " | ".join(bits) + f" {ratio}{over}")

    lines.append(f"―― 全服基准对比({liupai_name or '全流派'} · {group_label}级段 · 样本 {sample}) ――")
    compare(combat_panel.get("maxWAtk"), "avgWAtkMax", "maxWAtkMax", "最大外功攻击")
    compare(combat_panel.get("minWAtk"), "avgWAtkMin", "maxWAtkMin", "最小外功攻击")
    compare(combat_panel.get("realCriProb"), "avgCriProb", "maxCriProb", "实际会心率", True)
    compare(combat_panel.get("realAcrProb"), "avgAcrProb", "maxAcrProb", "实际精准率", True)
    compare(combat_panel.get("realBashProb"), "avgBashProb", "maxBashProb", "实际会意率", True)
    return lines
