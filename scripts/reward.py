# -*- coding: utf-8 -*-
"""
WorkBuddy 每日积分任务（签到 + Buddy 旅行 + 成长计划奖励）—— 走桌面端 wbipc 命名管道。

定位：这是**主力执行路线**（2026-10-27 起三条定时任务统一走本脚本）。
特性：纯 Python 标准库；幂等，重复运行无副作用；输出单行 JSON；结果追加到 logs/result.log。

用法：
    python reward.py                    # 默认：签到 + 旅行 + 成长计划自动领取
    python reward.py status             # 只读体检（不签不领不派）
    python reward.py growth status      # 只读：成长计划三档状态
    python reward.py growth claim 7d    # 领取指定档（仅 available 才发）
    python reward.py growth claim-all   # 只领所有可领档位

v2 增强（2026-11-18）：
    1. 默认流程附带成长计划领取（先查后领，无可领零成本空转）；
    2. 旅行领完奖后若当日额度未用完，自动再派一程（status=claimed_and_redeparted）；
    3. 管道握手失败自动等 5 秒重试一次，桌面端刚重启时不再误报。

v3 增强（2026-11-18 晚）：
    1. 日志短路：午/晚兜底运行先读当日日志，签到/旅行额度/成长计划均已办妥则
       零网络请求直接退出（skipped=all_done_today）；
    2. 退出码语义化：0=正常（含短路），1=有失败项，2=通道不可用；
    3. 日志轮转：result.log 超 300KB 自动归档为 result-<时间戳>.log。
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import uuid

TICKET_ID_LEN = 16
RECV_TIMEOUT = 20.0
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
LOG_FILE = os.path.join(LOG_DIR, "result.log")
LOG_ROTATE_BYTES = 300 * 1024


def log_result(lines):
    """追加摘要到 logs/result.log；超 300KB 先归档；任何异常静默吞掉，绝不影响主流程。"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        try:
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_ROTATE_BYTES:
                archive = os.path.join(
                    LOG_DIR, "result-%s.log" % time.strftime("%Y%m%d-%H%M%S"))
                os.replace(LOG_FILE, archive)
        except Exception:
            pass
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            for line in lines:
                f.write("[%s] %s\n" % (ts, line))
    except Exception:
        pass


def _today_all_done():
    """读当日日志判断是否已全部办妥（供午/晚兜底运行零成本短路）。

    判据（全部满足才短路）：
      ① 今日存在 checkin success / already_checked / skipped_checked_in 行；
      ② 今日存在 travel daily_limit_reached 行（该状态仅当日额度耗尽且猫空闲时产出，
         猫在途产出的是 traveling，不会误伤到站领取）；
      ③ 今日存在 growth claim-all no_claimable_rewards 行（或 ok）；
      ④ 今日无任何 failed / abort 行（自愈：失败过的日子不短路，后续运行会重走全流程）。
    """
    try:
        if not os.path.exists(LOG_FILE):
            return False
        today = time.strftime("%Y-%m-%d")
        ck = tv = gr = False
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if not line.startswith("[" + today):
                    continue
                if "checkin success" in line or "checkin already_checked" in line \
                        or "checkin skipped_checked_in" in line:
                    ck = True
                elif "travel daily_limit_reached" in line:
                    tv = True
                elif "growth claim-all no_claimable_rewards" in line \
                        or "growth claim-all ok" in line:
                    gr = True
                if " failed" in line or "abort" in line:
                    return False
        return ck and tv and gr
    except Exception:
        return False


