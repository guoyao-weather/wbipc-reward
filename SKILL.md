---
name: wbipc-reward
description: WorkBuddy 每日积分任务（签到 + Buddy 旅行 + 成长计划 7/14/28 天奖励）的 wbipc 兜底执行力。当本地登录态加密（$wbEncrypted / 需要 Electron 解密）导致读登录态路线报「登录态读取失败」时，改用本 skill 走桌面端 wbipc 命名管道直连官方接口完成任务。触发词：签到、每日签到、积分、Buddy 旅行、派猫猫、旅行领取、成长计划、连登奖励、daily-checkin、travel claim、growth claim、积分任务失败。
---

# wbipc-reward — 每日积分兜底执行力

## 它解决什么问题

WorkBuddy 桌面端 5.6.2 起，`workbuddy-desktop.info` 里的 `accessToken` 变成 `$wbEncrypted` 加密信封，
本地无法静态还原。任何「读本地登录态再去调 API」的路线（含 `workbuddy-reward-helper`）都会失败，
典型报错：

```
登录态读取失败：旧版 state.vscdb 需要 Electron 运行时解密（safeStorage），但未找到 Electron …
```

**换路线**：桌面端自己是 host，鉴权由它注入。走本机命名管道（wbipc）让它代为发起 HTTP 请求即可。

## 路线定位

任何「读本地登录态再去调 API」的路线（如旧的 state.vscdb / Cookie 重放方案）在登录态加密后都会失败。
本 skill 是当前唯一稳定路线：桌面端自己是 host，鉴权由它注入。
若你手上还有旧路线脚本，可以留着交叉验证，但定时任务请统一调本脚本——**单一入口、单一日志**。

本 skill 不修改、不覆盖其他任何 skill 或配置文件。

## 用法

```bash
"<python>" <本仓库路径>/scripts/reward.py            # 默认：签到 + 旅行 + 成长计划（定时任务用这个，含日志短路）
"<python>" ".../reward.py" status                   # 只读：签到/旅行状态
"<python>" ".../reward.py" growth status            # 只读：成长计划三档状态
"<python>" ".../reward.py" growth claim 7d          # 领取指定档（仅 available 才发）
"<python>" ".../reward.py" growth claim-all         # 只领所有可领档位
```

**成长计划命令说明（2026-11-18 新增，移植自 workbuddy-reward-helper v1.1.0）**
- `growth status`：纯只读 GET，不会领取任何东西。
- `growth claim <7d|14d|28d>` / `claim-all`：**先查后领**，仅服务端状态明确为 `available` 才发起一次 POST；
  已领（claimed）/未达标（locked）/状态未知一律跳过。
- 409 → 刷新后按已领取处理，**绝不重复 POST**；403 → 未达标，**绝不重试**。
- 连登天数与签到 streak 是两套口径：成长计划看 `/activity/growth/streak` 的 `streak.days`。

- 幂等，重复运行无副作用（今天已签到 / 额度已用完都返回正常状态，不算错误）。
- **默认运行即全套**：签到 + 旅行 + 成长计划自动领取（先查后领，无可领时零成本空转）。
- 输出单行 JSON，字段见下；**退出码语义化（v3）**：`0`=正常（含已办妥/短路）、`1`=有 failed/partial 项、`2`=通道不可用。上层自动化按退出码判断，不要逐字段拼状态。
- **日志短路（v3）**：默认流程先读当日 `logs/result.log`——若今日已有「签到成功/已签 + 旅行额度耗尽 + 成长计划无可领」且无 failed/abort 记录，直接返回 `{"skipped":"all_done_today"}`，**零网络请求**。午/晚兑底运行几乎零成本；当日有过失败则不短路，后续运行自动重走全流程（自愈）。
- **每次执行自动追加摘要到本 skill 的 `logs/result.log`**（与 helper 同格式：
  `[YYYY-MM-DD HH:MM:SS] checkin success credit=100 streak_days=28`），
  失败也留痕（`abort 通道不可用 ...`）。写日志失败静默跳过，不影响主流程。
- 无第三方依赖，Python 标准库即可。建议 Python 3.12+。

## 输出字段

```jsonc
{
  "ok": true,
  "checkin": {
    "status": "success | already_checked | failed",
    "credit": 100,            // success 时：本次获得积分
    "streak_days": 7,         // success 时：连续天数（以此为准）
    "detail": {...}           // 原始响应，排错用
  },
  "travel": {
    "status": "claimed | claimed_and_redeparted | departed | traveling | daily_limit_reached | no_active_buddy | failed",
    "reward_credit": 0,       // claimed 时：派遣奖励
    "claim_credit": 0,        // claimed 时：领取到手积分
    "detail": {...}
    // claimed_and_redeparted = 领完奖后当日额度未用完，已自动再派一程
  },
  "growth": {                 // 仅 growth 子命令返回
    "action": "status | claim | claim-all",
    "status": "ok | claimed_success | already_claimed | not_eligible | no_claimable_rewards | failed",
    "streak_days": 8,
    "tiers": {"7d": {"status": "claimed|available|locked", "credit": 0, "energy": 2, "cards": 1}, ...},
    "claimable": ["14d"],    // status 时：可领档位
    "post_count": 1           // claim 时：实际 POST 次数（成功路径恒为 1，不重试）
  }
}
```

