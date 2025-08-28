# Deploy to Render.com

## Quick Start
1. Push these files to a **GitHub** repo (whole folder).
2. In Render, click **New > Web Service** and choose your repo.
3. For Environment select **Python** (or use this `render.yaml` as a Blueprint).
4. Set:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn mine:app --host 0.0.0.0 --port $PORT`
   - **Health Check Path**: `/health`
   - **Environment Variables**:
     - `PYTHON_VERSION=3.11`
     - `JWT_SECRET` (click *Generate* or set a value yourself)

> WebSockets روی Render به‌طور پیش‌فرض فعاله. کلاینت به‌صورت خودکار روی HTTPS از `wss://` استفاده می‌کند.

## Notes
- پلن Free بعد از چند دقیقه بی‌ترافیکی **Sleep** می‌شود و اتصال WebSocket می‌پرد. برای پایداری تماس‌ها از پلن Starter استفاده کنید.
- اگر کاربران پشت NAT موبایل هستند، برای کیفیت بهتر صدا یک **TURN** به پیکربندی ICE اضافه کنید (در `static/app.js` تابع `ensurePeer()`):
  ```js
  const cfg = { iceServers: [
    { urls: 'stun:stun.l.google.com:19302' },
    { urls: 'turn:YOUR_TURN_HOST:3478', username: 'USER', credential: 'PASS' }
  ]};
  ```
