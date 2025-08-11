FROM node:20-bullseye

# نصب ffmpeg و yt-dlp
RUN apt-get update && apt-get install -y python3-pip ffmpeg \
    && pip install yt-dlp

# ایجاد فولدر اپ
WORKDIR /app

# کپی فایل‌های پروژه
COPY package*.json ./
RUN npm install

COPY . .

# پورت API
EXPOSE 3000

# اجرای برنامه
CMD ["npm", "start"]
