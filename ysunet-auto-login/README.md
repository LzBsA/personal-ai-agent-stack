# ysunet-auto-login

校园网自动认证守护（锐捷 V2 / Portal），带 Web 控制面板。用 Docker 部署在一台常开的机器上，每 5 分钟探测一次外网，掉线就自动重新登录。

> 实测环境：某高校校园网（锐捷 V2 认证，每晚 23:30 强制断网、次日 05:30 恢复）。
> 认证入口域名（`auth1.xxx.edu.cn`）写死在 `ysu_net_login/ruijie_client.py` 里，用于其他学校需要自行替换；"探测 + 重试 + 面板"这套壳与学校无关，可以直接复用。

## 它解决什么问题

校园网每晚断网，早上恢复后**每台设备都要手动重新认证一次**——那时候通常没人醒着。这个程序做的就是替你在 05:30 之后自动完成那次登录，并提供一个面板看状态、改配置、翻日志。

## 功能

- **自动保活**：每 `CHECK_INTERVAL` 秒用 HTTPS 探测外网，不通就调认证程序登录
- **断网窗口**：23:30–05:30 期间跳过检测（学校确实断了，认证也没用），时段可在面板改
- **Web 面板**：状态、四格时间信息、账号配置、运行日志、手动检测、手动认证、暂停自动认证
- **并发保护**：同一时刻只允许一个探测/认证任务，避免向 Portal 发重复登录
- **持久化**：账号、密码、断网时段存在 `data/config.json`，重启不丢
- **日志自清理**：超过 48 小时自动清空

## 快速开始

```bash
cp .env.example .env
vi .env                       # 填学号、密码、运营商

docker compose build
docker compose up -d
```

打开 `http://<你的IP>:8080`。

首次构建建议联网（需要装依赖）；离线环境可参考 `Dockerfile` 里的 `--no-index --find-links` 写法，把 wheel 预先放进 `wheels/`。

## 配置

环境变量（`.env`）：

| 变量 | 说明 | 默认 |
|---|---|---|
| `RUIJIE_USERNAME` | 校园网账号 | — |
| `RUIJIE_PASSWORD` | 密码 | — |
| `RUIJIE_SERVICE` | 运营商：中国联通 / 中国移动 / 中国电信 | 中国联通 |
| `CHECK_INTERVAL` | 探测间隔（秒） | 300 |
| `WINDOW_START` / `WINDOW_END` | 断网时段 `HH:MM` | 23:30 / 05:30 |
| `LOGIN_BIN` | 认证程序路径，测试时可指向 `/bin/true` | `ysunetlogin` |

面板上保存的配置写入 `data/config.json`，优先级高于环境变量。

## HTTP 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/` | 控制面板 |
| GET | `/api/status` | 当前状态、上次检测/成功/失败 |
| GET/POST | `/api/settings` | 读/写账号、密码、断网时段 |
| GET | `/api/logs?n=60` | 最近日志 |
| POST | `/api/check` | 手动触发一次探测+认证 |
| POST | `/api/login` | **跳过探测，强制认证一次** |
| POST | `/api/pause` | 暂停/恢复自动认证 |
| POST | `/api/clearlog` | 清空日志 |

## 几个踩过的坑

**用 HTTPS 探测，不要用 HTTP。** 断网状态下强制门户会劫持所有 HTTP 请求并返回 200，页面其实是认证页。HTTPS 没有目标站点的证书，握手直接失败，判断才准。

**日志里的"认证失败"经常是假象。** Portal 返回 `workFlowNode is empty` 通常意味着上一次登录**已经被接受**，只是出口放行有延迟（实测 10 分钟到 5 小时不等，完全由校方决定）。这种时候不要加大重试频率，越试越被拒，等就行。

**手动认证不要和自动检测并发。** 两条流程同时向 Portal 发请求会触发重复登录拒绝。代码里用一把非阻塞锁（`_busy`）串起来，抢不到锁的一方直接放弃。

**飞牛/群晖这类 NAS 上跑要注意会话归属。** 认证会话挂在主路由上，NAS 在 NAT 后面，但发起的认证请求会被 Portal 映射到那条共享会话——这是整套方案能成立的前提，已在实测中确认。

## 测试

```bash
python tests/test_panel.py        # 本地回归：起假后端验证接口与状态机
bash tests/remote_test.sh         # 端到端：临时容器 + LOGIN_BIN=/bin/true，不碰真实校园网
python tools/gen_preview.py       # 从 app.py 抽出 HTML，生成离线预览页
```

`tests/remote_test.sh` 会起一个独立容器跑完整链路，不改动线上实例，也不会真的去认证。

## 目录

```
app.py                 核心程序：keeper 线程 + Web 面板（单文件，标准库）
ysu_net_login/         锐捷 V2 认证工具（上游：KamijoToma/YSUNetLoginv2）
panel-preview.html     离线预览页（模拟后端，可直接双击打开）
tests/                 回归测试与端到端脚本
tools/gen_preview.py   从 app.py 重新生成预览页
```

## 许可

MIT。`ysu_net_login/` 来自 [KamijoToma/YSUNetLoginv2](https://github.com/KamijoToma/YSUNetLoginv2)，版权归原作者。

## 免责

仅供个人学习与自用。请遵守所在学校的网络使用规定，不要用于绕过计费或多设备共享等用途。