def summarize(out):
    """把输出 JSON 压成与 helper 同格式的可读摘要行。"""
    lines = []
    ck = out.get("checkin") or {}
    if ck:
        p = ["checkin %s" % ck.get("status", "unknown")]
        if ck.get("credit") is not None:
            p.append("credit=%s" % ck["credit"])
        if ck.get("streak_days") is not None:
            p.append("streak_days=%s" % ck["streak_days"])
        if ck.get("status") == "failed":
            p.append("detail=%s" % json.dumps(ck.get("detail"), ensure_ascii=False)[:300])
        lines.append(" ".join(p))

    tv = out.get("travel") or {}
    if tv:
        data = (tv.get("raw") or {}).get("data") or {}
        q = ["travel %s" % tv.get("status", "unknown")]
        if tv.get("claim_credit") is not None:
            q.append("claim_credit=%s" % tv["claim_credit"])
        if tv.get("reward_credit") is not None:
            q.append("reward_credit=%s" % tv["reward_credit"])
        if data.get("record_id"):
            q.append("record_id=%s" % data["record_id"])
        arr = tv.get("arrive_at") or data.get("arrive_at")
        if arr:
            q.append("arrive_at=%s" % arr)
        if tv.get("status") == "failed":
            q.append("detail=%s" % json.dumps(tv.get("detail"), ensure_ascii=False)[:300])
        lines.append(" ".join(q))

    gr = out.get("growth") or {}
    if gr:
        g = ["growth %s" % gr.get("action", "?"), gr.get("status", "unknown")]
        if gr.get("streak_days") is not None:
            g.append("streak_days=%s" % gr["streak_days"])
        if gr.get("claimable"):
            g.append("claimable=%s" % ",".join(gr["claimable"]))
        if gr.get("claimed") and isinstance(gr.get("claimed"), list):
            g.append("claimed=%s" % ",".join(gr["claimed"]))
        if gr.get("post_count") is not None:
            g.append("post_count=%s" % gr["post_count"])
        if gr.get("reason"):
            g.append("reason=%s" % str(gr["reason"])[:200])
        lines.append(" ".join(g))
    return lines



def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode()


def b64d(s: str) -> bytes:
    return base64.b64decode(s)


class Channel:
    """wbipc 命名管道会话：握手 -> GetPipe -> http.fetch"""

    def __init__(self, cfg):
        self.endpoint = cfg["endpoint"]
        self.ticket = cfg["ticket"]
        self.f = open(self.endpoint, "r+b", buffering=0)
        self._id = 0
        self._buf = b""
        self.chan = None

    # --- 帧收发 ---

    def send(self, obj):
        self.f.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
        self.f.flush()

    def _read_frame(self, timeout):
        end = time.time() + timeout
        while time.time() < end:
            try:
                ch = self.f.read(1)
            except Exception:
                time.sleep(0.05)
                continue
            if not ch:
                time.sleep(0.03)
                continue
            if ch == b"\n":
                if not self._buf.strip():
                    self._buf = b""
                    continue
                raw, self._buf = self._buf, b""
                try:
                    return json.loads(raw.decode("utf-8"))
                except Exception:
                    return None
            self._buf += ch
            if len(self._buf) > 4_000_000:
                self._buf = b""
        return None

    def _wait(self, pred, timeout=RECV_TIMEOUT):
        end = time.time() + timeout
        while time.time() < end:
            m = self._read_frame(max(0.5, end - time.time()))
            if m is None:
                continue
            if pred(m):
                return m
        return None

    # --- 握手 ---

    def handshake(self):
        cnonce = secrets.token_hex(16)
        tid = hashlib.sha256(self.ticket.encode()).hexdigest()[:TICKET_ID_LEN]
        self.send({
            "type": "session_hello",
            "protocol_min": 1,
            "protocol_max": 1,
            "client_nonce": cnonce,
            "ticket_id": tid,
        })
        ch = self._wait(lambda m: m.get("type") == "session_challenge")
        if not ch:
            raise RuntimeError("桌面端未在 wbipc 通道上响应（请确认桌面端正在运行且已登录）")
        snonce = ch.get("server_nonce") or ch.get("nonce")
        if not snonce:
            raise RuntimeError("session_challenge 缺少 server_nonce")

        transcript = b"".join(
            len(p).to_bytes(4, "big") + p
            for p in (b"wbipc-c", b"1", self.endpoint.encode(), cnonce.encode(), snonce.encode())
        )
        proof = base64.urlsafe_b64encode(
            hmac.new(self.ticket.encode(), transcript, hashlib.sha256).digest()
        ).decode().rstrip("=")
        self.send({"type": "session_prove", "protocol": 1, "client_proof": proof})
        if not self._wait(lambda m: m.get("type") in ("session_hello_ack", "session_ack")):
            raise RuntimeError("握手未被确认（session_hello_ack 缺失）")

        self._id += 1
        i = self._id
        self.send({"jsonrpc": "2.0", "id": i, "method": "broker/GetPipe",
                   "params": {"pipe": "wb.request"}})
        got = self._wait(lambda m: m.get("id") == i)
        if not got:
            raise RuntimeError("broker/GetPipe 无响应")
        chan = ((got.get("result") or {}).get("channel") or "c:wb.request")
        self.chan = chan if str(chan).startswith("c:") else "c:" + str(chan)

    def _rpc(self, method, params, timeout=RECV_TIMEOUT):
        self._id += 1
        i = self._id
        self.send({"jsonrpc": "2.0", "id": i, "method": method, "mode": "call", "params": params})
        return self._wait(lambda m: m.get("id") == i, timeout=timeout)

    def api(self, method, path, body=None):
        params = {"method": method, "path": path}
        if body is not None:
            params["body_b64"] = b64e(json.dumps(body, separators=(",", ":")).encode())
        r = self._rpc("%s/http.fetch" % self.chan, params)
        if r is None:
            return {"err": "timeout"}
        if "error" in r:
            return {"err": r["error"]}
        res = r.get("result") or {}
        out = {"status": res.get("status")}
        if res.get("body_b64"):
            try:
                out["body"] = json.loads(b64d(res["body_b64"]).decode("utf-8"))
            except Exception:
                out["raw"] = b64d(res["body_b64"]).decode("utf-8", "replace")
        return out

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


