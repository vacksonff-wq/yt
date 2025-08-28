# Chat WebSocket + WebRTC (fa-IR)

این پروژه یک چت ساده با **FastAPI + WebSocket** به‌همراه **تماس صوتی دو نفره (WebRTC)** است.

## اجرا
```bash
pip install -r requirements.txt
uvicorn mine:app --reload
```
سپس در مرورگر باز کنید: `http://localhost:8000/?room=lobby`  
برای تست تماس، همین آدرس را در تب/دستگاه دیگری باز کنید.

## ساختار
```
.
├─ mine.py            # سرور FastAPI + WebSocket + سیگنالینگ
├─ index.html         # کلاینت HTML
├─ static/
│  ├─ styles.css      # استایل
│  └─ app.js          # منطق کلاینت + WebRTC
└─ requirements.txt
```

> اگر می‌خواهید اسم فایل ورودی متفاوت باشد، از دستور زیر استفاده کنید:
```bash
uvicorn mine:app --reload
```
(که یعنی ماژول `mine` و آبجکت `app`).

## نکات
- برای خروجی صدا (Speaker/Device) از `setSinkId` استفاده شده که در بعضی مرورگرها فعال است.
- STUN عمومی: `stun:stun.l.google.com:19302`.
- برای محیط Production حتماً `JWT_SECRET` را در env ست کنید.
