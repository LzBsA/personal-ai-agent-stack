FROM python:3.11-slim

WORKDIR /srv

# 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 认证工具源码（纯 python，直接进 site-packages）
COPY ysu_net_login /usr/local/lib/python3.11/site-packages/ysu_net_login
RUN printf '#!/bin/sh\nexec python3 -m ysu_net_login.ruijie_cli "$@"\n' > /usr/local/bin/ysunetlogin \
 && chmod +x /usr/local/bin/ysunetlogin

# Web 面板 + 自动认证 keeper
COPY app.py /srv/app.py

EXPOSE 8080
CMD ["python3", "/srv/app.py"]
