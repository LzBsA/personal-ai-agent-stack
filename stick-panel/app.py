import os
import time
import threading

import paramiko
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

STICK_HOST = os.environ.get("STICK_HOST", "192.168.68.1")
STICK_USER = os.environ.get("STICK_USER", "root")
STICK_PASS = os.environ.get("STICK_PASS", "")
SSH_TIMEOUT = 6
CACHE_TTL = 5

_cache = {"ts": 0.0, "data": None}
_lock = threading.Lock()


def ssh_run(cmd, timeout=15):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            STICK_HOST,
            22,
            STICK_USER,
            STICK_PASS,
            timeout=SSH_TIMEOUT,
            allow_agent=False,
            look_for_keys=False,
        )
        _, out, err = client.exec_command(cmd, timeout=timeout)
        return out.read().decode("utf-8", "replace"), err.read().decode("utf-8", "replace")
    finally:
        try:
            client.close()
        except Exception:
            pass


COLLECT = r"""
echo "OS=$(grep -m1 PRETTY_NAME /etc/os-release | cut -d= -f2- | tr -d '\"')"
echo "KERNEL=$(uname -r)"
echo "UPTIME_SEC=$(cut -d. -f1 /proc/uptime)"
echo "MEM_TOTAL_KB=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)"
echo "MEM_AVAIL_KB=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)"
echo "SWAP_TOTAL_KB=$(awk '/^SwapTotal:/{print $2}' /proc/meminfo)"
echo "SWAP_FREE_KB=$(awk '/^SwapFree:/{print $2}' /proc/meminfo)"
echo "DISK_USED_MB=$(df -m / | awk 'NR==2{print $3}')"
echo "DISK_TOTAL_MB=$(df -m / | awk 'NR==2{print $2}')"
echo "TEMP_MC=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo 0)"
echo "WLAN_RAW=$(nmcli -t -f DEVICE,STATE,CONNECTION dev 2>/dev/null | grep '^wlan0:')"
echo "WLAN_IP=$(ip -4 -o addr show wlan0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)"
echo "SIGNAL=$(nmcli -t -f IN-USE,SIGNAL dev wifi 2>/dev/null | awk -F: '$1=="*"{print $2; exit}')"
echo "USBNET_IP=$(ip -4 -o addr show usb0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)"
echo "MODEM=$(mmcli -L 2>/dev/null | grep -o '/Modem/[0-9]*' | head -1)"
echo "AP_AUTOCONN=$(nmcli -t -f NAME,AUTOCONNECT con show 2>/dev/null | grep '^WIFI:' | cut -d: -f2)"
echo "SAVED=$(nmcli -t -f NAME,TYPE con show 2>/dev/null | grep ':802-11-wireless$' | cut -d: -f1 | paste -sd'|' -)"
echo "JOURNAL=$(journalctl --disk-usage 2>/dev/null | grep -oE '[0-9.]+[A-Z]' | head -1)"
echo "JOURNAL_RETENTION=$(grep -h '^MaxRetentionSec' /etc/systemd/journald.conf.d/*.conf /etc/systemd/journald.conf 2>/dev/null | head -1 | cut -d= -f2)"
if nmcli -t -f DEVICE,STATE dev 2>/dev/null | grep -q '^wlan0:connected'; then
  echo "WAN_IP=$(curl -s -m 3 -o /dev/null -w '%{http_code}' http://223.5.5.5/ 2>/dev/null)"
  echo "WAN_DNS=$(curl -s -m 4 -o /dev/null -w '%{http_code}' https://www.baidu.com/ 2>/dev/null)"
else
  echo "WAN_IP=SKIP"
  echo "WAN_DNS=SKIP"
fi
"""


def _num(v, default=0):
    try:
        return int(float(v))
    except Exception:
        return default


