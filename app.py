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

# 已处理的消息 ID
processed_message_ids = set()
message_lock = threading.Lock()
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
        logging.error(f"处理回调时发生异常：{e}")
    finally:
        return 'OK'  # 确保返回 200 OK

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    try:
        message_id = event.message.id
        with message_lock:
            if message_id in processed_message_ids:
                logging.info(f"重复的消息 ID：{message_id}，已跳过处理。")
                return
            else:
                processed_message_ids.add(message_id)

        user_id = event.source.user_id
        user_message = event.message.text.strip()

        if user_message.lower() == "開始新對話":
            # 开始新的对话
            with conversation_lock:
                user_conversations[user_id] = None
            reply_text = "已开始新的对话！请输入您的问题。"
            # 立即回复
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text)
            )
            return  # 确保函数退出

        else:
            # 立即回复“处理中”，然后异步处理
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="处理中，请稍候...")
            )

            def process_message():
                # 获取用户的 conversation_id
                with conversation_lock:
                    conversation_id = user_conversations.get(user_id)

                # 准备发送给 Dify API 的数据
                payload = {
                    "query": user_message,
                    "user": user_id,
                    "response_mode": "blocking",
                    "inputs": {}
                }

                if conversation_id:
                    payload["conversation_id"] = conversation_id

                headers = {
                    "Authorization": f"Bearer {DIFY_API_KEY}",
                    "Content-Type": "application/json"
                }

                try:
                    response = requests.post(DIFY_API_URL, json=payload, headers=headers, timeout=10)
                    response.raise_for_status()
                    result = response.json()

                    reply_text = result.get("answer", "抱歉，我无法理解您的问题。")
                    new_conversation_id = result.get("conversation_id")

                    with conversation_lock:
                        user_conversations[user_id] = new_conversation_id

                    # 发送推送消息
                    line_bot_api.push_message(
                        user_id,
                        TextSendMessage(text=reply_text)
                    )

                except requests.RequestException as e:
                    logging.error(f"请求 Dify API 出错：{e}")
                    line_bot_api.push_message(
                        user_id,
                        TextSendMessage(text="抱歉，服务器繁忙，请稍后再试。")
                    )
                except LineBotApiError as e:
                    logging.error(f"发送推送消息时发生错误：{e}")

            # 启动新线程处理消息
            threading.Thread(target=process_message).start()

    except Exception as e:
        logging.error(f"处理消息时发生异常：{e}")

@handler.default()
def default(event):
    # 对于非 MessageEvent 的事件，不做处理
    pass

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
