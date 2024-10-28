import uuid  # 用於生成唯一的對話 ID
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import *
from openai import OpenAI
import os
import time
import threading
import requests
import re
import logging

# 設置日誌級別
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# 初始化 LINE Bot 和 OpenAI API
line_bot_api = LineBotApi(os.environ['CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(os.environ['CHANNEL_SECRET'])
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# 用戶對話記錄存儲區
user_sessions = {}
SESSION_TIMEOUT = 30 * 60  # 30 分鐘

# Dify API 設定
DIFY_API_URL = "https://api.dify.ai/v1/workflows/run"
DIFY_API_KEY = os.environ.get("DIFY_API_KEY")

# AI 系統提示
SYSTEM_PROMPT = {
    "role": "system",
    "content": (
        "你是一個法律AI助理。你有一個工具，可以呼叫 `Search API` 搜尋資訊。"
        "請在需要時使用 `[SEARCH]` 指令，例如：`[SEARCH]請搜尋最新的離婚法規`。"
        "完成搜索後，你會收到以 `[SEARCH_RESULT]` 開頭的結果，並應將其整合進回覆中。"
        "請注意: 用戶來詢問的問題可能是同一個，請你根據上下文判斷你要使用 API 搜尋的問題，"
        "並且透過問答深入了解用戶真正想解決的問題是什麼。"
    )
}

def generate_conversation_id():
    """生成唯一的對話 ID"""
    return str(uuid.uuid4())

def get_user_session(user_id, conversation_id):
    """取得或初始化用戶的對話記錄，使用 conversation_id 區分不同對話"""
    current_time = time.time()
    if user_id in user_sessions:
        if conversation_id in user_sessions[user_id]:
            session = user_sessions[user_id][conversation_id]
            if current_time - session['last_time'] <= SESSION_TIMEOUT:
                return session['messages']

    # 初始化新對話
    if user_id not in user_sessions:
        user_sessions[user_id] = {}
    user_sessions[user_id][conversation_id] = {
        'messages': [SYSTEM_PROMPT],
        'last_time': current_time
    }
    return user_sessions[user_id][conversation_id]['messages']

def update_user_session(user_id, conversation_id, role, content):
    """更新對話記錄"""
    messages = get_user_session(user_id, conversation_id)
    messages.append({"role": role, "content": content})
    user_sessions[user_id][conversation_id]['last_time'] = time.time()

def call_dify_workflow(question, user_id):
    """呼叫 Dify Workflow API 並回傳搜尋結果"""
    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "inputs": {"Question": question},
        "response_mode": "blocking",
        "user": user_id
    }

    logging.info(f"發送 API 請求至 {DIFY_API_URL}，問題：{question}")

    try:
        response = requests.post(DIFY_API_URL, json=payload, headers=headers)
        response.raise_for_status()
        result = response.json()
        logging.info(f"API 回應內容：{result}")

        if result["data"]["status"] == "succeeded":
            return result["data"]["outputs"].get("text", "未找到相關結果。")
        else:
            return f"API 搜尋失敗：{result['data'].get('error', '未知錯誤')}"

    except requests.RequestException as e:
        return f"API 請求失敗：{e}"

def handle_search_request(user_id, conversation_id, search_query):
    """非同步處理搜尋請求"""
    def search_and_respond():
        search_result = call_dify_workflow(search_query, user_id)
        update_user_session(user_id, conversation_id, "system", f"[SEARCH_RESULT] {search_result}")

        messages = get_user_session(user_id, conversation_id)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages
        )
        reply_text = response.choices[0].message.content.strip()
        update_user_session(user_id, conversation_id, "assistant", reply_text)

        line_bot_api.push_message(user_id, TextSendMessage(text=reply_text))

    threading.Thread(target=search_and_respond).start()

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
    conversation_id = generate_conversation_id()
    user_message = event.message.text

    if user_message.lower() == "開始新對話":
        conversation_id = generate_conversation_id()  # 重置對話 ID
        reply_text = "已開始新的對話！請輸入您的法律問題。"
    else:
        update_user_session(user_id, conversation_id, "user", user_message)
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=get_user_session(user_id, conversation_id)
            )
            reply_text = response.choices[0].message.content.strip()
            update_user_session(user_id, conversation_id, "assistant", reply_text)

            search_pattern = r"\[SEARCH\](.*)"
            match = re.search(search_pattern, reply_text)
            if match:
                search_query = match.group(1).strip()
                reply_text = "正在為您搜尋相關資訊，請稍候..."
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
                handle_search_request(user_id, conversation_id, search_query)
                return

        except Exception as e:
            reply_text = f"發生錯誤：{e}"

    message = TextSendMessage(
        text=reply_text,
        quick_reply=QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="開始新對話", text="開始新對話")),
            QuickReplyButton(action=MessageAction(label="繼續對話", text="繼續對話")),
            QuickReplyButton(action=MessageAction(label="搜尋資料庫", text="我想要搜尋資料庫"))
        ])
    )
    line_bot_api.reply_message(event.reply_token, message)

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