def load_cfg():
    home = os.path.expanduser("~")
    ep = os.path.join(home, ".workbuddy", "wbipc", "endpoint.json")
    if not os.path.exists(ep):
        raise RuntimeError("未找到 %s，桌面端可能从未启动" % ep)
    cfg = json.load(open(ep, "r", encoding="utf-8"))
    if not cfg.get("endpoint") or not cfg.get("ticket"):
        raise RuntimeError("endpoint.json 缺少 endpoint 或 ticket")
    return cfg


# ---------- 业务逻辑 ----------

def do_checkin(ch):
    r = ch.api("POST", "/billing/meter/daily-checkin")
    if "err" in r:
        return {"status": "failed", "detail": r["err"]}
    body = r.get("body") or {}
    code, msg, data = body.get("code"), str(body.get("msg", "")), body.get("data") or {}
    already = code == 10001 or "已签到" in msg or "already" in msg.lower()
    if already:
        return {"status": "already_checked", "detail": body}
    if code in (0, 200, None) or body.get("success"):
        # 连续天数只信本接口返回的 streak_days
        return {"status": "success",
                "credit": data.get("credit"),
                "streak_days": data.get("streak_days"),
                "detail": body}
    if "no active buddy" in msg.lower() or "buddy" in msg.lower():
        return {"status": "no_active_buddy", "detail": body}
    return {"status": "failed", "detail": body}


def do_travel(ch):
    r = ch.api("GET", "/activity/growth/buddy/travel/status")
    if "err" in r:
        return {"status": "failed", "detail": r["err"]}
    body = r.get("body") or {}
    data = body.get("data") or {}
    state, limit = data.get("state"), bool(data.get("daily_limit_reached"))
    base = {"state": state, "raw": body}

    # 判定顺序不能改：有可执行动作的 state 优先于额度假信号
    if state == "arrived":
        credit = data.get("reward_credit")
        c = ch.api("POST", "/activity/growth/buddy/travel/claim", {})
        cb = c.get("body") or {}
        ok = cb.get("code") in (0, 200, None) or cb.get("success")
        if not ok:
            return dict(base, status="failed", reward_credit=credit,
                        claim_credit=(cb.get("data") or {}).get("credit"), detail=cb)
        # v2 增强：领完奖后若当日额度未用完且猫已空闲，立刻再派一程，不浪费额度
        r2 = ch.api("GET", "/activity/growth/buddy/travel/status")
        d2 = ((r2.get("body") or {}).get("data") or {})
        if d2.get("state") == "idle" and not d2.get("daily_limit_reached"):
            d = ch.api("POST", "/activity/growth/buddy/travel/depart", {"location_id": 1})
            pb = d.get("body") or {}
            ok2 = pb.get("code") in (0, 200, None) or pb.get("success")
            if ok2:
                return dict(base, status="claimed_and_redeparted", reward_credit=credit,
                            claim_credit=(cb.get("data") or {}).get("credit"), detail=pb)
        return dict(base, status="claimed", reward_credit=credit,
                    claim_credit=(cb.get("data") or {}).get("credit"), detail=cb)

    if state == "traveling":  # 途中：可能领过一轮后再次派遣，limit=true 也会伴随出现
        return dict(base, status="traveling", reward_credit=data.get("reward_credit"),
                    arrive_at=data.get("arrive_at"))

    if limit:
        return dict(base, status="daily_limit_reached")

    if state == "traveling":
        return dict(base, status="traveling")

    if state == "idle":
        d = ch.api("POST", "/activity/growth/buddy/travel/depart", {"location_id": 1})
        pb = d.get("body") or {}
        msg = str(pb.get("msg", ""))
        ok = pb.get("code") in (0, 200, None) or pb.get("success")
        if not ok and "limit" in msg.lower():
            return dict(base, status="daily_limit_reached", detail=pb)
        if not ok and "buddy" in msg.lower():
            return dict(base, status="no_active_buddy", detail=pb)
        return dict(base, status="departed" if ok else "failed", detail=pb)

    return dict(base, status="unknown_state")


