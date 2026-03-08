#!/usr/bin/env python3
"""
Запустить ОДИН РАЗ после деплоя.
python setup_webhook.py
"""
import os, requests

TOKEN = os.environ.get("BOT_TOKEN", "")
URL = os.environ.get("WEBAPP_URL", "https://ponton-bot-production.up.railway.app")

if not TOKEN:
    print("Укажи BOT_TOKEN=... python setup_webhook.py")
    exit(1)

# Webhook
r = requests.post(f"https://api.telegram.org/bot{TOKEN}/setWebhook",
                  json={"url": f"{URL}/webhook/{TOKEN}"})
print("Webhook:", r.json())

# Menu button
r2 = requests.post(f"https://api.telegram.org/bot{TOKEN}/setChatMenuButton",
                   json={"menu_button": {"type": "web_app", "text": "🎬 Купить билет",
                                         "web_app": {"url": URL}}})
print("Menu:", r2.json())
print(f"\n✅ Готово! https://t.me/Ponton_ast_bot")