## 关键口径（踩过的坑，别再犯）

1. **`GET/POST /billing/meter/checkin-status` 的 `data.streak_days` 恒为 0**，那是另一套活动口径。
   连续天数只看 `POST /billing/meter/daily-checkin` 的响应里的 `credit` 与 `streak_days`。
2. **`travel/status` 的 `data.state` 与 `daily_limit_reached` 是两个独立字段**，会不一致：
   - `state="idle"` + `limit=true`：当日派遣+领取已做完、当前无进行中旅行。
   - `state="traveling"` + `limit=true`：** trip 仍在途中且待会儿要领奖**，`arrive_at` 有值。
     —— 这种组合最容易误判，只信 `limit` 会被报成「已用完」而漏领。
   **判定顺序必须严格为**（有可执行动作的 state 优先于额度假信号）：
   ① `state == "arrived"` → 先 `claim`
   ② `state == "traveling"` → 途中，按时再来
   ③ `daily_limit_reached == true` → 额度用完（不是 failed）
   ④ `state == "idle"` → `depart`
   顺序错了会误判成失败，或漏领已在途/已抵达的奖励。
3. `depart` 在额度用完时返回 `400 daily limit reached` —— 这是正常响应，不是错误。
4. `claim` 无待领取时返回 `400 no unclaimed travel` —— 同样正常。
5. **`no active buddy`**（无 Buddy）是账号状态，不是故障：账号下还没领养 Buddy，
   旅行功能暂不可用。此时 `travel.status = "no_active_buddy"`，签到照常，不重试。
6. **wbipc 时灵时不灵**：取决于桌面端当时是否处于可响应状态，与 `endpoint.json` 新旧无关。
   v2 起脚本内建重试：握手失败自动等 5 秒重试一次，仍失败才报 `abort`；
   报 `桌面端未响应` 时不要反复重试，先确认桌面端在运行且已登录。

## 通道协议备忘（标准实现见 scripts/reward.py）

- 端点文件：`~/.workbuddy/wbipc/endpoint.json` → `{endpoint: \\.\pipe\wbipc-xxx, ticket}`
- 帧格式：每行一个 JSON（`json.dumps(o) + "\n"`），管道以 `open(pipe,'r+b',buffering=0)` 打开
- 三步握手：
  1. `{type:"session_hello", protocol_min:1, protocol_max:1, client_nonce, ticket_id: sha256(ticket)[:16]}`
     → 收 `session_challenge`（含 `server_nonce`）
  2. transcript = 依次 `4字节BE长度前缀 + 内容` 拼 `["wbipc-c", "1", endpoint, client_nonce, server_nonce]`
     `client_proof = base64url(HMAC-SHA256(key=ticket, msg=transcript))`
     发 `{type:"session_prove", protocol:1, client_proof}` → 收 `session_hello_ack`
  3. `{jsonrpc:"2.0", id, method:"broker/GetPipe", params:{pipe:"wb.request"}}` → 得到 channel（如 `c:wb.request`）
- 调接口：`{jsonrpc:"2.0", id, method:"<channel>/http.fetch", mode:"call",
  params:{method:"POST"|"GET", path:"/相对路径", body_b64:<标准base64>}}`
  返回 `{status, headers, body_b64}`。鉴权头由宿主注入；`path` 必须是相对路径。

## 接口清单

| 用途 | 方法 | 路径 | body |
|---|---|---|---|
| 签到状态 | POST | `/billing/meter/checkin-status` | — |
| 执行签到 | POST | `/billing/meter/daily-checkin` | — |
| 旅行状态 | GET | `/activity/growth/buddy/travel/status` | — |
| 派遣 | POST | `/activity/growth/buddy/travel/depart` | `{"location_id":1}` |
| 领取 | POST | `/activity/growth/buddy/travel/claim` | `{}` |
| 成长计划连登+三档状态 | GET | `/activity/growth/streak` | — |
| 成长计划兑换汇总（仅展示） | GET | `/activity/growth/redeem/summary` | — |
| 成长计划领取 | POST | `/activity/growth/redeem` | `{"tier":"7d|14d|28d","client_token":"redeem-<tier>-<uuid4>"}` |

## 安全边界

- 只做「读取 + 领取自身积分」，不发外部请求，凭据不落盘。
- 禁止走本地密钥派生 / 破解 `$wbEncrypted` 的路线 —— 与本 skill 的宿主注入思路相悖，也没必要。
- 所有请求都由桌面端宿主代发，凭据不出本机。
