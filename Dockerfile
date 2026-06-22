FROM python:3.12-slim

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

# cron インストール
RUN apt-get update && apt-get install -y cron tzdata && rm -rf /var/lib/apt/lists/*

# JST timezone
ENV TZ=Asia/Tokyo
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 毎朝6:00 JST に実行
RUN echo "0 6 * * * cd /app && /usr/local/bin/python main.py >> /var/log/cron.log 2>&1" | crontab -

CMD ["cron", "-f"]
