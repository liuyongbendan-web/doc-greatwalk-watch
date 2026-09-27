#!/usr/bin/env python3
"""
DOC Great Walk 持续盯梢器（纯 HTTP 版，无需浏览器 / 无需登录）
支持全部 10 条 Great Walk，线路/日期/人数在 watch_config.py 里配，或跑 configure.py 交互设置。

用法:
    python3 watch.py            # 持续循环盯梢（Ctrl-C 停止）
    python3 watch.py --once     # 只查一次（适合 cron / GitHub Actions）
    python3 watch.py --status   # 打印当前快照，不报警
    python3 watch.py --table    # 打印整段日期的余量总表

数据来源: DOC 预订系统前端自己调用的 JSON 接口
    POST .../nzrdr/rdr/search/greatwalkplacefacility
    {"placeId":873,"arrivalDate":"YYYY-MM-DD","nights":11,...}
只读，不需要 cookie / 账号，一次请求约 7KB。
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import tracks
import watch_config as wc
import watchlist

# 环境变量覆盖（给 GitHub Actions / 云端 cron 用，不必改配置文件）
def _env(*names):
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    return None


if _env("GW_WEBHOOK_URL", "MILFORD_WEBHOOK_URL"):
    wc.WEBHOOK_URL = _env("GW_WEBHOOK_URL", "MILFORD_WEBHOOK_URL")
if _env("GW_TRACK"):                               # 多条用逗号分隔
    _t = [x.strip() for x in _env("GW_TRACK").split(",") if x.strip()]
    wc.TRACK = _t[0] if len(_t) == 1 else _t
if _env("GW_DATE_RANGE", "MILFORD_DATE_RANGE"):   # 形如 "2027-02-10,2027-04-28"
    wc.WATCH_DATE_RANGE = tuple(_env("GW_DATE_RANGE", "MILFORD_DATE_RANGE").split(","))
if _env("GW_PEOPLE", "MILFORD_PEOPLE"):
    wc.PEOPLE = int(_env("GW_PEOPLE", "MILFORD_PEOPLE"))
if os.environ.get("CI"):                          # CI 里没有通知中心/扬声器/浏览器
    wc.NOTIFY_MACOS = wc.NOTIFY_SPEAK = wc.OPEN_BROWSER = wc.AUTO_RESERVE = False

API_URL = ("https://prod-nz-rdr.recreation-management.tylerapp.com"
           "/nzrdr/rdr/search/greatwalkplacefacility")
BOOKING_URL = "https://bookings.doc.govt.nz/Web/#!greatwalk-result"

HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://bookings.doc.govt.nz",
    "Referer": "https://bookings.doc.govt.nz/",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
}

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "watch_state.json")
LOG_FILE = os.path.join(HERE, "watch.log")
HEARTBEAT_FILE = os.path.join(HERE, "watch_heartbeat.json")
PID_FILE = os.path.join(HERE, "watch.pid")

# 一次性查询用的开关。带这些开关的进程不是常驻盯梢，单实例锁必须忽略它们，
# 否则一次性查询和守护进程启动撞车时，守护进程会误判成"已有实例"而自杀。
ONE_SHOT_FLAGS = ("--once", "--status", "--table", "--health", "--watchdog", "--test-notify")

MAX_SILENT_FAILURES = 3
WEBHOOK_FAILED = False   # 推送失败过吗 —— CI 里要据此返回非零退出码     # 连续这么多轮取数失败就报警（不能让它默默死掉）

def watched_tracks():
    """清单里启用中的线路（去重、保序）"""
    seen = []
    for e in watchlist.active():
        if e["track"] not in seen:
            seen.append(e["track"])
    return seen


def track_info(name=None):
    ts = watched_tracks()
    return tracks.get(name or (ts[0] if ts else "Milford Track"))


def track_name(name=None):
    if name:
        return tracks.resolve_name(name)
    ts = watched_tracks()
    return ts[0] if ts else "（清单为空）"


def tracks_label():
    ts = watched_tracks()
    if not ts:
        return "（清单为空）"
    return " + ".join(ts) if len(ts) <= 3 else f"{len(ts)} 条线路"


def cfg_for(track, key, default=None):
    """
    取某条线路的配置。MODE / ITINERARY / HUTS_FILTER 既可以写成单个值
    （所有线路通用），也可以写成 {线路名: 值} 的字典分别指定。
    """
    v = getattr(wc, key, default)
    if isinstance(v, dict):
        for k, val in v.items():
            try:
                if tracks.resolve_name(k) == track:
                    return val
            except KeyError:
                continue
        return default
    return v

MAX_NIGHTS = 11          # 接口上限，一次返回 12 天
WINDOW_DAYS = MAX_NIGHTS + 1
REQUEST_GAP = 3.0        # 相邻请求间隔（秒），实测 5s 稳，3s 也没被限


class Throttled(Exception):
    """接口返回空 —— 被限流，不能当成'没票'"""


# ── 小工具 ───────────────────────────────────────────────────

def log(msg):
    line = f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"counts": {}, "last_alert": {}}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


def write_heartbeat(**kv):
    """
    写心跳，供 --health / --watchdog 判断死活。会保留上一次的 ok/fails/cycle，
    只更新传进来的字段 —— 这样在「检查中」「占位中」也能随时刷新时间戳，
    而不会把「上轮取数成功」冲掉。
    """
    old = read_heartbeat() or {}
    hb = {k: v for k, v in old.items() if k in ("ok", "fails", "cycle", "error", "phase")}
    hb.update({"ts": time.time(), "iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "pid": os.getpid(), "track": tracks_label(), "poll": wc.POLL_SECONDS})
    hb.update(kv)
    try:
        tmp = HEARTBEAT_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(hb, f, indent=1)
        os.replace(tmp, HEARTBEAT_FILE)
    except OSError:
        pass


def read_heartbeat():
    try:
        with open(HEARTBEAT_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def process_cmdline(pid):
    try:
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                              capture_output=True, text=True).stdout.strip()
    except Exception:
        return ""


def daemon_pid():
    """正在跑的**常驻**盯梢进程 PID；没有就返回 None。

    只认 watch.pid 里记的那个，并核对它的命令行确实是 watch.py 且不带一次性开关。
    这样一次性查询（--once/--health/...）永远不会被误当成守护进程。
    """
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    if pid == os.getpid():
        return None
    cmd = process_cmdline(pid)
    if not cmd or "watch.py" not in cmd:
        return None                       # 进程已死，或 PID 被别的程序复用
    if any(flag in cmd for flag in ONE_SHOT_FLAGS):
        return None                       # 是一次性查询，不算守护进程
    return pid


def write_pidfile():
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def watcher_pids():
    p = daemon_pid()
    return [p] if p else []


def entry_dates(entry):
    """一条清单条目的全部出发日（含首尾）"""
    a = datetime.strptime(entry["from"], "%Y-%m-%d")
    b = datetime.strptime(entry["to"], "%Y-%m-%d")
    skip = set(entry.get("exclude") or [])      # 明确说过不要的出发日
    out = []
    while a <= b:
        d = a.strftime("%Y-%m-%d")
        if d not in skip:
            out.append(d)
        a += timedelta(days=1)
    return out


def all_start_dates():
    ds = sorted({d for e in watchlist.active() for d in entry_dates(e)})
    return ds


def watched_huts(track, entry=None):
    """任意空位模式要盯的住宿点。条目勾了「只要小屋」就排除营地"""
    all_huts = track_info(track)["huts"]
    if entry and entry.get("huts_only"):
        return [h for h in all_huts if "hut" in h.lower()] or all_huts
    f = cfg_for(track, "HUTS_FILTER", []) or []
    if not f:
        return all_huts
    picked = [h for h in all_huts if any(k.lower() in h.lower() for k in f)]
    return picked or all_huts


def itinerary_for(track):
    itin = cfg_for(track, "ITINERARY", None) or track_info(track)["default_itinerary"]
    if not itin:
        raise SystemExit(
            f"{track} 没有预设连住行程，请在 watch_config.py 的 ITINERARY 里填"
            f"（多线路时写成 {{'{track}': [...]}}），或把 MODE 改成 'any'。"
            f"可选住宿点：{track_info(track)['huts']}")
    return itin


def targets(entries=None):
    """
    返回 [(线路, 标签, [(住宿点, 日期), ...], 条目), ...]。组内全部有位 = 「整组命中」。
      连住线路（Milford/Routeburn/...）: 一组 = 一个出发日的连住行程
      其余线路                        : 一组 = 一个 (住宿点, 日期)
    """
    out = []
    for e in (entries if entries is not None else watchlist.active()):
        track = e["track"]
        if watchlist.mode_for(track) == "itinerary":
            itin = itinerary_for(track)
            for sd in entry_dates(e):
                d0 = datetime.strptime(sd, "%Y-%m-%d")
                out.append((track, sd,
                            [(hut, (d0 + timedelta(days=i)).strftime("%Y-%m-%d"))
                             for i, hut in enumerate(itin)], e))
        else:
            out += [(track, f"{hut} {d}", [(hut, d)], e)
                    for d in entry_dates(e) for hut in watched_huts(track, e)]
    return out


def needed_dates(tgts):
    need = {}
    for track, _, legs, _ in tgts:
        need.setdefault(track, set()).update(d for _, d in legs)
    return need


def windows(all_dates):
    """把需要覆盖的住宿日切成若干个 12 天请求窗口"""
    ds = sorted({datetime.strptime(d, "%Y-%m-%d") for d in all_dates})
    starts, i = [], 0
    while i < len(ds):
        s = ds[i]
        starts.append(s.strftime("%Y-%m-%d"))
        end = s + timedelta(days=WINDOW_DAYS - 1)
        while i < len(ds) and ds[i] <= end:
            i += 1
    return starts


# ── 取数 ─────────────────────────────────────────────────────

def fetch_window(track, arrival_date, retries=3):
    """返回 {(hut, 'YYYY-MM-DD'): 余量, 关闭则 None}"""
    body = json.dumps({
        "accomodation": "",
        "placeId": track_info(track)["place_id"],
        "customerClassificationId": 0,
        "arrivalDate": arrival_date,
        "nights": MAX_NIGHTS,
    }).encode()

    last_err = None
    for attempt in range(retries):
        if attempt:
            time.sleep(8 * attempt)
        try:
            req = urllib.request.Request(API_URL, data=body, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
            if not raw.strip():
                last_err = Throttled(f"空响应 (HTTP {r.status})")
                continue
            data = json.loads(raw)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError) as e:
            last_err = e
            continue

        grid = {}
        for fac in data.get("GreatWalkFacilityData", []):
            hut = fac.get("FacilityName", "").strip()
            for day in fac.get("GreatWalkFacilityDateData", []):
                iso = day["ArrivalDate"][:10]
                grid[(hut, iso)] = (day.get("TotalAvailable")
                                    if day.get("IsSeasonAvailable") else None)
        if not grid:
            last_err = Throttled("响应里没有小屋数据")
            continue
        return grid

    raise Throttled(f"{track} {arrival_date}: {last_err}")


def fetch_all(needed):
    """
    needed: {线路: {住宿日, ...}}
    返回 ({(线路, 住宿点, 日期): 余量}, 请求次数)。
    任何窗口失败都抛错（绝不把限流当成没票）。
    """
    grid, nreq = {}, 0
    for track, dates in needed.items():
        for w in windows(dates):
            if nreq:
                time.sleep(REQUEST_GAP)
            nreq += 1
            for (hut, d), v in fetch_window(track, w).items():
                grid[(track, hut, d)] = v
    return grid, nreq


# ── 报警 ─────────────────────────────────────────────────────

def normalize_hook(hook):
    """
    容错：只填了 ntfy 主题名（没有 https://）时自动补全。
    urllib 遇到没有 scheme 的地址会抛 "unknown url type"，
    2026-09-25 云端首跑就是栽在这里 —— 票查到了却发不出去。
    """
    hook = (hook or "").strip()
    if not hook:
        return ""
    if "://" not in hook:
        return "https://ntfy.sh/" + hook.lstrip("/")
    return hook


def send_webhook(hook, title, body, url, track=None):
    """自动适配 ntfy / Discord / Slack / 通用 JSON 四种格式"""
    hook = normalize_hook(hook)
    full = (f"{body}\n\n{url}\n"
            f"手机上：选 {track or tracks_label()} → 选日期 → 点绿色格子 → Reserve\n"
            f"（Reserve 后购物车锁约 15 分钟，不必抢着付款）")
    host = hook.split("/")[2].lower() if "://" in hook else ""

    if "ntfy" in host:
        # ntfy: 正文即消息，标题/优先级/点击链接走 HTTP header。
        # HTTP header 只能是 latin-1，中文会乱码 -> 标题用 ASCII，中文全放正文。
        ascii_title = "DOC Great Walk availability"
        data = (f"{title}\n{full}").encode()
        headers = {
            "Title": ascii_title,
            "Priority": "urgent",
            "Tags": "tada",
            "Click": url,
        }
    elif "discord" in host:
        data = json.dumps({"content": f"**{title}**\n{full}"}).encode()
        headers = {"Content-Type": "application/json"}
    elif "slack" in host:
        data = json.dumps({"text": f"*{title}*\n{full}"}).encode()
        headers = {"Content-Type": "application/json"}
    else:
        data = json.dumps({"title": title, "message": full, "text": full,
                           "content": full, "url": url}).encode()
        headers = {"Content-Type": "application/json"}

    urllib.request.urlopen(
        urllib.request.Request(hook, data=data, headers=headers), timeout=15)


MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def speak_date(d):
    """'2027-03-11' -> 'March 11'（给 say 朗读用）"""
    try:
        return f"{MONTHS[int(d[5:7]) - 1]} {int(d[8:10])}"
    except Exception:
        return d


def today_key():
    """「同一天」按墨尔本/悉尼日历算 —— 云端跑在 UTC，不统一的话它会在上午 10 点换天"""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Australia/Melbourne")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


SENT_FILE = os.path.join(HERE, "notify_sent.json")


def _load_sent():
    try:
        with open(SENT_FILE) as f:
            d = json.load(f)
        if d.get("day") == today_key():
            return d
    except (OSError, json.JSONDecodeError):
        pass
    return {"day": today_key(), "titles": []}


def _sent_today(title):
    return title in _load_sent()["titles"]


def _mark_sent(title):
    d = _load_sent()
    if title not in d["titles"]:
        d["titles"].append(title)
    try:
        tmp = SENT_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, SENT_FILE)
    except OSError:
        pass


def notify(title, body, url=None, track=None, kind="availability", speech=None):
    """
    kind: availability（有票）/ reserve（自动占位结果）/ health（盯梢自身状况）/ test
    推到手机的规则：
      云端(CI)   —— 全部推
      本机       —— 只推 LOCAL_PUSH_KINDS 里的种类（默认只推占位结果）。
                    有票的提醒由云端推，本机再推一遍就重复了；
                    但「Mac 上替你占了位、等你付款」只有本机知道，必须推。
    标题里就放日期 —— macOS 常把通知正文藏起来（预览设为「解锁时显示」），
    只看得到标题，所以日期不能只写在正文里。
    """
    # 同一天、同一条（按标题）只发一次 —— 不管你订没订。测试消息不去重。
    # 看门狗、常驻盯梢、一次性查询是不同进程，所以记在独立的小文件里共用。
    if kind != "test" and _sent_today(title):
        log(f"   🔕 今天已发过同样的通知，不再发：{title}")
        return
    log(f"🔔 {title} — {body}")
    if wc.NOTIFY_BELL:
        sys.stdout.write("\a" * 3)
        sys.stdout.flush()
    if wc.NOTIFY_MACOS and sys.platform == "darwin":
        subprocess.run(["osascript", "-e",
                        'display notification "{}" with title "{}" sound name "Glass"'
                        .format(body.replace('"', "'"), title.replace('"', "'"))],
                       capture_output=True)
    if wc.NOTIFY_SPEAK and sys.platform == "darwin" and kind != "health":
        subprocess.run(["say", "-r", "175",
                        speech or f"{(track or tracks_label())} has availability. Go book it now."],
                       capture_output=True)
    push_kinds = getattr(wc, "LOCAL_PUSH_KINDS", ("reserve",))
    hook = getattr(wc, "WEBHOOK_URL", "") or ""
    if hook and (os.environ.get("CI") or kind in push_kinds or kind == "test"):
        try:
            send_webhook(hook, title, body, url or BOOKING_URL, track)
            log("   webhook 已发送")
        except Exception as e:
            log(f"   ⚠️ webhook 失败: {e}")
            globals()["WEBHOOK_FAILED"] = True
    if wc.OPEN_BROWSER and sys.platform == "darwin" and kind == "availability":
        subprocess.run(["open", url or BOOKING_URL], capture_output=True)
    if kind != "test" and not globals().get("WEBHOOK_FAILED"):
        _mark_sent(title)


# ── 一轮检查 ─────────────────────────────────────────────────

def _fmt_dates(dates, cap=12):
    """把一串日期压成人能读的样子：同月合并，超过 cap 个就截断"""
    dates = sorted(set(dates))
    shown, rest = dates[:cap], len(dates) - cap
    out, i = [], 0
    while i < len(shown):
        ym = shown[i][:7]
        same = [d for d in shown[i:] if d[:7] == ym]
        out.append(f"{ym[5:]}月 " + "/".join(d[8:] for d in same) + " 日")
        i += len(same)
    txt = "，".join(out)
    if rest > 0:
        txt += f" …等共 {len(dates)} 个"
    return txt


def summarize_hits(hits, itinerary_mode):
    """
    把命中结果写成一眼能看懂的话，重点是「哪几天有票」。
    hits: [(标签, 住宿点, 日期, 余量, 是否整组命中), ...]
    """
    if not itinerary_mode:
        # 捡漏模式：每个命中就是一个 (住宿点, 日期)，按日期归纳
        by_date = {}
        for _, hut, d, n, _ in hits:
            by_date.setdefault(d, []).append(f"{hut.split()[0]}({n})")
        ds = sorted(by_date)
        head = "；".join(f"{d[5:]} {'、'.join(by_date[d])}" for d in ds[:8])
        more = f" …等共 {len(ds)} 天" if len(ds) > 8 else ""
        return f"{len(ds)} 天有空位：{head}{more}"

    full_dates = sorted({h[0] for h in hits if h[4]})
    parts = []

    if full_dates:
        parts.append(f"【整条行程可连订】{len(full_dates)} 个出发日：{_fmt_dates(full_dates)}")
        # 把最小余量标出来，1 个床位的要抓紧
        tight = sorted({h[0] for h in hits if h[4] and h[3] <= 2})
        if tight:
            parts.append(f"其中 {_fmt_dates(tight, 8)} 只剩 1~2 个床位，要快")

    # 剩下的是「只有部分晚数有空」的
    partial = [h for h in hits if not h[4]]
    if partial:
        by_date = {}
        for _, hut, d, n, _ in partial:
            by_date.setdefault(d, []).append(f"{hut.split()[0]}({n})")
        ds = sorted(by_date)
        head = "；".join(f"{d[5:]} {'、'.join(by_date[d])}" for d in ds[:6])
        more = f" …等共 {len(ds)} 天" if len(ds) > 6 else ""
        label = "【另有单晚空位】" if full_dates else "【单晚空位】"
        parts.append(f"{label}{head}{more}")

    return "  ".join(parts) if parts else "有空位（详见日志）"


def _day_bucket(state, name):
    """state[name] = {"day": 今天, ...}，换天自动清空"""
    b = state.get(name)
    if not isinstance(b, dict) or b.get("day") != today_key():
        b = {"day": today_key()}
        state[name] = b
    return b


def _md(d):
    return f"{int(d[5:7])}/{int(d[8:10])}"


def _short(h):
    return re.sub(r"\s*(Hut|Campsite|Shelter|Bunkroom)\s*$", "", h, flags=re.I)


def alert_title(track, hits, itinerary_mode):
    """标题里直接写日期（见 notify 的说明）"""
    t = track.replace(" Track", "")
    full = sorted({x[0] for x in hits if x[4]}) if itinerary_mode else []
    if full:
        ds = "、".join(_md(d) for d in full[:3]) + (f" 等{len(full)}个" if len(full) > 3 else "")
        return f"🎉 {t} 整条有票：{ds} 出发"
    nights = sorted({(x[2], _short(x[1])) for x in hits})
    ds = "、".join(f"{_md(d)} {h}" for d, h in nights[:2]) + (f" 等{len(nights)}处" if len(nights) > 2 else "")
    return f"✨ {t} 单晚有位：{ds}"


def alert_speech(track, hits, itinerary_mode):
    full = sorted({x[0] for x in hits if x[4]}) if itinerary_mode else []
    if full:
        return f"{track}: full trip available, departing {speak_date(full[0])}" + \
               (f", and {len(full) - 1} more dates." if len(full) > 1 else ".")
    d = sorted({x[2] for x in hits})[0]
    return f"{track}: a single night available on {speak_date(d)}."


def check_once(state, alert=True, verbose=False, allow_reserve=False):
    """
    allow_reserve 只有常驻循环才传 True —— --once / --status / --table 绝不下单。

    提醒规则（按天去重）：同一天里，同一个「整条可订的出发日」或同一个「单晚空位」
    只提醒一次；只有出现**新的日期**才再提醒，而且只列新出现的那些。
    整条可订提醒过之后，它包含的那几晚也算提醒过，不会再以「单晚」重复报。
    """
    tgts = targets()
    if not tgts:
        log("   清单为空（或全部停用），本轮无事可做")
        return False
    grid, nreq = fetch_all(needed_dates(tgts))

    alerted = _day_bucket(state, "alerted")
    alerted.setdefault("keys", [])
    done = set(alerted["keys"])
    counts = state.setdefault("counts", {})
    hits_by_track = {}                 # 线路 -> [(标签, 住宿点, 日期, 余量, 整组命中, 条目)]
    full_now = []                      # 本轮所有整条可订（给自动占位用，不受提醒去重影响）
    still_known = {}                   # 线路 -> 今天已提醒过、此刻仍有位的出发日数
    interesting_lines = []
    n_open = n_closed = 0
    multi = len(watched_tracks()) > 1

    for track, label, legs, entry in tgts:
        leg_state = [(h, d, grid.get((track, h, d), "?")) for h, d in legs]
        for _, _, n in leg_state:
            if n is None:
                n_closed += 1
            else:
                n_open += 1
        people = entry.get("people", 1)
        avail = [(h, d, n) for h, d, n in leg_state
                 if isinstance(n, int) and n >= people]
        full = bool(legs) and len(avail) == len(legs)
        itinerary_mode = watchlist.mode_for(track) == "itinerary"

        if avail or verbose:
            mark = "🎉" if full else ("✨" if avail else "  ")
            detail = "  ".join(
                f"{h.split()[0][:4]} {d[5:]}:" + ("关" if n is None else str(n))
                for h, d, n in leg_state)
            prefix = f"[{track.replace(' Track', '')}] " if multi else ""
            interesting_lines.append(f" {mark} {prefix}{label} 出发 | {detail}")

        if full and itinerary_mode:
            full_now.append((track, label, entry))
        if not alert or not avail:
            continue

        if full and itinerary_mode:
            fkey = f"{track}|FULL|{label}"
            if fkey in done:
                still_known[track] = still_known.get(track, 0) + 1
                continue
            for h, d, n in avail:
                hits_by_track.setdefault(track, []).append((label, h, d, n, True, entry))
            done.add(fkey)
            done.update(f"{track}|{h}|{d}" for h, d, _ in avail)
            continue

        if cfg_for(track, "REQUIRE_FULL_ITINERARY", False) and itinerary_mode:
            continue
        for h, d, n in avail:
            key = f"{track}|{h}|{d}"
            if key in done:
                continue
            hits_by_track.setdefault(track, []).append((label, h, d, n, full, entry))
            done.add(key)

    alerted["keys"] = sorted(done)
    for (track, h, d), n in grid.items():
        counts[f"{track}|{h}|{d}"] = n

    for ln in interesting_lines:
        log(ln)
    if not interesting_lines:
        log(f"   {nreq} 次请求 | {len(grid)} 个格子 | 开放 {n_open} / 季外 {n_closed} | 全部无票")
    elif not hits_by_track and alert:
        log("   （都是今天提醒过的，不重复提醒）")

    for track, hits in hits_by_track.items():
        itinerary_mode = watchlist.mode_for(track) == "itinerary"
        body = summarize_hits([x[:5] for x in hits], itinerary_mode)
        if still_known.get(track):
            body += f"  （另有 {still_known[track]} 个今天已提醒过的出发日仍可订）"
        notify(alert_title(track, hits, itinerary_mode), body, track=track,
               speech=alert_speech(track, hits, itinerary_mode))

    # 自动占位：看的是「此刻整条可订」，不管今天提醒过没有；
    # 同一出发日一天最多成功占 1 次、最多尝试 2 次（你没付款就说明不想要，别反复锁别人的位）
    if allow_reserve and wc.AUTO_RESERVE:
        tries = _day_bucket(state, "reserve").setdefault("tries", {})
        for track, label, entry in full_now:
            if not entry.get("auto_reserve"):
                continue
            k = f"{track}|{label}"
            t = tries.get(k, {"attempts": 0, "success": False})
            if t["success"] or t["attempts"] >= 2:
                continue
            t["attempts"] += 1
            r = auto_reserve(track, label, entry.get("people", 1))
            t["success"] = bool(r.get("reserved"))
            tries[k] = t
            break                        # 一轮只占一个，占完再说
    return bool(hits_by_track)


def print_table():
    """打印每条线路的整段日期 × 住宿点余量总表"""
    tgts = targets()
    if not tgts:
        log("清单为空（或全部停用）")
        return
    grid, nreq = fetch_all(needed_dates(tgts))
    log(f"{tracks_label()} | {nreq} 次请求，{len(grid)} 个格子")
    min_people = {}
    for t, _, _, e in tgts:
        min_people[t] = min(min_people.get(t, 99), e.get("people", 1))

    for track in watched_tracks():
        sub = {(h, d): v for (t, h, d), v in grid.items() if t == track}
        if not sub:
            continue
        dates = sorted({d for _, d in sub})
        huts = [h for h in track_info(track)["huts"] if any(h == hh for hh, _ in sub)]
        if not huts:
            huts = sorted({h for h, _ in sub})
        print(f"\n── {track} ──")
        def short(h):
            """压短名字但保留 Hut/Campsite 区分，否则同名的小屋和营地会分不出来"""
            kind = "营" if "campsite" in h.lower() else ("屋" if "hut" in h.lower() else "")
            base = re.sub(r"\s*(Hut|Campsite|Shelter|Bunkroom)\s*$", "", h, flags=re.I)
            base = base[:8]
            return f"{base}{kind}"

        labels = [short(h) for h in huts]
        w = [max(6, len(l) + 1) for l in labels]
        header = "  " + f"{'住宿日':<12}" + "".join(
            f" {labels[i]:>{w[i]}}" for i in range(len(huts)))
        print(header)
        print("  " + "-" * (len(header) - 2))
        for d in dates:
            cells, hot = [], False
            for i, h in enumerate(huts):
                v = sub.get((h, d), "?")
                if v is None:
                    txt = "关"
                elif v == "?":
                    txt = "?"
                else:
                    txt = str(v)
                    if v >= min_people.get(track, 1):
                        hot = True
                cells.append(f" {txt:>{w[i]}}")
            print(f"  {d:<12}" + "".join(cells) + ("  ⬅ 有位" if hot else ""))


# ── 自动占位（可选，需要 playwright）──────────────────────────

async def hold_for_payment(page, browser, minutes=25):
    """
    占住后把窗口留给你付款。每 30 秒写一次心跳 —— 以前这 30 分钟里主循环不写心跳，
    看门狗以为进程死了，15:05 把它连同付款窗口一起杀掉（2026-09-27）。
    你关掉窗口、或 25 分钟购物车过期，就结束。
    """
    import asyncio
    end = time.time() + minutes * 60
    log(f"   🕒 窗口留给你付款，最多 {minutes} 分钟（关掉窗口即结束）")
    while time.time() < end:
        write_heartbeat(phase="holding", hold_left_min=round((end - time.time()) / 60, 1))
        if page.is_closed() or not browser.is_connected():
            log("   🪟 付款窗口已被关闭，结束等待")
            return
        await asyncio.sleep(30)
    log("   ⌛ 25 分钟到了，购物车应已过期，关闭窗口")


def auto_reserve(track, start_date, people=1):
    """
    登录 → 搜索 → 选格子 → Reserve → 填表 → Book Great Walk → **核对购物车**。
    返回 {"reserved": 是否锁住了位子, "cart": 购物车条数, "error": 错误}。
    """
    result = {"reserved": False, "cart": 0, "error": None}
    log(f"🤖 AUTO_RESERVE: 尝试占位 {track} {start_date} ...")
    write_heartbeat(phase="reserving")
    try:
        import asyncio
        from playwright.async_api import async_playwright
        import book
        import config
    except Exception as e:
        log(f"   ⚠️ 缺少 playwright / book.py，跳过自动占位: {e}")
        result["error"] = str(e)
        return result
    itin = itinerary_for(track)
    config.GREAT_WALK = track
    config.START_DATE = start_date
    config.NUM_PEOPLE = people
    config.NUM_NIGHTS = len(itin)
    shots = os.path.join(HERE, "reserve_shots")
    os.makedirs(shots, exist_ok=True)
    stamp = datetime.now().strftime("%m%d-%H%M%S")
    md = lambda d: f"{int(d[5:7])}/{int(d[8:10])}"

    async def snap(page, name):
        try:
            await page.screenshot(path=os.path.join(shots, f"{stamp}-{name}.png"), full_page=True)
        except Exception:
            pass

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, slow_mo=60)
            ctx = await browser.new_context(viewport={"width": 1400, "height": 900},
                                            locale="en-NZ", timezone_id="Pacific/Auckland")
            page = await ctx.new_page()
            try:
                await page.goto("https://bookings.doc.govt.nz/Web/#!greatwalk-result",
                                wait_until="domcontentloaded")
                await book.show_banner(page, f"🤖 自动占位进行中：{track} {start_date} 出发 —— 请不要关闭这个窗口")
                await book.login(page)
                write_heartbeat(phase="reserving")
                await book.fill_search_form(page)
                ok = await book.select_huts(page, datetime.strptime(start_date, "%Y-%m-%d"), itin)
                if not ok and await book.occupant_modal_open(page):
                    log("   ℹ️ select_huts 报失败，但弹窗是开着的 —— 位子已锁，继续填表")
                    ok = True
                await snap(page, "1-reserve")
                if not ok:
                    result["error"] = "Reserve 没成功（可能被别人先抢了）"
                    return
                result["reserved"] = True
                log("   ✅ Reserve 成功，位子已锁 25 分钟，继续填表…")
                write_heartbeat(phase="reserving")
                filled = await book.fill_occupant_details(page)
                await snap(page, "2-occupant")
                if filled:
                    await book.book_great_walk(page)
                n, left = await book.cart_status(page)
                await snap(page, "3-cart")
                result["cart"] = n
                if n:
                    log(f"   🛒 购物车核对：{n} 条，剩余 {left or '?'}")
                    await book.show_banner(page, f"🤖 已替你占位 {track} {start_date} 出发（{n} 晚）"
                                                 f"—— 请在这个窗口付款，剩余 {left or '约 25 分钟'}")
                else:
                    log("   ⚠️ 购物车是空的 —— 位子锁住了但没进购物车，需要你在窗口里手动完成")
                    await book.show_banner(page, "🤖 位子已锁但没进购物车 —— 请在这个窗口里手动完成填表和付款")

                title = (f"🤖 已占位 {track.replace(' Track','')} {md(start_date)} 出发，去付款！"
                         if n else f"⚠️ {track.replace(' Track','')} {md(start_date)} 已锁位但需你手动完成")
                body = ("位子锁 25 分钟。⚠️ 必须在 Mac 上弹出的那个浏览器窗口里付款——"
                        "换设备登录看不到这个购物车。") if n else \
                       "Reserve 成功但没能自动加进购物车。去 Mac 上那个窗口里手动填表、付款，25 分钟内有效。"
                notify(title, body, track=track, kind="reserve",
                       speech=f"{track} reserved, departing {speak_date(start_date)}. "
                              f"Please pay on the Mac within 25 minutes.")
                await hold_for_payment(page, browser)
            except Exception as e:
                result["error"] = f"{type(e).__name__}: {e}"
                log(f"   ⚠️ 占位出错: {result['error'][:200]}")
                await snap(page, "error")
                if result["reserved"] and not page.is_closed():
                    await hold_for_payment(page, browser)   # 位子锁着就别白白放掉
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

    asyncio.run(run())
    if not result["reserved"]:
        notify(f"⚠️ {track.replace(' Track','')} {md(start_date)} 自动占位没成功",
               f"原因：{result['error'] or '未知'}。票可能还在，去手动抢。", track=track,
               kind="reserve", speech=f"Auto reserve for {track} failed. Please book manually.")
    write_heartbeat(phase="checking")
    return result


# ── 主循环 ───────────────────────────────────────────────────

_WAKE = False


def _on_wake(signum, frame):
    """收到 SIGUSR1：界面刚保存了清单，提前结束本轮睡眠。
    不打断正在进行的事（比如自动占位开着浏览器等你付款）。"""
    global _WAKE
    _WAKE = True


def interruptible_sleep(seconds):
    """查询途中收到的叫醒不能丢：那一轮用的还是旧清单，醒来要立刻再查"""
    global _WAKE
    end = time.time() + seconds
    while time.time() < end:
        if _WAKE:
            _WAKE = False
            log("   ⏰ 被界面叫醒，立即按新清单检查")
            return
        time.sleep(1)


def watchlist_signature():
    """清单内容指纹 —— 常驻循环每轮比对，界面改了就在日志里说一声并立即生效"""
    try:
        import hashlib
        return hashlib.md5(json.dumps(watchlist.load()["entries"], sort_keys=True,
                                      ensure_ascii=False).encode()).hexdigest()
    except Exception:
        return ""


def describe_watchlist():
    act = watchlist.active()
    if not act:
        log("🎯 关注清单为空（或全部停用）—— 盯梢空转中，去界面里加一条")
        return
    log(f"🎯 盯梢 {len(act)} 条关注：")
    for e in act:
        t = e["track"]
        if watchlist.mode_for(t) == "itinerary":
            what = " → ".join(itinerary_for(t))
        else:
            what = f"任意空位（{len(watched_huts(t, e))} 个住宿点）"
        flag = "  🤖自动占位" if (e.get("auto_reserve") and wc.AUTO_RESERVE) else ""
        log(f"   • {t}  {e['from']} ~ {e['to']}  {e.get('people',1)} 人  | {what}{flag}")
    tg = targets(act)
    nreq = sum(len(windows(v)) for v in needed_dates(tg).values())
    log(f"   模式: {'整条齐了才叫' if wc.REQUIRE_FULL_ITINERARY else '任意一晚有空就叫'}"
        f" | 间隔 {wc.POLL_SECONDS}s | 每轮 {nreq} 次请求")


def ensure_single_instance():
    """只允许一个常驻盯梢进程。撞车就安静退出（退出码 0，这样 launchd 的
    KeepAlive=SuccessfulExit:false 不会陷入重启循环）。

    只跟 watch.pid 里那个真正的守护进程比对 —— 一次性查询不参与判断。"""
    other = daemon_pid()
    if other:
        log(f"已有常驻盯梢进程在跑 (PID {other})，本次退出，不重复占用。")
        sys.exit(0)
    write_pidfile()


def health(quiet=False):
    """自检：进程在不在？心跳新不新？最近有没有连续失败？
    返回 (是否健康, 一句话说明)。退出码 0=健康 1=有问题。"""
    pids = watcher_pids()
    hb = read_heartbeat()
    poll = getattr(wc, "POLL_SECONDS", 600)
    stale_after = poll * 2.5 + 120      # 容忍抖动 + 一轮取数时间

    lines, ok = [], True
    if pids:
        lines.append(f"  进程      ✅ 在跑 (PID {', '.join(map(str, pids))})")
    else:
        lines.append("  进程      ❌ 没有盯梢进程")
        ok = False

    if not hb:
        lines.append("  心跳      ❌ 没有心跳文件（从没成功跑过一轮？）")
        ok = False
    else:
        age = time.time() - hb.get("ts", 0)
        fresh = age <= stale_after
        lines.append(f"  心跳      {'✅' if fresh else '❌'} {hb.get('iso','?')}"
                     f"（{age/60:.1f} 分钟前，阈值 {stale_after/60:.1f} 分钟）")
        if not fresh:
            ok = False
        f = hb.get("fails", 0)
        if hb.get("ok"):
            lines.append(f"  上轮取数  ✅ 成功")
        else:
            lines.append(f"  上轮取数  ❌ 失败 [连续 {f} 次] {hb.get('error','')}")
            if f >= MAX_SILENT_FAILURES:
                ok = False
        lines.append(f"  盯梢目标  {hb.get('track','?')}  每 {hb.get('poll','?')} 秒")

    st = load_state()
    n = len(st.get("counts", {}))
    lines.append(f"  已知格子  {n} 个住宿点-日期")

    if not quiet:
        print(f"\n{'✅ 盯梢健康' if ok else '🚨 盯梢有问题'}")
        print("\n".join(lines))
        if not ok:
            print(f"\n  重启：launchctl kickstart -k gui/{os.getuid()}/com.liuyong.milford-watch\n")
        else:
            print()
    return ok, "; ".join(l.strip() for l in lines[:3])


def watchdog():
    """看门狗：心跳过期就报警并拉起主进程。由 launchd 每 10 分钟跑一次。"""
    ok, summary = health(quiet=True)
    if ok:
        log("🐕 watchdog: 一切正常")
        return 0
    log(f"🐕 watchdog: 检测到异常 -> {summary}")
    label = "com.liuyong.milford-watch"
    r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
                       capture_output=True, text=True)
    restarted = r.returncode == 0
    log(f"🐕 watchdog: 重启{'成功' if restarted else '失败: ' + (r.stderr or '').strip()}")
    notify(f"🚨 {tracks_label()} 盯梢曾经停止",
           ("看门狗已自动重启它，盯梢已恢复。" if restarted
            else "看门狗尝试重启失败，需要你手动处理！") + f" 详情：{summary}", kind="health")
    return 0 if restarted else 1


def daily_ping(state):
    """每天推一次「还活着」，让你知道盯梢真的在跑，而不是在傻等一个死进程"""
    hour = getattr(wc, "DAILY_PING_HOUR", None)
    if hour is None:
        return
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if now.hour < hour or state.get("last_ping") == today:
        return
    state["last_ping"] = today
    n = len(state.get("counts", {}))
    notify(f"👀 {tracks_label()} 盯梢正常",
           f"今日已检查 {n} 个住宿点-日期，暂无空位。盯梢运行中。", kind="health")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只查一次")
    ap.add_argument("--status", action="store_true", help="只看当前情况，不报警")
    ap.add_argument("--table", action="store_true", help="打印整段余量总表")
    ap.add_argument("--test-notify", action="store_true",
                    help="发一条假的中奖通知，用来验证 webhook / 通知配置")
    ap.add_argument("--health", action="store_true",
                    help="自检：进程死活 + 心跳新鲜度 + 连续失败情况")
    ap.add_argument("--watchdog", action="store_true",
                    help="看门狗：不健康就报警并自动重启（给 launchd 定时调用）")
    args = ap.parse_args()

    if args.health:
        sys.exit(0 if health()[0] else 1)

    if args.watchdog:
        sys.exit(watchdog())

    if args.test_notify:
        if not wc.WEBHOOK_URL:
            log("⚠️ watch_config.py 里 WEBHOOK_URL 还是空的，只会走本机通知")
        notify("🎉 Milford 整条有票：2/14 出发（测试）",
               "【这是测试消息，不是真的有票】"
               "Clinton Hut 2027-02-14 余1; Mintaro Hut 2027-02-15 余1; Dumpling Hut 2027-02-16 余2",
               kind="test", speech="Test. Milford Track available, departing February 14.")
        return

    if args.table:
        print_table()
        return

    # 单实例锁只针对常驻循环；--once / --status 是一次性查询，不拦
    if not (args.once or args.status):
        ensure_single_instance()

    if not (args.once or args.status):
        import signal
        signal.signal(signal.SIGUSR1, _on_wake)

    state = load_state()
    log("=" * 62)
    describe_watchlist()

    cycle = 0
    fails = 0            # 连续失败轮数
    warned = False       # 已经就"连续失败"报过警了吗
    sig = watchlist_signature()
    try:
        while True:
            cycle += 1
            new_sig = watchlist_signature()
            if new_sig != sig:
                sig = new_sig
                log("📝 关注清单有改动，已按新清单继续：")
                describe_watchlist()
            write_heartbeat(phase="checking")     # 一开始就写，查询/占位再久也不会被看门狗误杀
            try:
                daemon = not (args.once or args.status)
                check_once(state, alert=not args.status,
                           verbose=args.status and cycle == 1,
                           allow_reserve=daemon)
                if WEBHOOK_FAILED:
                    log("   ↩️  推送失败，本轮状态不落盘 —— 下一轮会重新报这个空位")
                else:
                    save_state(state)
                if fails >= MAX_SILENT_FAILURES:
                    notify(f"✅ {tracks_label()} 盯梢已恢复",
                           f"连续失败 {fails} 轮后恢复正常，继续盯梢中", kind="health")
                fails, warned = 0, False
                write_heartbeat(cycle=cycle, ok=True, fails=0, phase="sleeping")
                daily_ping(state)
            except Throttled as e:
                fails += 1
                log(f"   ⚠️ 取数失败（限流/网络），本轮跳过 [连续第 {fails} 次]: {e}")
                write_heartbeat(cycle=cycle, ok=False, fails=fails, error=str(e)[:200])
            except Exception as e:
                fails += 1
                log(f"   ⚠️ 本轮出错 [连续第 {fails} 次]: {type(e).__name__}: {e}")
                write_heartbeat(cycle=cycle, ok=False, fails=fails,
                                error=f"{type(e).__name__}: {e}"[:200])

            # 连续失败到阈值就吼一声 —— 绝不让它默默地"一直没票"
            if fails >= MAX_SILENT_FAILURES and not warned and not args.status:
                warned = True
                notify(f"🚨 {tracks_label()} 盯梢出问题了",
                       f"连续 {fails} 轮取不到数据，现在的「无票」不可信，请检查。"
                       f"命令：cd {HERE} && python3 watch.py --health", kind="health")

            if args.once or args.status:
                if args.once and WEBHOOK_FAILED:
                    log("❌ 查到了空位但推送没发出去 —— 这次等于白跑，用非零退出码让工作流变红")
                    sys.exit(2)
                break
            nap = wc.POLL_SECONDS * random.uniform(0.75, 1.25)
            log(f"   💤 {nap/60:.1f} 分钟后再查")
            interruptible_sleep(nap)
    except KeyboardInterrupt:
        log("👋 停止盯梢")
    finally:
        if not WEBHOOK_FAILED:
            save_state(state)


if __name__ == "__main__":
    main()
