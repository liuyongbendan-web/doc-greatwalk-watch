# ============================================================
#  DOC Great Walk 持续盯梢 - 配置文件
#
#  不想手改这个文件的话，跑：  python3 configure.py
# ============================================================

# --- 盯哪条线路 ---
# 可填全名或关键词（"milford" / "heaphy" / "kepler" 都行）
# 全部 10 条见 tracks.py
TRACK = ['Milford Track', 'Routeburn Track']

# --- 盯哪段日期 ---
# itinerary 模式下这是「出发日」区间；any 模式下是「住宿日」区间。含首尾。
#   Milford 2026/27 季最后一个可住宿日 2027-04-30，三连晚 => 最晚出发日 2027-04-28
WATCH_DATE_RANGE = ("2027-02-10", "2027-04-30")

# 额外单独指定的日期（可与区间叠加）
WATCH_START_DATES = []

# --- 人数（余量少于这个数不报警）---
PEOPLE = 1

# --- 盯梢模式 ---
#   "itinerary" = 连住整条行程。适合 Milford / Routeburn / Kepler 这类单向线，
#                 从出发日起逐晚住 ITINERARY 里的住宿点
#   "any"       = 该线路上任意住宿点、区间内任意日期出现空位就报
MODE = 'itinerary'

# itinerary 模式的连住顺序：第 N 项 = 出发日 + N 晚。留空 = 用 tracks.py 的预设行程。
# 多条线路时可以写成字典分别指定，例如：
#   ITINERARY = {"Milford Track": ["Clinton Hut","Mintaro Hut","Dumpling Hut"],
#                "Routeburn Track": ["Routeburn Flats Hut","Lake Mackenzie Hut"]}
# 留空 [] 则每条线路各用自己的预设行程 —— 通常这样就够了
ITINERARY = []

# any 模式下只盯这些住宿点（关键词匹配，如 ["Luxmore","Iris Burn"]）。留空 = 全部
HUTS_FILTER = []

# --- 只在整条行程全部有票时才报警？（仅 itinerary 模式有意义）---
# True  = 全部晚数同时有位才叫你
# False = 任意一晚出现空位就叫你（捡漏 / 拼行程用）
REQUIRE_FULL_ITINERARY = False

# --- 轮询间隔（秒），会自动加 ±25% 随机抖动 ---
POLL_SECONDS = 600

# --- 同一个空位，多少分钟内不重复报警 ---
REALERT_MINUTES = 60

# --- 每天推一次「我还活着」的时间（0-23 点），None = 关闭 ---
# 这样你能确认盯梢真的在跑，而不是在干等一个早就死掉的进程
DAILY_PING_HOUR = 9

# --- 报警方式 ---
NOTIFY_MACOS   = True    # macOS 通知中心 + 提示音
NOTIFY_SPEAK   = True    # 用 say 朗读（睡觉时能叫醒你）
NOTIFY_BELL    = True    # 终端响铃
OPEN_BROWSER   = True    # 命中时自动用默认浏览器打开 DOC 预订页
WEBHOOK_URL    = ""      # 手机推送：ntfy / Discord / Slack 的 webhook，自动适配格式

# --- 自动占位只允许抢这个日期范围（出发日）---
# None = 整个 WATCH_DATE_RANGE 都能抢
# 用法：盯梢范围放宽（什么时候有票都想知道），但只让它自动抢你真会去的那几周
# 例：AUTO_RESERVE_DATE_RANGE = ("2027-02-10", "2027-02-28")
AUTO_RESERVE_DATE_RANGE = None

# --- 只允许对这些线路自动占位 ---
# None = 所有盯的线路都能抢
# 例：只想让它自动抢 Milford，Routeburn 只报警让你自己决定
#     AUTO_RESERVE_TRACKS = ["Milford Track"]
AUTO_RESERVE_TRACKS = ["Milford Track"]

# --- 自动占位（危险）---
# True = 命中整条行程时，自动开有头浏览器、登录、选格子、点 Reserve 放进购物车
#        （不会付款，购物车通常保留 ~15 分钟）
# 需要 config.py 里的 EMAIL / PASSWORD 正确，且本机装了 playwright
# ⚠️ 目前只对 Milford Track + itinerary 模式生效（book.py 的行程逻辑是按 Milford 写的）
AUTO_RESERVE = True
