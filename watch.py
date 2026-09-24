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
    """TRACK 可以是一条（字符串）或多条（列表）"""
    raw = wc.TRACK
    names = [raw] if isinstance(raw, str) else list(raw)
    return [tracks.resolve_name(n) for n in names]


def track_info(name=None):
    return tracks.get(name or watched_tracks()[0])


def track_name(name=None):
    return tracks.resolve_name(name) if name else watched_tracks()[0]


def tracks_label():
    ts = watched_tracks()
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
    """每轮写一次心跳，供 --health / --watchdog 判断死活"""
    hb = {"ts": time.time(), "iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
          "pid": os.getpid(), "track": tracks_label(), "poll": wc.POLL_SECONDS}
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


def start_dates():
    """配置里的区间 + 单点，合并去重排序"""
    out = set(wc.WATCH_START_DATES)
    rng = getattr(wc, "WATCH_DATE_RANGE", None)
    if rng:
        a = datetime.strptime(rng[0], "%Y-%m-%d")
        b = datetime.strptime(rng[1], "%Y-%m-%d")
        while a <= b:
            out.add(a.strftime("%Y-%m-%d"))
            a += timedelta(days=1)
    return sorted(out)


def watched_huts(track):
    """MODE='any' 时要盯的住宿点；HUTS_FILTER 留空则盯该线路全部"""
    all_huts = track_info(track)["huts"]
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


def targets():
    """
    返回 [(线路, 标签, [(住宿点, 日期), ...]), ...]。组内全部有位 = 「整组命中」。
      MODE='itinerary' : 一组 = 一个出发日的连住行程
      MODE='any'       : 一组 = 一个 (住宿点, 日期)
    """
    out = []
    for track in watched_tracks():
        mode = cfg_for(track, "MODE", "itinerary")
        if mode == "itinerary":
            itin = itinerary_for(track)
            for sd in start_dates():
                d0 = datetime.strptime(sd, "%Y-%m-%d")
                out.append((track, sd,
                            [(hut, (d0 + timedelta(days=i)).strftime("%Y-%m-%d"))
                             for i, hut in enumerate(itin)]))
        else:
            out += [(track, f"{hut} {d}", [(hut, d)])
                    for d in start_dates() for hut in watched_huts(track)]
    return out


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


def notify(title, body, url=None, track=None):
    log(f"🔔 {title} — {body}")
    if wc.NOTIFY_BELL:
        sys.stdout.write("\a" * 3)
        sys.stdout.flush()
    if wc.NOTIFY_MACOS and sys.platform == "darwin":
        subprocess.run(["osascript", "-e",
                        'display notification "{}" with title "{}" sound name "Glass"'
                        .format(body.replace('"', "'"), title.replace('"', "'"))],
                       capture_output=True)
    if wc.NOTIFY_SPEAK and sys.platform == "darwin":
        subprocess.run(["say", "-r", "180",
                        f"{(track or tracks_label())} has availability. Go book it now."],
                       capture_output=True)
    if wc.WEBHOOK_URL:
        try:
            send_webhook(wc.WEBHOOK_URL, title, body, url or BOOKING_URL, track)
            log("   webhook 已发送")
        except Exception as e:
            log(f"   ⚠️ webhook 失败: {e}")
            globals()["WEBHOOK_FAILED"] = True
    if wc.OPEN_BROWSER and sys.platform == "darwin":
        subprocess.run(["open", url or BOOKING_URL], capture_output=True)


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


