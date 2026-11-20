# -*- coding: utf-8 -*-
"""本地回归测试: 面板后端行为 (不触碰真实校园网, run_login 已被替换)

覆盖: 手动认证状态机 / 断网窗口手动填写 / 认证错误提示翻译 / 恢复记录 / 原有接口回归
用法:  python test_panel.py       # 全 PASS 且退出码 0 才算通过
"""
import datetime
import json
import os
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APPDIR = os.path.join(HERE, "ysunet-app")
TESTDATA = os.path.join(HERE, "_test_data")

os.environ["DATA_DIR"] = TESTDATA
os.environ["HTTP_PORT"] = "8099"
os.environ["CHECK_INTERVAL"] = "3600"
os.environ["RUIJIE_USERNAME"] = "demo-user"
os.environ["RUIJIE_PASSWORD"] = "dummy-pass"
os.environ["RUIJIE_SERVICE"] = "中国联通"
os.makedirs(TESTDATA, exist_ok=True)
for _f in ("config.json", "ysunet.log"):          # 每次从干净状态开始
    try:
        os.remove(os.path.join(TESTDATA, _f))
    except OSError:
        pass

sys.path.insert(0, APPDIR)
import app  # noqa: E402

BASE = "http://127.0.0.1:8099"
FAILS = []


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode())


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("  " + str(extra) if extra else ""))
    if not cond:
        FAILS.append(name)


# ---- 替换真实认证程序, 避免真的去认证校园网 ----
FAKE = {"mode": "ok", "delay": 2, "tail": None}


def fake_run_login():
    time.sleep(FAKE["delay"])
    if FAKE["mode"] == "ok":
        return True, "Login successful to service: 中国联通"
    return False, FAKE["tail"] or ("Invalid service login response: {'code': 400, 'message': "
                                   "'登录失败，失败原因:workFlowNode is empty'}")


app.run_login = fake_run_login
app.probe_ok = lambda: FAKE["mode"] == "ok"

srv = app.ThreadingHTTPServer(("127.0.0.1", 8099), app.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)

print("== 1. 页面元素 ==")
html = urllib.request.urlopen(BASE + "/", timeout=10).read().decode()
check("手动认证按钮 manualBtn", 'id="manualBtn"' in html and ">手动认证<" in html)
check("按钮挂 onclick", 'onclick="manualLogin()"' in html)
check("青色样式 .btn.teal", ".btn.teal{" in html)
check("手动认证状态行 manualState", 'id="manualState"' in html)
check("断网时间输入框 setWinStart", 'id="setWinStart"' in html and 'type="time"' in html)
check("断网时间输入框 setWinEnd", 'id="setWinEnd"' in html)
check("断网时间说明 winState", 'id="winState"' in html)
check("页脚断网窗口占位 footWin", 'id="footWin"' in html)
check("JS 窗口文案函数 winLabel", "function winLabel(" in html)
check("JS 函数 manualLogin", "function manualLogin()" in html)
check("页脚含手动认证说明", "手动认证绕过探测与断网窗口判断" in html)
check("状态文案里已无硬编码窗口", "断网窗口(23:30-05:30)" not in html)

print("== 2. 手动认证 成功路径 ==")
FAKE.update(mode="ok", delay=2)
code, res = post("/api/login")
check("POST /api/login -> 200", code == 200, res)
check("返回 ok:true + 立即进入 manual 态", res.get("ok") is True and res.get("manual") is True, res)
st = get("/api/status")
check("运行中 manual=true checking=true", st.get("manual") is True and st.get("checking") is True)
code2, res2 = post("/api/login")
check("并发再点 -> 400 明确提示", code2 == 400 and "正在" in res2.get("error", ""), res2)
deadline = time.time() + 40
while time.time() < deadline:
    st = get("/api/status")
    if not st.get("manual"):
        break
    time.sleep(0.5)
check("结束后 manual=false", st.get("manual") is False, st)
check("结果 manual_ok=true", st.get("manual_ok") is True, st)
check("提示 网络已恢复", st.get("manual_msg") == "网络已恢复", st)
check("online=true", st.get("online") is True)
check("写入了 last_ok / last_change", bool(st.get("last_ok")) and bool(st.get("last_change")))
logs = get("/api/logs?n=50")["lines"]
check("日志含 手动认证: 开始", any("手动认证: 开始" in l for l in logs))
check("日志含 手动认证成功", any("手动认证成功" in l for l in logs))

print("== 3. 手动认证 失败路径 (workFlowNode is empty) ==")
FAKE.update(mode="fail", delay=1)
code, res = post("/api/login")
check("POST /api/login -> 200", code == 200, res)
deadline = time.time() + 40
while time.time() < deadline:
    st = get("/api/status")
    if not st.get("manual"):
        break
    time.sleep(0.5)
