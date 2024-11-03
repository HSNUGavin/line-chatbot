import os
import time
import logging
import requests
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import *
import threading

# 设置日志级别
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# 初始化 LINE Bot
line_bot_api = LineBotApi(os.environ['CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(os.environ['CHANNEL_SECRET'])

# Dify API 设置
DIFY_API_URL = "https://api.dify.ai/v1/chat-messages"
DIFY_API_KEY = os.environ.get("DIFY_API_KEY")

# 检查 DIFY_API_KEY 是否成功获取
if not DIFY_API_KEY:
    logging.error("DIFY_API_KEY 未设置，请检查环境变量。")

# 用户对话记录存储区
user_conversations = {}  # 存储用户的 conversation_id

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

    if user_message.lower() == "开始新对话":
        # 开始新的对话
        user_conversations[user_id] = None
        reply_text = "已开始新的对话！请输入您的问题。"
    else:
        # 获取用户的 conversation_id
        conversation_id = user_conversations.get(user_id)

        # 准备要发送给 Dify API 的数据
        payload = {
            "inputs": {},
            "query": user_message,
            "user": user_id,
            "response_mode": "blocking",  # 使用阻塞模式获取完整回复
            "conversation_id": conversation_id if conversation_id else "",
        }

        headers = {
            "Authorization": f"Bearer {DIFY_API_KEY}",
            "Content-Type": "application/json"
        }

        # 打印调试信息
        logging.info(f"Payload: {payload}")
        logging.info(f"Headers: {headers}")

        try:
            # 调用 Dify API
            response = requests.post(DIFY_API_URL, json=payload, headers=headers)
            response.raise_for_status()
            result = response.json()

            # 获取回复内容和新的 conversation_id
            reply_text = result.get("answer", "抱歉，我无法理解您的问题。")
            conversation_id = result.get("conversation_id")

            # 更新用户的 conversation_id
            user_conversations[user_id] = conversation_id

        except requests.RequestException as e:
            if e.response is not None:
                logging.error(f"Response content: {e.response.text}")
            logging.error(f"调用 Dify API 时发生错误：{e}")
            reply_text = f"抱歉，发生了错误：{e}"

    # 发送回复给用户
    message = TextSendMessage(
        text=reply_text,
        quick_reply=QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="开始新对话", text="开始新对话")),
            QuickReplyButton(action=MessageAction(label="继续对话", text="继续对话"))
        ])
    )
    line_bot_api.reply_message(event.reply_token, message)

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