def collect():
    raw, err = ssh_run(COLLECT, timeout=28)
    if not raw.strip():
        raise RuntimeError(err.strip() or "no output from stick")

    d = {}
    for line in raw.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()

    wlan_raw = d.get("WLAN_RAW", "")
    parts = wlan_raw.split(":", 2)
    state = parts[1] if len(parts) > 1 else "unknown"
    conn = parts[2] if len(parts) > 2 else ""

    total = _num(d.get("MEM_TOTAL_KB"))
    avail = _num(d.get("MEM_AVAIL_KB"))
    swap_total = _num(d.get("SWAP_TOTAL_KB"))
    swap_free = _num(d.get("SWAP_FREE_KB"))

    # 中继 WiFi 是否真能出网（棒子自己发起的测试，与飞牛走哪条线无关）
    wan_ip = d.get("WAN_IP", "SKIP")
    wan_dns = d.get("WAN_DNS", "SKIP")
    # 223.5.5.5 的根路径返回 404 是正常的，只要不是 000 就说明 TCP/HTTP 通
    ip_reachable = wan_ip not in ("000", "SKIP", "")
    dns_ok = wan_dns[:1] in ("2", "3")
    if wan_ip == "SKIP":
        wan_status = "skip"
    elif dns_ok:
        wan_status = "ok"          # 域名能通 = 真实可用
    elif ip_reachable:
        wan_status = "dns_fail"    # 只有 IP 通，DNS 有问题
    else:
        wan_status = "down"

    return {
        "os": d.get("OS", ""),
        "kernel": d.get("KERNEL", ""),
        "uptime": _num(d.get("UPTIME_SEC")),
        "mem_total_mb": round(total / 1024),
        "mem_used_mb": round((total - avail) / 1024),
        "swap_total_mb": round(swap_total / 1024),
        "swap_used_mb": round((swap_total - swap_free) / 1024),
        "disk_used_mb": _num(d.get("DISK_USED_MB")),
        "disk_total_mb": _num(d.get("DISK_TOTAL_MB")),
        "temp_c": round(_num(d.get("TEMP_MC")) / 1000, 1),
        "wlan_state": state,
        "wlan_conn": conn,
        "wlan_ip": d.get("WLAN_IP", ""),
        "signal": _num(d.get("SIGNAL")),
        "usb_ip": d.get("USBNET_IP", ""),
        "modem": d.get("MODEM", ""),
        "ap_autoconnect": d.get("AP_AUTOCONN", ""),
        "saved": [s for s in d.get("SAVED", "").split("|") if s],
        "journal": d.get("JOURNAL", ""),
        "journal_retention": d.get("JOURNAL_RETENTION", ""),
        "wan_status": wan_status,
        "wan_ip_code": wan_ip,
        "wan_dns_code": wan_dns,
        "online": True,
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    with _lock:
        now = time.time()
        if _cache["data"] and now - _cache["ts"] < CACHE_TTL:
            return jsonify(_cache["data"])

    try:
        data = collect()
    except Exception as exc:
        data = {"online": False, "error": str(exc)}

    with _lock:
        _cache["ts"] = time.time()
        _cache["data"] = data
    return jsonify(data)


@app.route("/api/scan")
def api_scan():
    cmd = "nmcli -t -f SSID,SIGNAL,SECURITY dev wifi list --rescan yes 2>/dev/null"
    try:
        raw, err = ssh_run(cmd, timeout=25)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "networks": []})

    seen = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.rsplit(":", 2)
        if len(parts) != 3:
            continue
        ssid, sig, sec = parts
        ssid = ssid.strip()
        if not ssid:
            continue
        try:
            sig_i = int(sig)
        except ValueError:
            continue
        if ssid in seen and seen[ssid]["signal"] >= sig_i:
            continue
        seen[ssid] = {"ssid": ssid, "signal": sig_i, "security": sec or "open"}

    networks = sorted(seen.values(), key=lambda x: -x["signal"])
    return jsonify({"ok": True, "networks": networks})


@app.route("/api/connect", methods=["POST"])
def api_connect():
    body = request.get_json(silent=True) or {}
    ssid = (body.get("ssid") or "").strip()
    password = body.get("password") or ""
    if not ssid:
        return jsonify({"ok": False, "error": "缺少 SSID"})

    safe_ssid = ssid.replace("'", "'\\''")
    saved_cmd = "nmcli -t -f NAME,TYPE con show 2>/dev/null | grep ':802-11-wireless$' | cut -d: -f1"
    try:
        saved_raw, _ = ssh_run(saved_cmd, timeout=15)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})

    saved = [s.strip() for s in saved_raw.splitlines() if s.strip()]

    if ssid in saved and not password:
        cmd = "nmcli connection up '%s' 2>&1" % safe_ssid
    elif password:
        safe_pwd = password.replace("'", "'\\''")
        cmd = "nmcli dev wifi connect '%s' password '%s' 2>&1" % (safe_ssid, safe_pwd)
    else:
        return jsonify({"ok": False, "error": "该网络未保存，需要提供密码"})

    try:
        out, err = ssh_run(cmd, timeout=45)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})

    text = (out + err).strip()
    ok = ("successfully activated" in text) or ("已成功激活" in text)
    return jsonify({"ok": ok, "output": text[:600]})


