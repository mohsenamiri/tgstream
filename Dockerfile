# 1) بیس ایمیج سبک برای Python
FROM python:3.11-slim

# 2) تنظیم محیط
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 3) نصب وابستگی‌های سیستمی لازم (در صورت نیاز)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 4) ساخت دایرکتوری اپ
WORKDIR /app

# 5) کپی requirements و نصب پکیج‌ها
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# 6) کپی کل سورس
COPY . /app

# 7) نقطه شروع کانتینر
CMD ["python", "app.py"]
