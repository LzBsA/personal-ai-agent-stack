#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ysunet-panel: 校园网自动认证 + Web 状态面板 (单进程)
- keeper 线程: 周期探测外网, 不通则调用 ysunetlogin CLI 认证
- Web 面板: 实时状态 / 日志 / 手动"立即检测" / 手动认证(强制登录) /
            配置(账号/密码/运营商/断网窗口) / 清空日志
配置持久化: /data/config.json (优先于容器环境变量), 保存后立即生效且重启不丢
纯标准库, 无额外依赖。时区固定 UTC+8。

断网窗口(默认 23:30-05:30)可在网页面板里手动填写, 起止相同表示不做跳过。

手动认证(handover 文档第六节)与自动认证的区别:
    自动 = 探测(HTTPS 200) -> 不通才 login        (受 paused 与断网窗口限制)
    手动 = 跳过探测与断网窗口, 直接 login -> 复探一次  (用户点按钮显式触发)
同一时刻只允许一个探测/认证任务 (_busy 非阻塞锁), 避免并发登录触发
portal 侧的 workFlowNode is empty。
"""
import datetime
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------- 配置 ----------
DATA_DIR = os.getenv("DATA_DIR", "/data")
STATUS_FILE = os.path.join(DATA_DIR, "status.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
LOG_FILE = os.path.join(DATA_DIR, "ysunet.log")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
HTTP_PORT = int(os.getenv("HTTP_PORT", "8080"))
PROBE_URL = "https://www.baidu.com"
LOGIN_BIN = os.getenv("LOGIN_BIN", "ysunetlogin")   # 镜像内 /usr/local/bin/ysunetlogin
# ↑ 测试钩子: 把 LOGIN_BIN 设成 /bin/true, 可在"不真正认证"的前提下
#   验证整个"探测/认证/状态反馈"流程 (见交接文档第十三节)
LOGIN_TIMEOUT = 100
SERVICES = ["校园网", "中国联通", "中国电信", "中国移动"]
AUTO_CLEAN_SECONDS = 2 * 24 * 3600   # 每 2 天自动清理一次日志
# 断网窗口默认值 (学校每晚断电断网的时段); 起 = 含, 止 = 不含
DROP_WINDOW_DEFAULT = ("23:30", "05:30")

CST = datetime.timezone(datetime.timedelta(hours=8))
LOG_LINES = []                     # 内存环形日志
LOG_LINES_MAX = 500
_cfg = {                           # 运行配置 (os.environ 之上的一层覆盖)
    "username": os.getenv("RUIJIE_USERNAME", ""),
    "password": os.getenv("RUIJIE_PASSWORD", ""),
    "service": os.getenv("RUIJIE_SERVICE", "中国联通"),
    "window_start": DROP_WINDOW_DEFAULT[0],
    "window_end": DROP_WINDOW_DEFAULT[1],
}
_paused = False                    # 手动暂停自动认证 (持久化到 config.json)
_state = {
    "online": None,
    "window": False,
    "checking": False,
    "paused": False,
    "manual": False,               # 是否正在执行"手动认证"
    "last_manual": None,           # 最近一次手动认证的时间
    "manual_ok": None,             # 最近一次手动认证结果 True/False
    "manual_msg": None,            # 最近一次手动认证的提示文案
    "last_check": None,
    "last_change": None,
    "last_ok": None,
    "last_fail": None,
    "username": _cfg["username"],
    "service": _cfg["service"],
    "window_start": _cfg["window_start"],
    "window_end": _cfg["window_end"],
    "error": None,
}
_lock = threading.Lock()
# 同一时刻只允许一个"探测+认证"任务: keeper 的自动检测与网页的手动认证共用
_busy = threading.Lock()


def now_str():
    return datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def parse_hhmm(value):
    """把用户填的时间解析成 'HH:MM'; 不合法返回 None

    接受 '23:30' / '3:5' / '2330' 这几种写法, 统一补零成 'HH:MM'。
    """
    if value is None:
        return None
    s = str(value).strip().replace("：", ":")
    m = re.fullmatch(r"(\d{1,2}):(\d{1,2})", s) or re.fullmatch(r"(\d{2})(\d{2})", s)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return "%02d:%02d" % (hh, mm)


def hm_int(hhmm):
    """'23:30' -> 2330 (便于区间比较); 解析失败返回 None"""
    v = parse_hhmm(hhmm)
    return None if v is None else int(v[:2]) * 100 + int(v[3:])


def window_str():
    """当前断网窗口的显示文案, 如 '23:30-05:30'"""
    with _lock:
        return "%s-%s" % (_cfg.get("window_start", DROP_WINDOW_DEFAULT[0]),
                          _cfg.get("window_end", DROP_WINDOW_DEFAULT[1]))


def log(msg):
    ts = now_str()
    line = f"[{ts}] {msg}"
    LOG_LINES.append(line)
    if len(LOG_LINES) > LOG_LINES_MAX:
        del LOG_LINES[: len(LOG_LINES) - LOG_LINES_MAX]
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def set_state(**kw):
    with _lock:
        _state.update(kw)


def load_config():
    """启动时读持久化配置, 覆盖环境变量值"""
    global _cfg, _paused
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for k in ("username", "password", "service"):
                if saved.get(k):
                    _cfg[k] = str(saved[k])
            for k in ("window_start", "window_end"):     # 断网窗口可手动填写
                v = parse_hhmm(saved.get(k))
                if v:
                    _cfg[k] = v
            _paused = bool(saved.get("paused", False))
            _state["username"] = _cfg["username"]
            _state["service"] = _cfg["service"]
            _state["window_start"] = _cfg["window_start"]
            _state["window_end"] = _cfg["window_end"]
            _state["paused"] = _paused
            log("已载入持久化配置 (data/config.json)")
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"读取配置失败: {e}")


def save_config(username, password, service, window_start=None, window_end=None):
    """保存配置到 config.json 并更新运行时状态; password 为空表示不修改

    window_start / window_end 为 'HH:MM' 且必须已经过 parse_hhmm 校验;
    传 None 表示不改动该项。
    """
    global _cfg
    with _lock:
        if username:
            _cfg["username"] = username.strip()
        if password:
            _cfg["password"] = password
        if service:
            _cfg["service"] = service
        if window_start:
            _cfg["window_start"] = window_start
        if window_end:
            _cfg["window_end"] = window_end
        _state["username"] = _cfg["username"]
        _state["service"] = _cfg["service"]
        _state["window_start"] = _cfg["window_start"]
        _state["window_end"] = _cfg["window_end"]
        payload = dict(_cfg)       # 密码也持久化, 便于重启后免密自动认证
        payload["paused"] = _paused
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log(f"写入配置失败: {e}")
            return False
    return True


def get_public_cfg():
    """对外暴露配置 (不含密码明文)"""
    with _lock:
        return {
            "username": _cfg.get("username", ""),
            "service": _cfg.get("service", "中国联通"),
            "password_set": bool(_cfg.get("password", "")),
            "paused": _paused,
            "window_start": _cfg.get("window_start", DROP_WINDOW_DEFAULT[0]),
            "window_end": _cfg.get("window_end", DROP_WINDOW_DEFAULT[1]),
        }


def persist_paused():
    """把当前 paused 状态写入 config.json"""
    global _cfg
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({**_cfg, "paused": _paused}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"写入配置失败: {e}")
        return False
    return True


_last_clean = 0.0   # 上次自动清理时间戳 (进程内记忆; 重启后会以文件 mtime 兜底)


def clear_log(msg="日志已清空"):
    """清空内存日志与日志文件; msg 为清空后写入的提示行"""
    with _lock:
        LOG_LINES.clear()
    try:
        open(LOG_FILE, "w", encoding="utf-8").close()
    except Exception:
        pass
    log(msg)


def maybe_auto_clean():
    """距上次清理(进程内记录, 兜底看文件 mtime)超过 48h 则自动清空一次"""
    global _last_clean
    ref = _last_clean
    if ref <= 0:
        try:
            ref = os.path.getmtime(LOG_FILE)
        except OSError:
            ref = time.time()
    if time.time() - ref > AUTO_CLEAN_SECONDS:
        _last_clean = time.time()
        clear_log("日志已自动清理(每 48 小时)")


def set_paused(paused):
    """暂停/恢复自动认证。暂停后 keeper 不再做探测与登录"""
    global _paused
    with _lock:
        _paused = bool(paused)
        _state["paused"] = _paused
        ok = persist_paused()
    if _paused:
        log("自动认证已暂停(手动)")
    else:
        log("自动认证已恢复(手动)")
    return ok


def in_drop_window(now=None):
    """是否处于断网窗口内 (默认 23:30-05:30, 可在网页面板手动填写)

    区间语义: 起 = 含, 止 = 不含 (默认窗口 = 23:30 起, 到 05:30 结束)。
    - 起 > 止  → 跨零点, 如 23:30-05:30
    - 起 < 止  → 同一天内, 如 01:00-06:00
    - 起 = 止  → 视为不跳过 (想彻底关掉这个功能就这么填)
    """
    st, en = hm_int(_cfg.get("window_start")), hm_int(_cfg.get("window_end"))
    if st is None or en is None or st == en:
        return False
    now = now or datetime.datetime.now(CST)
    hm = now.hour * 100 + now.minute
    if st > en:
        return hm >= st or hm < en
    return st <= hm < en


def probe_ok():
    """真实外网探测: HTTPS 200 才算在线"""
    import requests
    try:
        r = requests.get(PROBE_URL, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0"})
        return r.status_code == 200
    except Exception:
        return False


def run_login():
    """用当前配置登录, 返回 (success, output_tail)"""
    with _lock:
        env = dict(os.environ)
        env["RUIJIE_USERNAME"] = _cfg.get("username", "")
        env["RUIJIE_PASSWORD"] = _cfg.get("password", "")
        env["RUIJIE_SERVICE"] = _cfg.get("service", "中国联通")
    try:
        proc = subprocess.run([LOGIN_BIN, "login"], env=env,
                              capture_output=True, text=True,
                              timeout=LOGIN_TIMEOUT)
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, out.strip()[-500:]
    except subprocess.TimeoutExpired:
        return False, "login timed out"
    except Exception as e:
        return False, f"exec error: {e}"


def check_once():
    """自动检测一轮: 探测外网, 不通则认证 (受 paused 与断网窗口限制)"""
    if not _busy.acquire(blocking=False):
        log("已有检测/认证任务在进行，本轮自动检测跳过")
        return False
    try:
        set_state(checking=True, error=None)
        if _paused:
            set_state(paused=True, checking=False, last_check=now_str())
            return False
        if in_drop_window():
            set_state(window=True, checking=False,
                      last_check=now_str(), online=False)
            log(f"断网窗口({window_str()})，跳过检测")
            return False
        set_state(window=False)

        was_offline = _state.get("online") is False
        online = probe_ok()
        if online:
            if was_offline:
                # 离线 -> 在线: 多半是上一轮认证延迟生效, 或学校侧自行恢复。
                # 这一轮没跑认证, 但网络确实回来了, 所以同样记一次"恢复"。
                set_state(online=True, checking=False, last_check=now_str(),
                          last_change=now_str(), last_ok=now_str())
                log("网络已恢复（本轮探测确认，未触发认证）")
                return True
            set_state(online=True, checking=False, last_check=now_str())
            log("外网正常，无需认证")
            return True

        log("外网不通，开始认证...")
        ok, tail = run_login()
        if ok:
            time.sleep(4)
            online = probe_ok()
            if online:
                set_state(online=True, checking=False, last_check=now_str(),
                          last_change=now_str(), last_ok=now_str())
                log("认证成功，网络已恢复")
                return True
            log(f"认证命令执行成功但网络仍不通，下轮重试。{tail}")
            set_state(checking=False, last_check=now_str(), last_fail=now_str(),
                      error="认证后探测仍失败")
            return False
        else:
            log(f"认证失败: {tail}")
            set_state(checking=False, last_check=now_str(), last_fail=now_str(),
                      error=tail[:200])
            return False
    finally:
        set_state(checking=False)
        _busy.release()


def friendly_login_error(tail):
    """把认证工具的输出翻译成一眼能看懂的提示 (含交接文档记录过的已知情况)"""
    t = (tail or "").strip()
    if "workFlowNode is empty" in t:
        return "portal 拒绝重复登录(会话半开 workFlowNode is empty)，等 1-2 分钟再点一次通常会成功"
    if "不在认证时段内" in t:
        return (f"现在是学校断网时段({window_str()})，portal 不提供认证，"
                "窗口结束后会自动重连，无需手动干预")
    if "Network connection failed" in t:
        return "认证服务器不可达，可能是校园网侧不稳定，请稍后重试"
    if "timed out" in t:
        return f"认证命令超时(超过 {LOGIN_TIMEOUT} 秒)，请检查网络后重试"
    if t.startswith("exec error"):
        return f"无法执行认证程序: {t}"
    # 兜底: 去掉工具抛出的英文外壳, 只留 portal 的失败原因
    m = re.search(r"失败原因[：:]\s*([^,，)）]+)", t)
    if m:
        return "portal 拒绝认证：" + m.group(1).strip()
    return t[-200:] if t else "认证程序没有返回任何信息"


def manual_precheck():
    """手动认证前置校验, 返回错误文案; None 表示可以执行"""
    with _lock:
        user, pwd = _cfg.get("username", ""), _cfg.get("password", "")
    if not user:
        return "尚未配置账号，请先在【认证配置】中填写账号并保存"
    if not pwd:
        return "尚未设置密码，请先在【认证配置】中填写密码并保存"
    if _busy.locked():
        return "已有检测/认证正在进行，请稍候再试"
    return None


def manual_login():
    """手动认证: 跳过探测与断网窗口判断, 直接执行一次认证登录再复探

    与自动认证复用同一套 _busy 锁, 两者不会并发;
    不受 paused 影响 (暂停只约束 keeper 的自动探测/登录)。
    """
    if not _busy.acquire(blocking=False):
        why = "已有检测/认证正在进行，请稍候再试"
        log(f"手动认证: 已忽略 ({why})")
        set_state(manual=False, last_manual=now_str(),
                  manual_ok=False, manual_msg=why)
        return False

    def finish(ok, m, **extra):
        set_state(manual=False, checking=False, last_manual=now_str(),
                  manual_ok=ok, manual_msg=m, last_check=now_str(), **extra)

    try:
        set_state(manual=True, checking=True, error=None,
                  manual_ok=None, manual_msg="正在认证…")
        with _lock:
            user = _cfg.get("username", "")
            svc = _cfg.get("service", "")
            paused = _paused
        log(f"手动认证: 开始 (账号={user} 运营商={svc}"
            + ("，自动认证处于暂停状态)" if paused else ")"))
        if in_drop_window():
            log(f"手动认证提示: 当前在断网窗口({window_str()})内，校园网已断开，认证可能失败")

        ok, tail = run_login()
        if not ok:
            reason = friendly_login_error(tail)
            log(f"手动认证失败: {reason}")
            finish(False, reason, last_fail=now_str(), error=reason[:200])
            return False

        time.sleep(4)
        if probe_ok():
            log("手动认证成功，网络已恢复")
            finish(True, "网络已恢复", online=True, last_ok=now_str(),
                   last_change=now_str())
            return True

        reason = "认证命令已执行成功，但外网探测仍不通，请稍后重试或查看日志"
        log(f"手动认证: {reason} {tail}")
        finish(False, reason, online=False, last_fail=now_str(),
               error=reason)
        return False
    except Exception as e:
        reason = f"手动认证异常: {e}"
        log(reason)
        set_state(manual=False, checking=False, last_manual=now_str(),
                  manual_ok=False, manual_msg=reason, error=str(e))
        return False
    finally:
        _busy.release()


def keeper_loop():
    log("ysunet-panel 启动, 每 %d 秒检测一次" % CHECK_INTERVAL)
    log("服务: %s  账号: %s  断网窗口: %s"
        % (_state["service"], _state["username"], window_str()))
    while True:
        try:
            check_once()
        except Exception as e:
            log(f"检测异常: {e}")
            set_state(error=str(e))
        try:
            if not _paused:
                maybe_auto_clean()
        except Exception:
            pass
        time.sleep(CHECK_INTERVAL)


# ---------- HTTP ----------
HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>校园网自动认证</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
     background:#0f1420;color:#e6ebf5;min-height:100vh;display:flex;justify-content:center;padding:24px 16px}
.card{background:#1a2233;border-radius:16px;padding:26px;max-width:600px;width:100%;
      box-shadow:0 8px 30px rgba(0,0,0,.4)}
h1{font-size:20px;margin-bottom:4px}
.sub{color:#8fa0bd;font-size:13px;margin-bottom:18px}
.status{display:flex;align-items:center;gap:14px;padding:18px;border-radius:12px;
        background:#101828;margin-bottom:14px}
.dot{width:16px;height:16px;border-radius:50%;flex-shrink:0}
.dot.on{background:#2ecc71;box-shadow:0 0 12px #2ecc71}
.dot.off{background:#e74c3c;box-shadow:0 0 12px #e74c3c}
.dot.window{background:#f39c12;box-shadow:0 0 12px #f39c12}
.dot.unknown{background:#7f8c8d}
.st-text{font-size:18px;font-weight:600}
.st-sub{font-size:12px;color:#8fa0bd;margin-top:3px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}
.box{background:#101828;border-radius:10px;padding:12px}
.box .k{font-size:11px;color:#8fa0bd;margin-bottom:4px}
.box .v{font-size:14px;word-break:break-all}
.sec-title{font-size:14px;font-weight:600;color:#c9d4e8;margin:16px 0 10px;
           display:flex;align-items:center;justify-content:space-between}
.cfgrow{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.cfgrow .full{grid-column:1/-1}
.cfgrow .k{font-size:11px;color:#8fa0bd;margin-bottom:4px}
input,select{width:100%;background:#0a0f1a;border:1px solid #2a3550;color:#e6ebf5;
             border-radius:8px;padding:9px 10px;font-size:13px;outline:none}
input:focus,select:focus{border-color:#2b6de8}
.hint{font-size:11px;color:#5c6a84;margin-top:6px}
.logbox{background:#0a0f1a;border-radius:10px;padding:12px;height:220px;
        overflow-y:auto;font-family:ui-monospace,Consolas,monospace;
        font-size:12px;line-height:1.6;color:#9fb0d0;white-space:pre-wrap;word-break:break-all}
.btnrow{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}
.btn{background:#2b6de8;border:none;color:#fff;padding:9px 16px;
     border-radius:8px;font-size:13px;cursor:pointer}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn:hover:not(:disabled){background:#3a7bf5}
.btn.ghost{background:#28344e}
.btn.ghost:hover:not(:disabled){background:#34405e}
.btn.danger{background:#3a2530;color:#ff9aa2}
.btn.danger:hover:not(:disabled){background:#4d2f3a}
.btn.teal{background:#0e7b86}
.btn.teal:hover:not(:disabled){background:#12909c}
.msg{font-size:12px;margin-top:8px;min-height:16px}
.msg.ok{color:#2ecc71}.msg.err{color:#e74c3c}
.foot{margin-top:16px;font-size:11px;color:#5c6a84;text-align:center}
</style>
</head>
<body>
<div class="card">
  <h1>🌐 校园网自动认证</h1>
  <div class="sub" id="svc">…</div>

  <div class="status" id="statusCard">
    <div class="dot unknown" id="dot"></div>
    <div><div class="st-text" id="stText">连接中…</div>
         <div class="st-sub" id="stSub"></div></div>
  </div>

  <div class="grid">
    <div class="box"><div class="k">上次检测</div><div class="v" id="lastCheck">—</div></div>
    <div class="box"><div class="k">最近成功</div><div class="v" id="lastOk">—</div></div>
    <div class="box"><div class="k">最近失败</div><div class="v" id="lastFail">—</div></div>
    <div class="box"><div class="k">当前账号</div><div class="v" id="curUser">—</div></div>
  </div>

  <div class="sec-title">⚙️ 认证配置 <span style="font-weight:400;font-size:11px;color:#5c6a84">密码留空则不修改</span></div>
  <div class="cfgrow">
    <div><div class="k">账号</div><input id="setUser" placeholder="学号" autocomplete="off"></div>
    <div><div class="k">运营商</div>
      <select id="setSvc"></select>
    </div>
    <div class="full"><div class="k">密码</div>
      <input id="setPass" type="password" placeholder="••••••••  (留空则不修改)" autocomplete="new-password">
      <div class="hint" id="passState"></div>
    </div>
    <div><div class="k">断网开始时间</div><input id="setWinStart" type="time"></div>
    <div><div class="k">断网结束时间</div><input id="setWinEnd" type="time"></div>
    <div class="full"><div class="hint" id="winState">
      即学校的断网（认证）时段，窗口内自动检测会跳过；可手动填写，保存后立即生效。
      起止填成同一时间 = 不跳过；跨零点（如 23:30 → 05:30）直接这样填。</div>
    </div>
  </div>
  <div class="msg" id="saveMsg"></div>
  <div class="btnrow">
    <button class="btn" id="saveBtn" onclick="saveCfg()">保存配置</button>
    <button class="btn ghost" id="btn" onclick="checkNow()">立即检测</button>
    <button class="btn teal" id="manualBtn" onclick="manualLogin()">手动认证</button>
    <button class="btn" id="pauseBtn" onclick="togglePause()" style="background:#7a4dd0">暂停自动认证</button>
  </div>
  <div class="hint" id="manualState">尚未手动认证过</div>

  <div class="sec-title">📜 运行日志
    <span style="font-weight:400;font-size:11px;color:#5c6a84">每 48h 自动清理</span>
    <button class="btn danger" style="padding:4px 12px;font-size:12px" onclick="clearLog()">清空日志</button>
  </div>
  <div class="logbox" id="log"></div>

  <div class="foot">自动认证每 5 分钟运行 · <span id="footWin">23:30-05:30</span> 断网窗口自动跳过 · 日志每 48 小时自动清理<br>
  手动认证绕过探测与断网窗口判断，直接执行一次认证（不受暂停影响）</div>
</div>
<script>
var _lastState={};          /* 最近一次 /api/status 的结果 */
var _DEF={window_start:'23:30',window_end:'05:30'};
function winLabel(s){
  /* 断网窗口文案: 优先用后端下发的配置, 没有就退回默认值 */
  s=s||{};
  var a=s.window_start||_DEF.window_start, b=s.window_end||_DEF.window_end;
  return a===b ? (a+' 起不跳过') : (a+'-'+b);
}
function st(state){
  _lastState=state||{};
  var el=document.getElementById('stText');
  var dot=document.getElementById('dot');
  dot.className='dot unknown';
  if(state.paused){
    el.textContent='自动认证已暂停';dot.className='dot window';
  }
  else if(state.online===true){el.textContent='在线';dot.className='dot on';}
  else if(state.online===false && state.window){el.textContent='断网窗口('+winLabel(state)+')';dot.className='dot window';}
  else if(state.online===false){el.textContent='离线';dot.className='dot off';}
  else {el.textContent='未知';}
  var sub=document.getElementById('stSub');
  sub.textContent = state.manual ? '正在手动认证…' :
    (state.checking ? '正在检测/认证…' :
    (state.paused ? '已手动暂停，不会自动探测或登录' :
    (state.last_check ? '上次检测: '+state.last_check : '')));
  document.getElementById('lastCheck').textContent=state.last_check||'—';
  document.getElementById('lastOk').textContent=state.last_ok||'—';
  document.getElementById('lastFail').textContent=state.last_fail||'—';
  document.getElementById('curUser').textContent=state.username||'—';
  document.getElementById('svc').textContent='运营商: '+(state.service||'—')+' · 自动检测';
  var b=document.getElementById('btn');
  b.disabled=state.checking||state.paused;
  b.textContent=state.manual?'立即检测':(state.checking?'检测中…':(state.paused?'(已暂停)':'立即检测'));
  var mb=document.getElementById('manualBtn');
  if(mb){
    mb.disabled=state.checking||state.manual;
    mb.textContent=state.manual?'认证中…':'手动认证';
  }
  var ms=document.getElementById('manualState');
  if(ms){
    if(state.manual){ms.textContent='正在执行手动认证，请稍候（最长约 2 分钟）…';}
    else if(state.last_manual){
      ms.textContent='最近手动认证: '+state.last_manual+(state.manual_ok===true?' ✓ 成功':' ✗ 失败');
    }
  }
  var pb=document.getElementById('pauseBtn');
  pb.textContent=state.paused?'恢复自动认证':'暂停自动认证';
  pb.style.background=state.paused?'#2ecc71':'#7a4dd0';
  var ft=document.getElementById('footWin');
  if(ft)ft.textContent=winLabel(state);
}
function logOut(t){var el=document.getElementById('log');el.textContent=t;el.scrollTop=el.scrollHeight;}
function msg(t,cls){var el=document.getElementById('saveMsg');el.textContent=t;el.className='msg '+(cls||'');}
function fillSvc(v){
  var sel=document.getElementById('setSvc');
  sel.innerHTML='';
  ['校园网','中国联通','中国电信','中国移动'].forEach(function(s){
    var o=document.createElement('option');o.value=s;o.textContent=s;
    if(s===v)o.selected=true;sel.appendChild(o);
  });
}
function loadCfg(){
  fetch('/api/settings').then(r=>r.json()).then(c=>{
    document.getElementById('setUser').value=c.username||'';
    fillSvc(c.service||'中国联通');
    document.getElementById('passState').textContent =
      c.password_set ? '✓ 密码已设置 (留空则保持不变)' : '✗ 尚未设置密码';
    document.getElementById('setWinStart').value=c.window_start||_DEF.window_start;
    document.getElementById('setWinEnd').value=c.window_end||_DEF.window_end;
    var ws=document.getElementById('winState');
    ws.textContent='即学校的断网（认证）时段，窗口内自动检测会跳过；可手动填写，保存后立即生效。'
      +' 起止填成同一时间 = 不跳过；跨零点（如 23:30 → 05:30）直接这样填。'
      +' 当前生效: '+winLabel(c);
    var pb=document.getElementById('pauseBtn');
    if(c.paused){pb.textContent='恢复自动认证';pb.style.background='#2ecc71';}
    else{pb.textContent='暂停自动认证';pb.style.background='#7a4dd0';}
  }).catch(()=>{});
}
function togglePause(){
  var pb=document.getElementById('pauseBtn');
  var target = pb.textContent.indexOf('暂停')===0 ? false : true;
  var isPausing = pb.textContent==='暂停自动认证';
  fetch('/api/pause',{method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify({paused:isPausing})})
    .then(r=>r.json()).then(res=>{
      if(res.ok){
        msg(isPausing?'自动认证已暂停':'自动认证已恢复','ok');
        refresh();
      }else msg(res.error||'操作失败','err');
    }).catch(()=>msg('网络错误','err'));
}
function saveCfg(){
  var b=document.getElementById('saveBtn');b.disabled=true;
  var body={username:document.getElementById('setUser').value.trim(),
            service:document.getElementById('setSvc').value,
            window_start:document.getElementById('setWinStart').value,
            window_end:document.getElementById('setWinEnd').value};
  var p=document.getElementById('setPass').value;
  if(p)body.password=p;
  if(!body.username){msg('账号不能为空','err');b.disabled=false;return;}
  if(!body.window_start||!body.window_end){
    msg('断网开始/结束时间不能为空（起止相同表示不跳过）','err');b.disabled=false;return;}
  fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify(body)})
    .then(r=>r.json()).then(res=>{
      if(res.ok){msg('配置已保存，立即生效（断网窗口 '+winLabel(res)+'）','ok');
        document.getElementById('setPass').value='';
        document.getElementById('passState').textContent='✓ 密码已设置 (留空则保持不变)';
        refresh(); loadCfg();
      }else msg(res.error||'保存失败','err');
    }).catch(()=>msg('网络错误','err'))
    .finally(()=>{b.disabled=false;});
}
function clearLog(){
  if(!confirm('确定清空全部日志？'))return;
  fetch('/api/clearlog',{method:'POST'}).then(r=>r.json()).then(()=>{
    logOut(''); refresh(); msg('日志已清空','ok');
  }).catch(()=>msg('清空失败','err'));
}
function refresh(){
  fetch('/api/status').then(r=>r.json()).then(s=>{
    st(s);
    if(!s.checking){
      fetch('/api/logs?n=60').then(r=>r.json()).then(d=>logOut(d.lines.join('\\n'))).catch(()=>{});
    }
  }).catch(()=>{st({online:null});logOut('无法连接后端');});
}
function checkNow(){
  var b=document.getElementById('btn');
  if(b.disabled)return;
  fetch('/api/check',{method:'POST'}).then(r=>r.json()).then(s=>{
    st(s); setTimeout(refresh,1500);
  }).catch(()=>{});
}
/* ---- 手动认证: 强制登录一次, 不经过探测/断网窗口判断 ---- */
function manualLogin(){
  var mb=document.getElementById('manualBtn');
  if(mb.disabled)return;
  var s=_lastState||{};
  if(s.manual){msg('手动认证正在进行，请稍候','');return;}
  if(s.checking){msg('正在自动检测/认证中，请等这一轮结束再试','err');return;}
  var warn='';
  if(s.window){
    warn='当前处于断网窗口('+winLabel(s)+')，校园网已断开，认证大概率失败。\\n仍要执行一次手动认证吗？';
  }else if(s.online===true){
    warn='当前显示在线。手动认证会强制重新登录，若 portal 侧会话半开可能返回\\n'
        +'"workFlowNode is empty" 错误。确定继续？';
  }
  if(warn && !confirm(warn))return;
  mb.disabled=true;mb.textContent='认证中…';
  msg('正在手动认证，请稍候（最长约 2 分钟）…','');
  fetch('/api/login',{method:'POST'}).then(r=>r.json().then(res=>({code:r.status,res:res})))
    .then(o=>{
      var res=o.res||{};
      if(o.code!==200 || res.ok===false){
        msg('无法发起手动认证: '+(res.error||('HTTP '+o.code)),'err');
        st(_lastState); refresh(); return;
      }
      st(res);
      waitManual(0);
    }).catch(()=>{
      msg('网络错误: 无法连接面板后端','err');
      st(_lastState); refresh();
    });
}
function waitManual(n){
  fetch('/api/status').then(r=>r.json()).then(s=>{
    st(s);
    if(s.manual){
      if(n<100)setTimeout(function(){waitManual(n+1);},1500);
      else msg('手动认证长时间没有结束，请查看运行日志','err');
      return;
    }
    if(s.manual_ok===true)msg('手动认证成功：'+(s.manual_msg||'网络已恢复'),'ok');
    else if(s.manual_ok===false)msg('手动认证失败：'+(s.manual_msg||'请查看运行日志'),'err');
    else msg(s.manual_msg||'手动认证已结束','');
    refresh();
  }).catch(()=>{
    if(n<20)setTimeout(function(){waitManual(n+1);},2000);
    else msg('无法获取认证结果，请刷新页面','err');
  });
}
setInterval(refresh,5000);
loadCfg();
refresh();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self):
        body = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self._html()
        if path == "/api/status":
            with _lock:
                return self._json(dict(_state))
        if path == "/api/settings":
            return self._json(get_public_cfg())
        if path == "/api/logs":
            q = parse_qs(urlparse(self.path).query)
            n = int(q.get("n", ["100"])[0])
            with _lock:
                lines = LOG_LINES[-n:]
            return self._json({"lines": lines})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            data = {}

        if path == "/api/check":
            threading.Thread(target=check_once, daemon=True).start()
            with _lock:
                return self._json(dict(_state))
        if path == "/api/login":
            # 手动认证: 跳过探测, 直接强制登录一次
            err = manual_precheck()
            if err:
                log(f"手动认证: 请求被拒绝 ({err})")
                return self._json({"ok": False, "error": err}, 400)
            threading.Thread(target=manual_login, daemon=True).start()
            with _lock:
                st = dict(_state)
            # 任务已启动, 这里直接给出"进行中"状态, 避免前端读到启动前的瞬间
            st["manual"] = True
            st["checking"] = True
            st["manual_ok"] = None
            st["manual_msg"] = "正在认证…"
            return self._json({"ok": True, **st})
        if path == "/api/settings":
            username = str(data.get("username", "")).strip()
            service = str(data.get("service", "")).strip()
            password = data.get("password", "") or ""
            if not username:
                return self._json({"ok": False, "error": "账号不能为空"}, 400)
            if service and service not in SERVICES:
                return self._json({"ok": False, "error": "无效的运营商"}, 400)
            if not service:
                service = _cfg.get("service", "中国联通")
            # 断网窗口: 允许只改其中一项; 给了值就必须是合法时间
            win_start = win_end = None
            for key, label in (("window_start", "断网开始时间"), ("window_end", "断网结束时间")):
                if key in data:
                    v = parse_hhmm(data.get(key))
                    if not v:
                        return self._json(
                            {"ok": False,
                             "error": f"{label}格式不对，请填 HH:MM（如 23:30）"}, 400)
                    if key == "window_start":
                        win_start = v
                    else:
                        win_end = v
            old_win = window_str()
            if save_config(username, password, service, win_start, win_end):
                log(f"配置已更新: 账号={username} 运营商={service}")
                if window_str() != old_win:
                    pub = get_public_cfg()
                    note = "（起止相同，不再跳过）" if pub["window_start"] == pub["window_end"] else ""
                    log(f"断网窗口已更新: {old_win} -> {window_str()}{note}")
                return self._json({"ok": True, **get_public_cfg()})
            return self._json({"ok": False, "error": "写入失败"}, 500)
        if path == "/api/clearlog":
            clear_log()
            return self._json({"ok": True})
        if path == "/api/pause":
            paused = bool(data.get("paused"))
            if set_paused(paused):
                return self._json({"ok": True, **get_public_cfg()})
            return self._json({"ok": False, "error": "写入失败"}, 500)
        self._json({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    load_config()
    threading.Thread(target=keeper_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    log(f"Web 面板已启动: http://0.0.0.0:{HTTP_PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
