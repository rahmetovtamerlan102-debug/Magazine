from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os

app = Flask(__name__)
CORS(app)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "8276815852,8840342301").split(',')))

@app.route('/order', methods=['POST'])
def create_order():
    data = request.json
    text = f"🛒 НОВЫЙ ЗАКАЗ С САЙТА!\n\n📦 {data['name']}\n💰 {data['price']}₽\n👤 {data['user']}"
    for admin_id in ADMIN_IDS:
        requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", params={"chat_id": admin_id, "text": text})
    return jsonify({"ok": True})

@app.route('/')
def health():
    return "OK", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
