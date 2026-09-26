"""
关注清单 watchlist.json —— 本机盯梢、云端盯梢、管理界面共用的唯一数据源。

每一条（entry）独立指定线路、出发日区间、人数、是否自动占位：

    {
      "entries": [
        {"id": "a1b2", "track": "Milford Track", "from": "2027-02-10", "to": "2027-04-28",
         "people": 1, "enabled": true, "auto_reserve": true, "huts_only": false},
        ...
      ]
    }

文件不存在时，从旧的 watch_config.py（TRACK / WATCH_DATE_RANGE / ...）推导一份，
所以老配置照样能跑。
"""

import json
import os
import secrets
from datetime import datetime

import tracks

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(HERE, "watchlist.json")

MAX_PEOPLE = 20
MAX_SPAN_DAYS = 400


def new_id():
    return secrets.token_hex(3)


def mode_for(track):
    """有预设连住行程的单向线用 itinerary，其余用「任意空位」"""
    return "itinerary" if tracks.get(track)["default_itinerary"] else "any"


def _legacy_entries():
    """从旧 watch_config.py 推导（兼容：没有 watchlist.json 时也能跑）"""
    import watch_config as wc
    raw = getattr(wc, "TRACK", "Milford Track")
    names = [raw] if isinstance(raw, str) else list(raw)
    rng = getattr(wc, "WATCH_DATE_RANGE", None) or ("2027-02-10", "2027-04-28")
    only = getattr(wc, "AUTO_RESERVE_TRACKS", None)
    only = [tracks.resolve_name(t) for t in only] if only else None
    out = []
    for n in names:
        t = tracks.resolve_name(n)
        out.append({
            "id": new_id(), "track": t, "from": rng[0], "to": rng[1],
            "people": int(getattr(wc, "PEOPLE", 1)), "enabled": True,
            "auto_reserve": bool(getattr(wc, "AUTO_RESERVE", False))
                            and (only is None or t in only),
            "huts_only": False,
        })
    return out


def _env_entries():
    """CI 覆盖：设了 GW_TRACK 就按环境变量生成（向后兼容旧 workflow）"""
    t = os.environ.get("GW_TRACK")
    if not t:
        return None
    rng = (os.environ.get("GW_DATE_RANGE") or "2027-02-10,2027-04-28").split(",")
    people = int(os.environ.get("GW_PEOPLE") or 1)
    return [{"id": new_id(), "track": tracks.resolve_name(x.strip()),
             "from": rng[0].strip(), "to": rng[1].strip(), "people": people,
             "enabled": True, "auto_reserve": False, "huts_only": False}
            for x in t.split(",") if x.strip()]


def load():
    env = _env_entries()
    if env is not None:
        return {"entries": env, "source": "env"}
    try:
        with open(WATCHLIST_FILE, encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("entries", [])
        data["source"] = "file"
        return data
    except FileNotFoundError:
        return {"entries": _legacy_entries(), "source": "legacy"}


def validate(data):
    """
    清洗并校验。返回 (干净的数据, 错误列表)。错误形如 {"id":..., "field":..., "msg":...}。
    有错误时调用方不应保存。
    """
    errors, clean = [], []
    for raw in (data or {}).get("entries", []):
        e = {
            "id": str(raw.get("id") or new_id())[:16],
            "track": raw.get("track", ""),
            "from": str(raw.get("from", "")).strip(),
            "to": str(raw.get("to", "")).strip(),
            "people": raw.get("people", 1),
            "enabled": bool(raw.get("enabled", True)),
            "auto_reserve": bool(raw.get("auto_reserve", False)),
            "huts_only": bool(raw.get("huts_only", False)),
        }
        eid = e["id"]
        try:
            e["track"] = tracks.resolve_name(e["track"])
        except KeyError:
            errors.append({"id": eid, "field": "track", "msg": "未知线路"})
            clean.append(e)
            continue

        d1 = d2 = None
        for k in ("from", "to"):
            try:
                d = datetime.strptime(e[k], "%Y-%m-%d")
                if k == "from":
                    d1 = d
                else:
                    d2 = d
            except ValueError:
                errors.append({"id": eid, "field": k, "msg": "日期格式要是 YYYY-MM-DD"})
        if d1 and d2:
            if d2 < d1:
                errors.append({"id": eid, "field": "to", "msg": "结束日早于开始日"})
            elif (d2 - d1).days > MAX_SPAN_DAYS:
                errors.append({"id": eid, "field": "to",
                               "msg": f"区间超过 {MAX_SPAN_DAYS} 天，太长了"})
            if d2 < datetime.now():
                errors.append({"id": eid, "field": "to", "msg": "整段日期都已过去"})

        try:
            e["people"] = int(e["people"])
            if not 1 <= e["people"] <= MAX_PEOPLE:
                raise ValueError
        except (TypeError, ValueError):
            errors.append({"id": eid, "field": "people", "msg": f"人数 1~{MAX_PEOPLE}"})
            e["people"] = 1

        if mode_for(e["track"]) != "itinerary":
            e["auto_reserve"] = False          # 任意空位模式没有「整条行程」可占
        clean.append(e)
    return {"entries": clean}, errors


def save(data):
    """原子写入。调用方负责先 validate。"""
    out = {
        "updated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "entries": data["entries"],
    }
    tmp = WATCHLIST_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, WATCHLIST_FILE)
    return out


def active(data=None):
    """启用中的条目"""
    data = data or load()
    return [e for e in data["entries"] if e.get("enabled", True)]
