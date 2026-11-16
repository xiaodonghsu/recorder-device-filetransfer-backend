# 嘉环声枢录音文件传输服务（独立镜像）
# 构建：docker build -t voicehub-ogg:1.0.0 -f filetransfer/Dockerfile filetransfer
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    API_PREFIX=/voicehub-ogg \
    STORAGE_DIR=/var/lib/voicehub-ogg \
    STORAGE_BACKEND=local \
    DATABASE_URL=sqlite:////var/lib/voicehub-ogg/filetransfer.db \
    CASDOOR_VERIFY_METHOD=auto

WORKDIR /srv/filetransfer

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 上传文件必须落在挂载卷上，避免容器重建丢数据
VOLUME ["/var/lib/voicehub-ogg"]
EXPOSE 8030

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8030/healthz', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8030", "--proxy-headers"]
