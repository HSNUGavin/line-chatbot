import uuid  # 用于生成唯一的对话 ID
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import *
import os
import time
import threading
import requests
import re
import logging

# 设置日志级别
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# 初始化 LINE Bot 和 Dify API
line_bot_api = LineBotApi(os.environ['CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(os.environ['CHANNEL_SECRET'])

# Dify API 设置
DIFY_API_URL = "https://api.dify.ai/v1/chat-messages"
DIFY_API_KEY = os.environ.get("DIFY_API_KEY")

# 用于存储用户的 conversation_id
conversation_ids = {}

def call_dify_chat_messages(user_message, user_id, conversation_id=None):
    """调用 Dify Chat Messages API 并返回响应"""
    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "inputs": {},
        "query": user_message,
        "response_mode": "blocking",
        "user": user_id
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id

    logging.info(f"发送 API 请求至 {DIFY_API_URL}，消息：{user_message}")

    try:
        response = requests.post(DIFY_API_URL, json=payload, headers=headers)
        response.raise_for_status()
        result = response.json()
        logging.info(f"API 响应内容：{result}")

        return result
    except requests.RequestException as e:
        return {"error": f"API 请求失败：{e}"}

@app.route("/callback", methods=['POST'])
def callback():
    """处理 LINE Webhook 回调"""
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """处理用户消息"""
    user_id = event.source.user_id
    user_message = event.message.text.strip()

    global conversation_ids  # 访问全局字典

    if user_message.lower() == "开始新对话":
        # 开始新的对话
        conversation_ids[user_id] = None
        reply_text = "已开始新的对话！请输入您的法律问题。"
    else:
        # 获取用户的 conversation_id
        conversation_id = conversation_ids.get(user_id)
        # 调用 Dify API 获取响应
        result = call_dify_chat_messages(user_message, user_id, conversation_id)
        if "error" in result:
            reply_text = result["error"]
        else:
            # 获取响应文本
            reply_text = result.get("answer", "未找到相关结果。")
            # 更新 conversation_id
            conversation_id = result.get("conversation_id")
            conversation_ids[user_id] = conversation_id

    # 发送回复给用户
    message = TextSendMessage(
        text=reply_text,
        quick_reply=QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="开始新对话", text="开始新对话")),
            QuickReplyButton(action=MessageAction(label="继续对话", text="继续对话")),
            QuickReplyButton(action=MessageAction(label="搜索数据库", text="我想要搜索数据库"))
        ])
    )
    line_bot_api.reply_message(event.reply_token, message)

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
