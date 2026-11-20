# dsh-loopback-proxy

一个 165 行的回环身份代理，让局域网里的设备能够访问 dsh-mobile 的管理接口。

## 问题

dsh-mobile 的管理 API（生成配对密钥、设备管理、通道开关）在源码里有一道硬校验：

```js
function assertLocalAdminTrust(request, ...) {
  if (!isLoopbackAddress(request.socket.remoteAddress)) {
    throw new HttpError(403, "forbidden");
  }
  ...
}
```

它只接受来自 `127.0.0.0/8` 和 `::1` 的请求。作者的设计假设是"你在本机开浏览器，浏览器和 dsh 在同一台机器上"。

但如果 dsh 跑在 NAS 或服务器上，你在另一台电脑上开浏览器，源地址就是你的局域网 IP，必然 403。`--trusted-host` 参数对这条校验无效——它根本不看。

我试过的三条路：

| 路径 | 结果 |
|---|---|
| 直连 `:28000` | `403 forbidden`，源地址不是回环 |
| 走网关（如飞牛 5667） | `404`，nginx 只配了 `/app/`，`/api/` 落到登录页 |
| 改 `--trusted-host` | 无效，校验逻辑不看它 |

## 方案

既然它只信任回环，那就让请求看起来来自回环：在 NAS 上开一个新端口，收到请求后**从 127.0.0.1 重新建立一条连接**到后端。后端看到的 `remoteAddress` 就是 `127.0.0.1`，校验通过。

```
浏览器(192.168.1.217)
  → NAS:28080（本代理）
  → 127.0.0.1:28000（重新建连，源地址是回环）
  → 管理 API 放行
```

关键细节：**保留原始 `Host` 头**。代理只重新建连，不改写头部——如果把 `Host` 改成 `127.0.0.1`，后端的 Origin 同源校验会失败（`Origin.host` 必须等于 `Host`）。

## 三条分支

代理必须同时处理三种流量，漏掉任何一种都会以很怪的方式失败：

| 流量 | 处理 | 漏掉的后果 |
|---|---|---|
| 普通 HTTP | 读满 `Content-Length` 的 body → 转发 → 强制 `Connection: close` | 不读 body 会丢 POST 数据；不关连接会让代理空等 60 秒 |
| WebSocket 升级 | 转发 `Upgrade` 头 → 等后端 `101` → 双向 `select` 泵 | 单向转发会导致界面一直显示"重新连接中" |
| 大文件（若干 MB 的前端 bundle） | 分块 `recv` / `sendall` | 单次 `read` 读不完会截断 |

代码里 `pump()` 那段是双向字节泵，`head_ok()` 必须接受 `HTTP/` 开头的响应行（含 `101`），这是最容易写错的两处。

## 部署

```bash
install -m 755 loopback-proxy.py /vol1/@appshare/dsh/dsh_home/loopback-proxy.py

# 编辑 service 里的路径和 User
install -m 644 dsh-loopback-proxy.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dsh-loopback-proxy
```

服务定义：

```ini
[Service]
Type=simple
User=dsh
Environment=LBP_PORT=28080
Environment=LBP_BACKEND_PORT=28000
ExecStart=/usr/bin/python3 /path/to/loopback-proxy.py
Restart=always
RestartSec=3
```

`LBP_PORT` 是对外监听端口，`LBP_BACKEND_PORT` 是后端 dsh 的端口。

## 验证

```bash
# 直连后端（应该 403）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:28000/api/mobile-access/lan/control

# 经代理（应该 200，且耗时 < 10ms）
curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' http://192.168.1.100:28080/api/mobile-access/lan/control

# 大文件（应该 200 且字节数完整）
curl -s -o /dev/null -w '%{http_code} %{size_download}\n' http://192.168.1.100:28080/plugins/dsh-mobile/client.js
```

## 别忘了这只是权宜之计

这个代理依赖 dsh-mobile 的内部行为，插件升级后可能失效。正解是给上游加一个 CIDR 配置项（复用 `lib/index.mjs` 里已有的 `allowedCidrs` 实现，只放宽地址校验，保留 Host / Origin / CSRF 三道检查），然后把代理撤掉。

我选择用代理，是因为它**不修改任何上游文件**，升级不受影响——而之前试过的另一种做法（改 `client.js` 让它感知网关路径前缀）每次升级都要重打一遍，属于负债。

## 许可

MIT。