def do_checkin_status(ch):
    """只读：查签到状态（不签到）。"""
    r = ch.api("POST", "/billing/meter/checkin-status", {})
    if "err" in r:
        return {"status": "failed", "detail": r["err"]}
    body = r.get("body") or {}
    data = body.get("data") or {}
    return {"status": "ok", "today_checked_in": data.get("today_checked_in"),
            "streak_days": data.get("streak_days"), "detail": body}


def do_travel_status(ch):
    """只读：查旅行状态（不派遣、不领取）。"""
    r = ch.api("GET", "/activity/growth/buddy/travel/status")
    if "err" in r:
        return {"status": "failed", "detail": r["err"]}
    body = r.get("body") or {}
    data = body.get("data") or {}
    return {"status": "ok", "state": data.get("state"),
            "daily_limit_reached": data.get("daily_limit_reached"),
            "arrive_at": data.get("arrive_at"), "detail": body}


# ---------- 成长计划（7/14/28 天奖励）----------
# 契约来源：workbuddy-reward-helper v1.1.0 references/endpoints.md（真实闭环验证）
#   GET  /activity/growth/streak         连登天数 + 三档兑换状态（claim 判定权威源）
#   GET  /activity/growth/redeem/summary 本月兑换汇总（仅展示对账，不参与判定）
#   POST /activity/growth/redeem         body {"tier": t, "client_token": ct}
#   client_token 规则：redeem-<tier>-<uuid4>，每次全新生成，不复用
# 安全红线：仅 available 才 POST；409 → 按已领取处理不重试；403 → 未达标不重试。

TIERS = ("7d", "14d", "28d")
TIER_DAYS = {"7d": 7, "14d": 14, "28d": 28}
GRANT_FIELDS = ("cards_granted", "cards_overflow", "credit_granted",
                "energy_granted", "chances_granted")


def _classify(raw, days, tier):
    """服务端状态优先；字段缺失才按连登天数兜底，绝不反向覆盖服务端结论。"""
    if isinstance(raw, str):
        v = raw.strip().lower()
        if v in ("claimed", "available", "locked"):
            return v
        return "unknown"
    if raw is None and isinstance(days, int) and tier in TIER_DAYS:
        return "available" if days >= TIER_DAYS[tier] else "locked"
    return "unknown"


def _fetch_streak(ch):
    """只读拉取 streak 并解析。返回 {"ok":True, ...} 或 {"ok":False,"error":...}"""
    r = ch.api("GET", "/activity/growth/streak")
    if "err" in r:
        return {"ok": False, "error": "网络/通道异常：%s" % r["err"]}
    body = r.get("body") or {}
    if r.get("status") != 200 or body.get("code") != 0:
        if r.get("status") == 401:
            return {"ok": False, "error": "令牌已过期（401），请打开桌面端刷新登录态"}
        return {"ok": False, "error": "streak 查询失败：HTTP %s code=%s msg=%s"
                % (r.get("status"), body.get("code"), body.get("msg"))}
    data = body.get("data") or {}
    streak = data.get("streak") or {}
    redemption = data.get("redemption_status") or {}
    days = streak.get("days")
    days = days if isinstance(days, int) else 0
    reward_by_tier = {}
    for t in (redemption.get("tiers") or []):
        if isinstance(t, dict) and t.get("tier") in TIERS:
            reward_by_tier[t["tier"]] = t
    tiers = {}
    for tier in TIERS:
        raw = redemption.get("tier_%s_status" % tier)
        rw = reward_by_tier.get(tier) or {}
        tiers[tier] = {
            "status": _classify(raw, days, tier),
            "raw_status": raw,
            "claimed_count": redemption.get("tier_%s_count" % tier),
            "credit": rw.get("credit"),
            "energy": rw.get("energy"),
            "cards": rw.get("cards"),
            "chances": rw.get("chances"),
        }
    return {"ok": True, "streak_days": days, "next_tier": streak.get("next_tier"),
            "next_tier_remaining": streak.get("next_tier_remaining"),
            "makeup_cards": (data.get("makeup_cards") or {}).get("balance"),
            "remaining_days": redemption.get("remaining_days"), "tiers": tiers}


