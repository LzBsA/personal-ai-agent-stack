FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    TZ=Asia/Shanghai

RUN pip install --no-cache-dir flask paramiko \
    && rm -rf /root/.cache/pip

WORKDIR /app
COPY app.py .
COPY templates ./templates

EXPOSE 8090

CMD ["python", "app.py"]