check("结果 manual_ok=false", st.get("manual_ok") is False, st)
check("友好提示含 workFlowNode", "workFlowNode" in (st.get("manual_msg") or ""), st.get("manual_msg"))
check("last_fail 已更新", bool(st.get("last_fail")))
logs = get("/api/logs?n=50")["lines"]
check("日志含 手动认证失败", any("手动认证失败" in l for l in logs))

print("== 4. 缺账号/密码的异常提示 ==")
app._cfg["password"] = ""
code, res = post("/api/login")
check("缺密码 -> 400", code == 400, res)
check("提示指向认证配置", "密码" in res.get("error", "") and "认证配置" in res.get("error", ""), res.get("error"))
app._cfg["password"] = "dummy-pass"
app._cfg["username"] = ""
code, res = post("/api/login")
check("缺账号 -> 400", code == 400, res)
app._cfg["username"] = "demo-user"

print("== 5. 断网时间(认证时间)手动填写 ==")
c0 = get("/api/settings")
check("默认 window_start=23:30", c0.get("window_start") == "23:30", c0)
check("默认 window_end=05:30", c0.get("window_end") == "05:30", c0)
st = get("/api/status")
check("status 里也带 window_start/end",
      st.get("window_start") == "23:30" and st.get("window_end") == "05:30", st)

code, res = post("/api/settings", {"username": "demo-user", "service": "中国联通",
                                   "window_start": "01:00", "window_end": "06:00"})
check("保存新窗口 -> 200", code == 200 and res.get("ok") is True, res)
check("响应回带新窗口", res.get("window_start") == "01:00" and res.get("window_end") == "06:00", res)
st = get("/api/status")
check("立即生效: status 已更新", st.get("window_start") == "01:00" and st.get("window_end") == "06:00", st)
with open(os.path.join(TESTDATA, "config.json"), encoding="utf-8") as f:
    saved = json.load(f)
check("已持久化到 config.json",
      saved.get("window_start") == "01:00" and saved.get("window_end") == "06:00", saved)
check("持久化未破坏账号/密码/运营商",
      saved.get("username") == "demo-user" and saved.get("password") == "dummy-pass"
      and saved.get("service") == "中国联通", saved)
check("内存日志记录了窗口变更",
      any("断网窗口已更新" in l for l in get("/api/logs?n=30")["lines"]))

code, res = post("/api/settings", {"username": "demo-user", "service": "中国联通",
                                   "window_start": "3:5", "window_end": "06:00"})
check("宽松格式 3:5 -> 03:05", code == 200 and res.get("window_start") == "03:05", res)
code, res = post("/api/settings", {"username": "demo-user", "service": "中国联通",
                                   "window_start": "2330", "window_end": "0530"})
check("紧凑格式 2330/0530 -> 23:30/05:30",
      code == 200 and res.get("window_start") == "23:30" and res.get("window_end") == "05:30", res)
for bad in ("25:00", "23:70", "abc", "23:3:0", ""):
    code, res = post("/api/settings", {"username": "demo-user", "service": "中国联通",
                                       "window_start": bad, "window_end": "05:30"})
    check("非法时间 %r -> 400 + 提示" % bad,
          code == 400 and "HH:MM" in res.get("error", ""), res)
app._cfg["window_start"], app._cfg["window_end"] = "23:30", "05:30"   # 还原默认

print("== 6. 断网窗口判定逻辑 (in_drop_window) ==")


def at(hh, mm):
    return datetime.datetime(2026, 9, 21, hh, mm, tzinfo=app.CST)


app._cfg["window_start"], app._cfg["window_end"] = "23:30", "05:30"
check("默认窗口 23:29 -> 不跳过", app.in_drop_window(at(23, 29)) is False)
check("默认窗口 23:30 -> 跳过", app.in_drop_window(at(23, 30)) is True)
check("默认窗口 00:00 -> 跳过", app.in_drop_window(at(0, 0)) is True)
check("默认窗口 05:29 -> 跳过", app.in_drop_window(at(5, 29)) is True)
check("默认窗口 05:30 -> 不跳过", app.in_drop_window(at(5, 30)) is False)
check("默认窗口 12:00 -> 不跳过", app.in_drop_window(at(12, 0)) is False)

app._cfg["window_start"], app._cfg["window_end"] = "01:00", "06:00"
check("同日窗口 00:59 -> 不跳过", app.in_drop_window(at(0, 59)) is False)
check("同日窗口 01:00 -> 跳过", app.in_drop_window(at(1, 0)) is True)
check("同日窗口 05:59 -> 跳过", app.in_drop_window(at(5, 59)) is True)
check("同日窗口 06:00 -> 不跳过", app.in_drop_window(at(6, 0)) is False)
check("同日窗口 23:00 -> 不跳过", app.in_drop_window(at(23, 0)) is False)

app._cfg["window_start"], app._cfg["window_end"] = "23:30", "23:30"
check("起止相同 -> 永不跳过",
      app.in_drop_window(at(23, 45)) is False and app.in_drop_window(at(3, 0)) is False)