def check_once(state, alert=True, verbose=False, allow_reserve=False):
    """allow_reserve 只有常驻循环才传 True。
    --once / --status / --table 是人工查询或 CI 巡检，绝不能顺手下单。"""
    tgts = targets()
    needed = {}
    for track, _, legs in tgts:
        needed.setdefault(track, set()).update(d for _, d in legs)
    grid, nreq = fetch_all(needed)

    now = time.time()
    counts, last_alert = state["counts"], state["last_alert"]
    hits_by_track = {}                 # 线路 -> [(标签, 住宿点, 日期, 余量, 整组命中)]
    interesting_lines = []
    seen_keys = set()                  # 同一个 hut-night 一轮只报一次
    n_open = n_closed = 0
    multi = len(watched_tracks()) > 1

    for track, label, legs in tgts:
        leg_state = [(h, d, grid.get((track, h, d), "?")) for h, d in legs]
        for _, _, n in leg_state:
            if n is None:
                n_closed += 1
            else:
                n_open += 1
        avail = [(h, d, n) for h, d, n in leg_state
                 if isinstance(n, int) and n >= wc.PEOPLE]
        full = len(avail) == len(legs)

        if avail or verbose:
            mark = "🎉" if full else ("✨" if avail else "  ")
            detail = "  ".join(
                f"{h.split()[0][:4]} {d[5:]}:" + ("关" if n is None else str(n))
                for h, d, n in leg_state)
            prefix = f"[{track.replace(' Track', '')}] " if multi else ""
            interesting_lines.append(f" {mark} {prefix}{label} 出发 | {detail}")

        if not alert:
            continue
        if cfg_for(track, "REQUIRE_FULL_ITINERARY", False) and not full:
            continue
        for h, d, n in avail:
            key = f"{track}|{h}|{d}"
            if key in seen_keys:
                continue
            prev = counts.get(key)
            fresh = prev in (None, 0) or (isinstance(prev, int) and n > prev)
            cooled = now - last_alert.get(key, 0) > wc.REALERT_MINUTES * 60
            if fresh or cooled:
                hits_by_track.setdefault(track, []).append((label, h, d, n, full))
                seen_keys.add(key)
                last_alert[key] = now

    for (track, h, d), n in grid.items():
        counts[f"{track}|{h}|{d}"] = n

    for ln in interesting_lines:
        log(ln)
    if not interesting_lines:
        log(f"   {nreq} 次请求 | {len(grid)} 个格子 | 开放 {n_open} / 季外 {n_closed} | 全部无票")

    # 每条线路单独发一条通知，标题里就带线路名，不会混在一起
    for track, hits in hits_by_track.items():
        full_hit = any(x[4] for x in hits)
        itinerary_mode = cfg_for(track, "MODE", "itinerary") == "itinerary"
        title = (f"🎉 {track} 整条行程有票！" if full_hit and itinerary_mode
                 else f"✨ {track} 有空位")
        notify(title, summarize_hits(hits, itinerary_mode), track=track)

        if full_hit and wc.AUTO_RESERVE and allow_reserve and itinerary_mode:
            # 只抢你真会去的日期；范围外的照样报警，但不下单
            rng = getattr(wc, "AUTO_RESERVE_DATE_RANGE", None)
            only = getattr(wc, "AUTO_RESERVE_TRACKS", None)
            if only and track not in [tracks.resolve_name(t) for t in only]:
                log(f"   ⏭  {track} 不在自动占位线路名单内，只报警不下单")
                continue
            bookable = [x[0] for x in hits if x[4]
                        and (not rng or rng[0] <= x[0] <= rng[1])]
            if bookable:
                auto_reserve(track, bookable[0])
            elif rng:
                log(f"   ⏭  {track} 命中的出发日不在自动占位范围 "
                    f"{rng[0]}~{rng[1]} 内，只报警不下单")
    return bool(hits_by_track)


def print_table():
    """打印每条线路的整段日期 × 住宿点余量总表"""
    tgts = targets()
    needed = {}
    for track, _, legs in tgts:
        needed.setdefault(track, set()).update(d for _, d in legs)
    grid, nreq = fetch_all(needed)
    log(f"{tracks_label()} | {nreq} 次请求，{len(grid)} 个格子")

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
                    if v >= wc.PEOPLE:
                        hot = True
                cells.append(f" {txt:>{w[i]}}")
            print(f"  {d:<12}" + "".join(cells) + ("  ⬅ 有位" if hot else ""))


# ── 自动占位（可选，需要 playwright）──────────────────────────

async def hold_browser_open(minutes=30):
    """占位成功后别关浏览器。后台运行（无 tty）时不能用 input()，否则会立刻 EOFError。"""
    import asyncio
    if sys.stdin and sys.stdin.isatty():
        print("浏览器保持打开，按 Enter 关闭...")
        await asyncio.get_event_loop().run_in_executor(None, input)
    else:
        log(f"   🕒 后台模式：浏览器保持打开 {minutes} 分钟，快去付款")
        await asyncio.sleep(minutes * 60)


