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
# 唯一数据源 = 云端仓库里的 watchlist.json。Mac 上用它的本机克隆（手机 Artifact、云端盯梢
# 也读写同一个文件）。克隆不存在时退回本目录下的文件，照常能跑。
CLONE = os.environ.get("GW_CLONE") or "/Users/liuyong/Claude/Projects/doc-greatwalk-watch"
_LOCAL_FILE = os.path.join(HERE, "watchlist.json")
WATCHLIST_FILE = (os.path.join(CLONE, "watchlist.json")
                  if os.path.isdir(os.path.join(CLONE, ".git")) else _LOCAL_FILE)

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
            "exclude": raw.get("exclude") or [],
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

        ex = e["exclude"]
        if isinstance(ex, str):
            ex = [x for x in ex.replace("，", ",").split(",")]
        good, bad = [], []
        for x in ex:
            x = str(x).strip()
            if not x:
                continue
            try:
                good.append(datetime.strptime(x, "%Y-%m-%d").strftime("%Y-%m-%d"))
            except ValueError:
                bad.append(x)
        if bad:
            errors.append({"id": eid, "field": "exclude",
                           "msg": f"排除日期格式不对：{'、'.join(bad)}（要 YYYY-MM-DD）"})
        e["exclude"] = sorted(set(good))

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


# ── 与 GitHub 同步（Mac 这一侧）─────────────────────────────────

import subprocess


def _git(*args, timeout=45):
    return subprocess.run(["git", "-C", CLONE, *args], capture_output=True, text=True, timeout=timeout)


def git_ready():
    return os.path.isdir(os.path.join(CLONE, ".git"))


def pull():
    """
    把手机/云端改过的清单拉下来。返回 (是否变了, 说明)。
    本机有没推上去的改动时先推再拉；拉不动（冲突）就丢弃本机未推送的那份、以云端为准。
    """
    if not git_ready():
        return False, "没有本机克隆，跳过同步"
    try:
        before = open(WATCHLIST_FILE, "rb").read() if os.path.exists(WATCHLIST_FILE) else b""
        if _git("status", "--porcelain", "watchlist.json").stdout.strip():
            ok, msg = push("Sync pending local change")
            if not ok:
                _git("checkout", "--", "watchlist.json")
        r = _git("pull", "--ff-only", "-q", "origin", "main")
        if r.returncode != 0:
            _git("fetch", "-q", "origin", "main")
            _git("reset", "-q", "--hard", "origin/main")
            note = "本机和云端分叉，已以云端为准"
        else:
            note = "已同步"
        after = open(WATCHLIST_FILE, "rb").read() if os.path.exists(WATCHLIST_FILE) else b""
        return before != after, note
    except Exception as e:
        return False, f"同步出错：{type(e).__name__}: {e}"


def push(message="Update watchlist"):
    """把本机改过的清单推到 GitHub（部署密钥）。返回 (ok, 说明)。"""
    if not git_ready():
        return False, "没有本机克隆"
    try:
        _git("add", "watchlist.json")
        if _git("diff", "--cached", "--quiet").returncode == 0:
            return True, "没有变化"
        c = _git("commit", "-q", "-m", message, "-m", "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>")
        if c.returncode != 0:
            return False, (c.stderr or c.stdout).strip()[:200]
        r = _git("pull", "--rebase", "-q", "origin", "main")
        if r.returncode != 0:
            _git("rebase", "--abort")
            _git("reset", "-q", "--hard", "origin/main")
            return False, "手机端刚改过清单，已载入最新版——请重新修改一次"
        p = _git("push", "-q", "origin", "main")
        if p.returncode != 0:
            return False, (p.stderr or p.stdout).strip()[:200]
        return True, _git("log", "--oneline", "-1").stdout.strip()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def sync_status():
    """本机清单和云端是否一致"""
    if not git_ready():
        return {"configured": False, "error": "没有本机克隆"}
    try:
        _git("fetch", "-q", "origin", "main")
        ahead = int(_git("rev-list", "--count", "origin/main..HEAD").stdout.strip() or 0)
        behind = int(_git("rev-list", "--count", "HEAD..origin/main").stdout.strip() or 0)
        dirty = bool(_git("status", "--porcelain", "watchlist.json").stdout.strip())
        return {"configured": True, "in_sync": not ahead and not behind and not dirty,
                "ahead": ahead, "behind": behind, "dirty": dirty, "error": None}
    except Exception as e:
        return {"configured": False, "error": str(e)}