app._cfg["window_start"], app._cfg["window_end"] = "23:30", "05:30"

check("parse_hhmm 宽进严出",
      app.parse_hhmm("5:30") == "05:30" and app.parse_hhmm("0530") == "05:30"
      and app.parse_hhmm(" 23:30 ") == "23:30" and app.parse_hhmm("23：30") == "23:30"
      and app.parse_hhmm("24:00") is None and app.parse_hhmm("") is None
      and app.parse_hhmm(None) is None)

print("== 7. 认证错误提示翻译 ==")
t = app.friendly_login_error(
    "Error: Authentication failed. Detail: authentication failed: portal认证失败，"
    "失败原因：不在认证时段内，用户(demo-user)，终端ip(10.0.0.9)，终端mac(aa:bb:cc:dd:ee:ff)")
check("不在认证时段内 -> 说人话", "断网时段" in t and "23:30-05:30" in t and "Error:" not in t, t)
t = app.friendly_login_error("portal认证失败，失败原因：账号或密码错误，xxx")
check("未知原因 -> 只留失败原因", t == "portal 拒绝认证：账号或密码错误", t)
check("workFlowNode 仍命中", "workFlowNode" in app.friendly_login_error("...workFlowNode is empty..."))
check("Network connection failed 仍命中",
      "不可达" in app.friendly_login_error("Error: Network connection failed"))
check("timeout 仍命中", "超时" in app.friendly_login_error("login timed out"))
check("exec error 仍命中", "无法执行" in app.friendly_login_error("exec error: [Errno 2]"))
check("空输出有兜底", app.friendly_login_error("") == "认证程序没有返回任何信息")
app._cfg["window_start"], app._cfg["window_end"] = "23:00", "06:00"
check("提示里的时段跟随配置", "23:00-06:00" in app.friendly_login_error("失败原因：不在认证时段内，x"))
app._cfg["window_start"], app._cfg["window_end"] = "23:30", "05:30"

print("== 8. 离线->在线 也要记一次恢复 ==")
FAKE.update(mode="ok", delay=0)
app.set_state(last_ok=None, last_change=None)
app._state["online"] = False
app.check_once()
st = get("/api/status")
check("恢复后 online=true", st.get("online") is True, st)
check("恢复后 last_ok 有值", bool(st.get("last_ok")), st)
check("恢复后 last_change 有值", bool(st.get("last_change")), st)
logs = get("/api/logs?n=20")["lines"]
check("日志写明未触发认证", any("网络已恢复（本轮探测确认，未触发认证）" in l for l in logs), logs[-2:])
prev_ok = st.get("last_ok")
app._state["online"] = True
app.check_once()
st = get("/api/status")
check("一直在线 -> 仍是「外网正常，无需认证」",
      any("外网正常，无需认证" in l for l in get("/api/logs?n=10")["lines"]), st)
check("一直在线 -> 不重复刷新 last_ok", st.get("last_ok") == prev_ok, st)

print("== 9. 原有功能回归 ==")
FAKE.update(mode="ok", delay=0)
code, res = post("/api/check")
check("POST /api/check -> 200", code == 200, res)
time.sleep(2)
st = get("/api/status")
check("/api/check 后 checking 归位", st.get("checking") is False, st)
code, res = post("/api/pause", {"paused": True})
check("/api/pause 仍可用", res.get("ok") is True, res)
st = get("/api/status")
check("暂停态 manual 为 false", st.get("paused") is True and st.get("manual") is False)
code, res = post("/api/pause", {"paused": False})
check("恢复自动认证", res.get("ok") is True and res.get("paused") is False, res)
code, res = post("/api/settings", {"username": "demo-user", "service": "中国联通"})
check("/api/settings 不带窗口字段也能保存", res.get("ok") is True, res)
check("窗口保持不变", res.get("window_start") == "23:30" and res.get("window_end") == "05:30", res)
check("/api/settings 不回传密码", "password" not in res, list(res.keys()))
check("/api/logs 正常", "lines" in get("/api/logs?n=5"))
code, res = post("/api/settings", {"username": "", "service": "中国联通"})
check("空账号仍被拦", code == 400, res)
code, res = post("/api/settings", {"username": "x", "service": "火星运营商"})
check("非法运营商仍被拦", code == 400, res)

print("== 10. HTML 结构完整性 ==")
check("</html> 结尾", html.rstrip().endswith("</html>"))
check("script 标签成对", html.count("<script>") == 1 and html.count("</script>") == 1)
check("按钮顺序 立即检测 < 手动认证 < 暂停",
      html.index(">立即检测<") < html.index(">手动认证<") < html.index(">暂停自动认证<"))

print()
print("失败项: %s" % (FAILS if FAILS else "无"))
srv.shutdown()
sys.exit(1 if FAILS else 0)