def do_growth_status(ch):
    """只读：查询成长计划三档状态，绝不领取。"""
    st = _fetch_streak(ch)
    if not st.get("ok"):
        return {"action": "status", "status": "failed", "reason": st["error"]}
    tiers = st["tiers"]
    return {"action": "status", "status": "ok", "read_only": True,
            "streak_days": st["streak_days"], "next_tier": st["next_tier"],
            "next_tier_remaining": st["next_tier_remaining"],
            "makeup_cards": st["makeup_cards"], "remaining_days": st["remaining_days"],
            "tiers": tiers,
            "claimable": [t for t in TIERS if tiers[t]["status"] == "available"],
            "claimed": [t for t in TIERS if tiers[t]["status"] == "claimed"],
            "locked": [t for t in TIERS if tiers[t]["status"] == "locked"]}


def _post_redeem(ch, tier):
    """对明确 available 的档位单次 POST，失败不重试，POST 后刷新确认。"""
    ct = "redeem-%s-%s" % (tier, uuid.uuid4())
    r = ch.api("POST", "/activity/growth/redeem", {"tier": tier, "client_token": ct})
    base = {"action": "claim", "tier": tier, "post_count": 1}
    if "err" in r:
        return dict(base, status="failed", reason="领取请求通道异常：%s" % r["err"])
    code = r.get("status")
    body = r.get("body") or {}
    msg = str(body.get("msg") or body.get("message") or "")
    if code == 200 and body.get("code") == 0:
        data = body.get("data") or {}
        return dict(base, status="claimed_success",
                    granted={k: data.get(k) for k in GRANT_FIELDS})
    if code == 409:
        after = _fetch_streak(ch)
        if after.get("ok") and after["tiers"][tier]["status"] == "claimed":
            return dict(base, status="already_claimed", reason="409 冲突，刷新确认为已领取")
        return dict(base, status="conflict", reason="409（本月已领取或冲突），不重试 msg=%s" % msg)
    if code == 403:
        return dict(base, status="not_eligible", reason="403 连登天数不足，不重试")
    if code == 401:
        return dict(base, status="failed", reason="令牌已过期（401），请打开桌面端刷新登录态")
    return dict(base, status="failed", reason="领取失败：HTTP %s code=%s msg=%s"
                % (code, body.get("code"), msg))


def do_growth_claim(ch, tier):
    """先查后领：仅真实状态 available 才 POST，其余一律跳过。"""
    if tier not in TIERS:
        return {"action": "claim", "tier": tier, "status": "failed",
                "reason": "档位非法（可选：7d/14d/28d）", "post_count": 0}
    st = _fetch_streak(ch)
    if not st.get("ok"):
        return {"action": "claim", "tier": tier, "status": "failed",
                "reason": st["error"], "post_count": 0}
    state = st["tiers"][tier]["status"]
    if state == "claimed":
        return {"action": "claim", "tier": tier, "status": "already_claimed",
                "reason": "该档位本月已领取", "post_count": 0}
    if state == "locked":
        return {"action": "claim", "tier": tier, "status": "not_eligible",
                "reason": "未达标（当前连登 %d 天）" % st["streak_days"], "post_count": 0}
    if state != "available":
        return {"action": "claim", "tier": tier, "status": "unknown_state",
                "reason": "状态未知（raw=%s），保守不领取" % st["tiers"][tier]["raw_status"],
                "post_count": 0}
    return _post_redeem(ch, tier)


