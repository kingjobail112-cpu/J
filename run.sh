#!/bin/bash
# বট বন্ধ হলে স্বয়ংক্রিয়ভাবে পুনরায় চালু হবে
while true; do
    echo "[$(date)] বট চালু হচ্ছে..."
    python bot.py
    echo "[$(date)] বট বন্ধ হয়েছে। ১০ সেকেন্ড পরে আবার চালু হবে..."
    sleep 10
done
