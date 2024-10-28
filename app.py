from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import *
import openai
import os
import time
import re
import threading
import requests

app = Flask(__name__)

# 初始化 LINE Bot 和 OpenAI API
line_bot_api = LineBotApi(os.environ['CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(os.environ['CHANNEL_SECRET'])

# 初始化 OpenAI 客戶端
openai.api_key = os.environ.get("OPENAI_API_KEY")

# 儲存用戶對話記錄
user_sessions = {}
SESSION_TIMEOUT = 30 * 60  # 30 分鐘

SYSTEM_PROMPT = SYSTEM_PROMPT = {
    "role": "system",
    "content": (
        "你是一個法律AI智能助理，可以回答法律相關的問題。"
        "請你引導用戶詢問具體的問題，當你大概知道問題是什麼之後，請你呼叫一個 `search API`。"
        "要發起搜索請求，請使用以下格式：`[SEARCH]你的問題`。"
        "例如`[SEARCH]請搜尋最新的離婚法規`。"
        "發起搜索後，對話會暫停，直到結果返回，你會收到以`[SEARCH_RESULT]`開頭的結果。"
        "你應該將這些結果整合到你的回覆中，並給用戶提供完整的答案。"
        "請注意：請你盡量使用 `search API`。"
    )
}

def get_user_session(user_id):
    current_time = time.time()
    if user_id in user_sessions:
        if current_time - user_sessions[user_id]['last_time'] > SESSION_TIMEOUT:
            del user_sessions[user_id]
        else:
            return user_sessions[user_id]['messages']

    user_sessions[user_id] = {'messages': [SYSTEM_PROMPT], 'last_time': current_time}
    return user_sessions[user_id]['messages']

def update_user_session(user_id, role, content):
    messages = get_user_session(user_id)
    messages.append({"role": role, "content": content})
    user_sessions[user_id]['last_time'] = time.time()

def call_dify_workflow(question):
    API_URL = "https://api.dify.ai/v1/workflows/run"
    API_KEY = os.environ.get("DIFY_API_KEY")
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "inputs": {"Question": question},
        "response_mode": "blocking",
    }

    try:
        response = requests.post(API_URL, json=payload, headers=headers)
        response.raise_for_status()
        result = response.json()
        if result["data"]["status"] == "succeeded":
            return result["data"]["outputs"].get("text", "未找到合適的結果。")
        else:
            return "搜索失敗，請稍後再試。"
    except requests.RequestException as e:
        return f"API 請求失敗：{e}"

def handle_search_request(user_id, search_query):
    def search_and_respond():
        search_result = call_dify_workflow(search_query)
        update_user_session(user_id, "system", f"搜索結果：{search_result}")
        messages = get_user_session(user_id)
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=messages
        )
        reply_text = response.choices[0].message.content.strip()
        update_user_session(user_id, "assistant", reply_text)
        line_bot_api.push_message(user_id, TextSendMessage(text=reply_text))

    threading.Thread(target=search_and_respond).start()

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text

    if user_message.lower() == "開始新對話":
        user_sessions.pop(user_id, None)
        reply_text = "已開始新的對話！請輸入您的法律問題。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    update_user_session(user_id, "user", user_message)

    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=get_user_session(user_id)
        )
        reply_text = response.choices[0].message.content.strip()
        update_user_session(user_id, "assistant", reply_text)

        search_pattern = r"\[SEARCH\](.*)"
        match = re.search(search_pattern, reply_text)
        if match:
            search_query = match.group(1).strip()
            reply_text = "正在進行搜索，請稍候..."
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
            handle_search_request(user_id, search_query)
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

    except Exception as e:
        reply_text = f"發生錯誤：{e}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