def do_growth_claim_all(ch):
    """按 7d→14d→28d 顺序，只领 available 档位；每领一档后以刷新状态继续判断。"""
    st = _fetch_streak(ch)
    if not st.get("ok"):
        return {"action": "claim-all", "status": "failed", "reason": st["error"], "post_count": 0}
    tiers_state = st["tiers"]
    claimed_ok, skipped, results, post_count, failures = [], [], {}, 0, 0
    for tier in TIERS:
        if tiers_state[tier]["status"] != "available":
            skipped.append({"tier": tier, "status": tiers_state[tier]["status"]})
            continue
        r = _post_redeem(ch, tier)
        post_count += r.get("post_count", 0)
        results[tier] = r
        if r.get("status") in ("claimed_success", "already_claimed"):
            claimed_ok.append(tier)
        else:
            failures += 1
        after = _fetch_streak(ch)
        if after.get("ok"):
            tiers_state = after["tiers"]
    out = {"action": "claim-all", "post_count": post_count,
           "claimed": claimed_ok, "skipped": skipped, "results": results}
    if not results:
        out.update(status="no_claimable_rewards", reason="无可领取奖励（三档均非 available）")
    elif failures:
        out.update(status="partial", reason="部分档位未确认成功（见 results），未重试")
    else:
        out.update(status="ok")
    return out


def _open_channel():
    """建通道并握手；失败等 5 秒重试一次（桌面端刚重启时管道偶发无响应）。"""
    last_err = None
    for attempt in (1, 2):
        try:
            cfg = load_cfg()
            ch = Channel(cfg)
            ch.handshake()
            return ch
        except Exception as e:
            last_err = e
            try:
                ch.close()
            except Exception:
                pass
            if attempt == 1:
                time.sleep(5)
    raise last_err if last_err else RuntimeError("通道建立失败")


def main():
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "all"
    # v3：默认流程先做日志短路——当日已全部办妥则零网络请求退出（午/晚兜底运行几乎零成本）
    if cmd == "all" and _today_all_done():
        log_result(["skip 当日已全部办妥（日志短路，0 网络请求）"])
        print(json.dumps({"ok": True, "skipped": "all_done_today",
                          "note": "今日签到/旅行额度/成长计划均已办妥或无可办事项"},
                         ensure_ascii=False))
        return 0
    try:
        ch = _open_channel()
    except Exception as e:
        log_result(["abort 通道不可用(含重试) error=%s" % str(e)[:300]])
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 2
    try:
        if cmd == "status":
            out = {"ok": True, "checkin_status": do_checkin_status(ch),
                   "travel_status": do_travel_status(ch)}
        elif cmd == "growth":
            action = sys.argv[2].lower() if len(sys.argv) > 2 else "status"
            if action == "status":
                out = {"ok": True, "growth": do_growth_status(ch)}
            elif action == "claim":
                out = {"ok": True, "growth": do_growth_claim(ch, sys.argv[3] if len(sys.argv) > 3 else "")}
            elif action == "claim-all":
                out = {"ok": True, "growth": do_growth_claim_all(ch)}
            else:
                print(json.dumps({"ok": False, "error": "未知成长计划动作：%s（可选 status/claim/claim-all）" % action}, ensure_ascii=False))
                return 1
        else:
            out = {"ok": True, "checkin": do_checkin(ch), "travel": do_travel(ch)}
            # v2 增强：日常流程顺手领成长计划奖励（先查后领，无可领时零成本空转）
            try:
                out["growth"] = do_growth_claim_all(ch)
            except Exception as e:
                out["growth"] = {"action": "claim-all", "status": "failed", "reason": str(e)[:200]}
            # v3：三项全部无需动作时标记短路上限（仅标注，不改变行为）
            no_post_ck = out["checkin"].get("status") in ("already_checked",)
            no_post_tv = out["travel"].get("status") in (
                "traveling", "daily_limit_reached", "no_active_buddy", "unknown_state")
            no_post_gr = (out.get("growth") or {}).get("post_count", 1) == 0
            if no_post_ck and no_post_tv and no_post_gr:
                out["all_done"] = True
        log_result(summarize(out))
        print(json.dumps(out, ensure_ascii=False))
        # v3 退出码语义化：0=正常，1=有失败项（供上层自动化可靠判断）
        blob = json.dumps(out, ensure_ascii=False)
        has_fail = ('"status": "failed"' in blob or '"status": "partial"' in blob)
        return 1 if has_fail else 0
    finally:
        ch.close()


if __name__ == "__main__":
    sys.exit(main())
