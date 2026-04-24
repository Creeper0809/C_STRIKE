FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# psycopg2-binary는 wheel 제공이라 빌드 도구 불필요하지만, 런타임 libpq는 slim에 없어서 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

# 외주 원본 단일 인스턴스 lock 위치. Docker에선 컨테이너 재시작 시 자동 해제되므로 문제 없음
RUN mkdir -p /app/.tmp /app/logs

EXPOSE 5000

CMD ["python", "-m", "ops_bot"]