def auto_reserve(track, start_date):
    log(f"🤖 AUTO_RESERVE: 尝试占位 {track} {start_date} ...")
    try:
        import asyncio
        from playwright.async_api import async_playwright
        import book
        import config
    except Exception as e:
        log(f"   ⚠️ 缺少 playwright / book.py，跳过自动占位: {e}")
        return
    itin = itinerary_for(track)
    config.GREAT_WALK = track
    config.START_DATE = start_date
    config.NUM_PEOPLE = wc.PEOPLE
    config.NUM_NIGHTS = len(itin)

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, slow_mo=60)
            ctx = await browser.new_context(viewport={"width": 1280, "height": 900},
                                            locale="en-NZ", timezone_id="Pacific/Auckland")
            page = await ctx.new_page()
            try:
                await page.goto("https://bookings.doc.govt.nz/Web/#!greatwalk-result",
                                wait_until="domcontentloaded")
                await book.login(page)
                await book.fill_search_form(page)
                ok = await book.select_huts(page, datetime.strptime(start_date, "%Y-%m-%d"), itin)
                if not ok and await book.occupant_modal_open(page):
                    # 兜底：即使上面判定失败，只要 Occupant Details 弹窗真的开着，
                    # 就说明位置已经 Reserve 住了，必须继续填表，不能白白放走
                    log("   ℹ️ select_huts 报失败，但弹窗是开着的 —— 继续填表")
                    ok = True
                if ok:
                    log("   ✅ Reserve 成功，25 分钟倒计时开始，继续填表…")
                    try:
                        await book.fill_occupant_details(page)
                        await book.book_great_walk(page)
                        await page.wait_for_timeout(2500)
                        try:                       # 停在购物车页面，方便直接付款
                            await page.click("#shopping-cart", timeout=5000)
                            await page.wait_for_timeout(3000)
                        except Exception:
                            pass
                        log("   🛒 已加入购物车，请在这个浏览器窗口里完成付款")
                    except Exception as e:
                        log(f"   ⚠️ 填表/加购物车出错，但位置已 Reserve 住了: {e}")

                # ⚠️ 购物车绑「浏览器会话」不绑账号（2026-08-23 实测）：
                #    换个浏览器/手机登录同一账号是看不到这个购物车的，
                #    所以必须在这个弹出的窗口里付款，且窗口不能关。
                notify(f"{track} 占位{'成功' if ok else '失败'}", 
                       ("已 Reserve 并加入购物车，25 分钟内付款。"
                        "⚠️ 必须在弹出的那个浏览器窗口里付款——换设备登录看不到这个购物车！")
                       if ok else "占位失败，请手动抢", track=track)
                await hold_browser_open()
            except Exception as e:
                log(f"   ⚠️ 占位出错: {e}")
                await hold_browser_open()
            finally:
                await browser.close()

    asyncio.run(run())


# ── 主循环 ───────────────────────────────────────────────────

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
            else "看门狗尝试重启失败，需要你手动处理！") + f" 详情：{summary}")
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
           f"今日已检查 {n} 个住宿点-日期，暂无空位。盯梢运行中。")


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
        notify(f"🎉 {tracks_label()} 整条行程有票！",
               "【这是测试消息，不是真的有票】"
               "Clinton Hut 2027-02-14 余1; Mintaro Hut 2027-02-15 余1; Dumpling Hut 2027-02-16 余2")
        return

    if args.table:
        print_table()
        return

    # 单实例锁只针对常驻循环；--once / --status 是一次性查询，不拦
    if not (args.once or args.status):
        ensure_single_instance()

    state = load_state()
    sds = start_dates()
    log("=" * 62)
    for t in watched_tracks():
        m = cfg_for(t, "MODE", "itinerary")
        items = itinerary_for(t) if m == "itinerary" else watched_huts(t)
        log(f"   • {t}: " + (" → ".join(items) if m == "itinerary"
                             else f"{len(items)} 个住宿点（任意空位）"))
    _need = {}
    for _t, _, _legs in targets():
        _need.setdefault(_t, set()).update(d for _, d in _legs)
    log(f"   模式: {'整条齐了才叫' if wc.REQUIRE_FULL_ITINERARY else '任意一晚有空就叫'}"
        f" | 间隔 {wc.POLL_SECONDS}s | 每轮 {sum(len(windows(v)) for v in _need.values())} 次请求")

    cycle = 0
    fails = 0            # 连续失败轮数
    warned = False       # 已经就"连续失败"报过警了吗
    try:
        while True:
            cycle += 1
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
                           f"连续失败 {fails} 轮后恢复正常，继续盯梢中")
                fails, warned = 0, False
                write_heartbeat(cycle=cycle, ok=True, fails=0)
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
                       f"命令：cd {HERE} && python3 watch.py --health")

            if args.once or args.status:
                if args.once and WEBHOOK_FAILED:
                    log("❌ 查到了空位但推送没发出去 —— 这次等于白跑，用非零退出码让工作流变红")
                    sys.exit(2)
                break
            nap = wc.POLL_SECONDS * random.uniform(0.75, 1.25)
            log(f"   💤 {nap/60:.1f} 分钟后再查")
            time.sleep(nap)
    except KeyboardInterrupt:
        log("👋 停止盯梢")
    finally:
        if not WEBHOOK_FAILED:
            save_state(state)


if __name__ == "__main__":
    main()
