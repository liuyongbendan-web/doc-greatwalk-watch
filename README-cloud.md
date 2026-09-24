# DOC Great Walk 云端盯梢

每 15 分钟查一次新西兰 DOC Great Walk 的退票余量，有空位就推送到手机。
**本机 Mac 关机时也照跑** —— 这是它存在的唯一理由。

## 它做什么 / 不做什么

- ✅ 查余量（只读官方 JSON 接口，无需登录）
- ✅ 有票推送到手机（ntfy / Discord / Slack）
- ❌ **不会替你下单**。买票必须你自己在浏览器里完成
  （DOC 的购物车绑浏览器会话，云端占位了你手机上也看不到）

## 必须配一个 Secret

Settings → Secrets and variables → Actions → New repository secret

| Name | Value |
|---|---|
| `GW_WEBHOOK_URL` | 你的推送地址，例如 `https://ntfy.sh/你起的随机主题名` |

没配的话工作流会直接报错退出 —— 因为查到票却通知不到你，等于白跑。

**ntfy 最省事**：手机装 ntfy App → 订阅一个你自己起的随机主题名（等同密码，起长一点）
→ 把 `https://ntfy.sh/那个主题名` 填进 Secret。

## 改盯什么

编辑 `.github/workflows/watch.yml` 里的 env：

```yaml
GW_TRACK: "Milford Track,Routeburn Track"   # 逗号分隔，见 tracks.py 里的 10 条
GW_DATE_RANGE: "2027-02-10,2027-04-30"      # 出发日区间
GW_PEOPLE: "1"
```

## 收到推送后

DOC 站点手机可直接下单。提前在手机浏览器登录 DOC 账号、存好信用卡。
选线路 → 选日期 → 点绿色格子 → **Reserve**（之后购物车锁 25 分钟，付款不用抢）。

## 注意

- `config.py`（含明文账号密码）**不在这个仓库里**，`.gitignore` 已排除，别加进来。
- 本机还有一套完整版（含自动占位），在 Mac 上的 `doc-booking/`，这里只是云端兜底。
