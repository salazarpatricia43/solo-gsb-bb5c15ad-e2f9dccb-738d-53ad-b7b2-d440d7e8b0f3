FROM python:3.12-slim

WORKDIR /app

# 构建期安装依赖（利用镜像层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

ENV DB_PATH=/data/lab.db
EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
