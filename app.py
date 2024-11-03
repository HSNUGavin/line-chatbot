import os
import logging
import requests
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
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

# 用户对话记录存储区
user_conversations = {}  # 保存用户的 conversation_id

# 线程锁，确保线程安全
conversation_lock = threading.Lock()

@app.route("/callback", methods=['POST'])
def callback():
    """处理 LINE Webhook 回调"""
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        # 记录异常
        logging.error(f"处理回调时发生异常：{e}")
    return 'OK'  # 确保返回 200 OK

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """处理用户消息"""
    user_id = event.source.user_id
    user_message = event.message.text.strip()

    if user_message.lower() == "開始新對話":
        # 开始新的对话
        with conversation_lock:
            # 重置 conversation_id 和 conversation_variables
            user_conversations[user_id] = {
                'conversation_id': None,
                'conversation_variables': {}
            }
        reply_text = "已开始新的对话！请输入您的问题。"
    else:
        # 获取用户的对话信息
        with conversation_lock:
            user_conv = user_conversations.get(user_id, {
                'conversation_id': None,
                'conversation_variables': {}
            })

        conversation_id = user_conv.get('conversation_id')
        conversation_variables = user_conv.get('conversation_variables', {})

        # 准备发送给 Dify API 的数据
        payload = {
            "query": user_message,
            "user": user_id,
            "response_mode": "blocking"  # 使用阻塞模式获取完整回复
        }

        # 当开始新对话时，不包含 conversation_id，并可以传递 inputs
        if conversation_id:
            # 已有对话，包含 conversation_id，不传递 inputs
            payload["conversation_id"] = conversation_id
        else:
            # 新的对话，可以传递 inputs（如有需要）
            if conversation_variables:
                payload["inputs"] = conversation_variables

        headers = {
            "Authorization": f"Bearer {DIFY_API_KEY}",
            "Content-Type": "application/json"
        }

        try:
            # 调用 Dify API
            response = requests.post(DIFY_API_URL, json=payload, headers=headers, timeout=15)
            response.raise_for_status()
            result = response.json()

            # 获取回复内容和新的 conversation_id
            reply_text = result.get("answer", "抱歉，我无法理解您的问题。")
            new_conversation_id = result.get("conversation_id")

            # 更新用户的 conversation_id
            with conversation_lock:
                user_conversations[user_id] = {
                    'conversation_id': new_conversation_id,
                    'conversation_variables': conversation_variables  # 保持不变
                }

            # 在回复的末尾附加 conversation_id 供调试使用
            if new_conversation_id:
                reply_text += f"\n\n[conversation_id: {new_conversation_id}]"

        except requests.RequestException as e:
            logging.error(f"请求 Dify API 出错：{e}")
            reply_text = "抱歉，服务器繁忙，请稍后再试。"

    # 发送回复给用户
    try:
        message = TextSendMessage(
            text=reply_text,
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="開始新對話", text="開始新對話")),
                QuickReplyButton(action=MessageAction(label="繼續對話", text="繼續對話"))
            ])
        )
        line_bot_api.reply_message(event.reply_token, message)
    except LineBotApiError as e:
        logging.error(f"发送消息时发生错误：{e}")

@handler.default()
def default(event):
    # 对于非 MessageEvent 的事件，不做处理
    pass

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