@app.route("/api/hotspot", methods=["POST"])
def api_hotspot():
    body = request.get_json(silent=True) or {}
    enable = bool(body.get("enable"))
    cmd = "nmcli connection up WIFI 2>&1" if enable else "nmcli connection down WIFI 2>&1"
    try:
        out, err = ssh_run(cmd, timeout=40)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})
    text = (out + err).strip()
    return jsonify({"ok": True, "output": text[:400]})


@app.route("/api/stick-reconnect", methods=["POST"])
def api_stick_reconnect():
    """单次自动重连：扫描一次 -> 连信号最强的已保存 WiFi（最多 3 次尝试）-> 验证出网。
    成功后向宿主机发重启 mihomo 的请求，确保 DSH 出站重建。"""
    cmd = "bash /usr/local/bin/reconnect-wifi.sh 2>&1"
    try:
        out, err = ssh_run(cmd, timeout=230)
    except Exception as exc:
        return jsonify({"ok": False, "result": "", "error": str(exc)})

    text = (out + err).strip()
    ok = text.startswith("OK:") or text.startswith("ALREADY")

    requested = False
    if ok:
        try:
            with open("/run/mihomo-restart.request", "w") as fh:
                fh.write(str(int(time.time())))
            requested = True
        except Exception:
            requested = False

    if text.startswith("OK:"):
        msg = "重连成功：" + text[3:].replace(":", " · ")
        msg += "；已请求重启 mihomo（约 1 分钟内生效）" if requested else "；请手动重启 mihomo"
    elif text.startswith("ALREADY"):
        msg = "当前已连接，无需重连"
    elif text.startswith("NO_SAVED_PROFILE"):
        msg = "棒子上没有已保存的 WiFi，无法自动重连"
    elif text.startswith("NO_SAVED_IN_RANGE"):
        msg = "附近扫描不到已保存的 WiFi，已停止（不会反复重试）"
    elif text.startswith("FAILED"):
        msg = "3 次连接都失败：" + text
    else:
        msg = text or "无返回"

    with _lock:
        _cache["data"] = None
        _cache["ts"] = 0

    return jsonify({"ok": ok, "result": text[:600], "message": msg,
                    "mihomo_restart_requested": requested})


@app.route("/api/logs")
def api_logs():
    cmd = "journalctl -u NetworkManager --no-pager -n 25 2>/dev/null | tail -25"
    try:
        raw, _ = ssh_run(cmd, timeout=15)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "lines": []})
    return jsonify({"ok": True, "lines": [l for l in raw.splitlines() if l.strip()]})


@app.route("/api/cleanlogs", methods=["POST"])
def api_cleanlogs():
    body = request.get_json(silent=True) or {}
    days = body.get("days", 2)
    try:
        days = max(1, min(30, int(days)))
    except Exception:
        days = 2

    before_out, _ = ssh_run("journalctl --disk-usage 2>/dev/null | grep -oE '[0-9.]+[A-Z]' | head -1", timeout=15)
    cmd = "journalctl --vacuum-time=%dd 2>&1 | tail -3" % days
    try:
        out, err = ssh_run(cmd, timeout=60)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})

    after_out, _ = ssh_run("journalctl --disk-usage 2>/dev/null | grep -oE '[0-9.]+[A-Z]' | head -1", timeout=15)
    with _lock:
        _cache["data"] = None
        _cache["ts"] = 0

    text = (out + err).strip()
    return jsonify({
        "ok": True,
        "before": before_out.strip(),
        "after": after_out.strip(),
        "days": days,
        "output": text[:500],
    })


@app.route("/api/reboot", methods=["POST"])
def api_reboot():
    try:
        ssh_run("(sleep 1; reboot) >/dev/null 2>&1 &", timeout=8)
    except Exception:
        pass
    with _lock:
        _cache["data"] = None
        _cache["ts"] = 0
    return jsonify({"ok": True, "message": "重启指令已下发，约 40 秒后恢复"})


if __name__ == "__main__":
    bind_host = os.environ.get("BIND_HOST", "0.0.0.0")
    app.run(host=bind_host, port=8090, threaded=True)
