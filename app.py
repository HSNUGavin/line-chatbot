import os
import time
import logging
import requests
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import *
import threading

# 設置日誌級別
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# 初始化 LINE Bot
line_bot_api = LineBotApi(os.environ['CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(os.environ['CHANNEL_SECRET'])

# Dify API 設定
DIFY_API_URL = "https://api.dify.ai/v1/chat-messages"
DIFY_API_KEY = os.environ.get("DIFY_API_KEY")

# 用戶對話紀錄存儲區
user_conversations = {}  # 儲存用戶的 conversation_id

@app.route("/callback", methods=['POST'])
def callback():
    """處理 LINE Webhook 回調"""
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """處理用戶訊息"""
    user_id = event.source.user_id
    user_message = event.message.text.strip()

    if user_message.lower() == "開始新對話":
        # 開始新的對話
        user_conversations[user_id] = None
        reply_text = "已開始新的對話！請輸入您的問題。"
    else:
        # 獲取用戶的 conversation_id
        conversation_id = user_conversations.get(user_id)

        # 準備要發送給 Dify API 的資料
        payload = {
            "query": user_message,
            "user": user_id,
            "conversation_id": conversation_id if conversation_id else "",
            "response_mode": "blocking",  # 使用阻塞模式獲取完整回應
            "inputs": {}
        }

        headers = {
            "Authorization": f"Bearer {DIFY_API_KEY}",
            "Content-Type": "application/json"
        }

        try:
            # 呼叫 Dify API
            response = requests.post(DIFY_API_URL, json=payload, headers=headers)
            response.raise_for_status()
            result = response.json()

            # 獲取回應內容和新的 conversation_id
            reply_text = result.get("answer", "抱歉，我無法理解您的問題。")
            conversation_id = result.get("conversation_id")

            # 更新用戶的 conversation_id
            user_conversations[user_id] = conversation_id

        except requests.RequestException as e:
            reply_text = f"抱歉，發生了錯誤：{e}"

    # 發送回應給用戶
    message = TextSendMessage(
        text=reply_text,
        quick_reply=QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="開始新對話", text="開始新對話")),
            QuickReplyButton(action=MessageAction(label="繼續對話", text="繼續對話"))
        ])
    )
    line_bot_api.reply_message(event.reply_token, message)

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
