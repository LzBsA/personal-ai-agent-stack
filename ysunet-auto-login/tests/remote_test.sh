#!/bin/bash
# 端到端验证 (独立测试容器, LOGIN_BIN=/bin/true 不触发真实认证)
# 覆盖: 手动认证流程 + 断网时间手动填写 + 页面元素
set -u
BASE=http://localhost:8099
J='Content-Type: application/json'

docker rm -f ysunet-test ysunet-test2 >/dev/null 2>&1
docker run -d --name ysunet-test \
  -e LOGIN_BIN=/bin/true -e DATA_DIR=/tmp/d \
  -e RUIJIE_USERNAME=demo-user -e RUIJIE_PASSWORD=x \
  -e RUIJIE_SERVICE=test -e CHECK_INTERVAL=99999 \
  -p 8099:8080 ysunet-panel:latest >/dev/null
sleep 6

echo "===== 1) 页面关键元素 (每项应为 1) ====="
curl -s $BASE/ | grep -c 'id="manualBtn"'
curl -s $BASE/ | grep -c 'class="btn teal"'
curl -s $BASE/ | grep -c 'id="manualState"'
curl -s $BASE/ | grep -c 'function manualLogin()'
curl -s $BASE/ | grep -c 'id="setWinStart"'
curl -s $BASE/ | grep -c 'id="setWinEnd"'
curl -s $BASE/ | grep -c 'id="footWin"'
curl -s $BASE/ | grep -c "/api/login"

echo "===== 2) 初始 status (应带 window_start/window_end) ====="
curl -s $BASE/api/status; echo

echo "===== 3) 触发手动认证 POST /api/login ====="
curl -s -X POST $BASE/api/login; echo

sleep 0.6
echo "===== 4) 进行中再点一次 (应 400 + 明确提示) ====="
curl -s -w '  <- http_code=%{http_code}\n' -X POST $BASE/api/login

echo "===== 5) 进行中 GET /api/status ====="
curl -s $BASE/api/status; echo

echo "===== 6) 认证结束后 status (应 manual_ok=true) ====="
sleep 10
curl -s $BASE/api/status; echo

echo "===== 7) 断网时间 手动填写: 改成 01:00-06:00 ====="
curl -s -X POST -H "$J" -d '{"username":"demo-user","window_start":"01:00","window_end":"06:00"}' $BASE/api/settings; echo
curl -s $BASE/api/settings; echo

echo "===== 8) 非法时间应被拦 (25:00 / abc) ====="
curl -s -w '  <- http_code=%{http_code}\n' -X POST -H "$J" -d '{"username":"demo-user","window_start":"25:00"}' $BASE/api/settings
curl -s -w '  <- http_code=%{http_code}\n' -X POST -H "$J" -d '{"username":"demo-user","window_end":"abc"}' $BASE/api/settings

echo "===== 9) 还原默认 23:30-05:30 ====="
curl -s -X POST -H "$J" -d '{"username":"demo-user","window_start":"23:30","window_end":"05:30"}' $BASE/api/settings; echo

echo "===== 10) 容器内 config.json 是否落盘 ====="
docker exec ysunet-test cat /tmp/d/config.json; echo

echo "===== 11) 运行日志 ====="
curl -s "$BASE/api/logs?n=25"; echo

echo "===== 12) 未配置密码时点手动认证 (应 400 指向认证配置) ====="
docker run -d --name ysunet-test2 -e LOGIN_BIN=/bin/true -e DATA_DIR=/tmp/d2 \
  -e RUIJIE_USERNAME=demo-user -e CHECK_INTERVAL=99999 \
  -p 8098:8080 ysunet-panel:latest >/dev/null
sleep 6
curl -s -w '  <- http_code=%{http_code}\n' -X POST localhost:8098/api/login

echo "===== 13) 清理 ====="
docker rm -f ysunet-test ysunet-test2 >/dev/null 2>&1 && echo cleaned
