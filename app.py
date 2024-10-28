from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import *
from openai import OpenAI
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
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# 儲存用戶對話記錄的字典（格式：{user_id: [(role, content), ...]})
user_sessions = {}
SESSION_TIMEOUT = 30 * 60  # 30 分鐘（以秒為單位）

SYSTEM_PROMPT = {
    "role": "system",
    "content": (
        "你是一個法律AI智能助理，會回答任何與法律有關的疑難雜症。"
        "當你需要更多資訊時，可以使用以下格式請求搜尋：`[SEARCH]你的問題`。"
        "收到搜尋結果後，會以`[SEARCH_RESULT]`開頭，請將其整合進你的回覆中。"
        "請勿在用戶沒有詢問時主動使用搜尋功能。"
    )
}

def get_user_session(user_id):
    """取得用戶的對話記錄，若超過 30 分鐘則重置"""
    current_time = time.time()
    if user_id in user_sessions:
        last_interaction = user_sessions[user_id]['last_time']
        if current_time - last_interaction > SESSION_TIMEOUT:
            del user_sessions[user_id]  # 刪除過期的對話記錄
        else:
            return user_sessions[user_id]['messages']

    # 如果是新的對話或已過期，初始化對話
    user_sessions[user_id] = {'messages': [SYSTEM_PROMPT], 'last_time': current_time}
    return user_sessions[user_id]['messages']

def update_user_session(user_id, role, content):
    """更新用戶的對話記錄"""
    messages = get_user_session(user_id)
    messages.append({"role": role, "content": content})
    user_sessions[user_id]['last_time'] = time.time()  # 更新互動時間

def call_dify_workflow(question):
    """
    呼叫 Dify Workflow API 並回傳搜尋結果。
    :param question: 要查詢的問題
    :return: 搜尋結果文字
    """
    API_URL = "https://api.dify.ai/v1/workflows/run"
    API_KEY = os.environ.get("DIFY_API_KEY")  # 將 API 金鑰設置為環境變數
    USER_ID = "your_unique_user_id"  # 設定一個唯一的使用者 ID
    WORKFLOW_ID = "56389aca-bed6-4333-96a9-ce89f27b780c"  # 請替換為您的 Workflow ID

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "inputs": {"Question": question},
        "response_mode": "blocking",
        "user": USER_ID,
    }

    try:
        response = requests.post(API_URL, json=payload, headers=headers)
        response.raise_for_status()
        result = response.json()

        if result["data"]["status"] == "succeeded":
            outputs = result["data"]["outputs"]
            output_text = outputs.get("text") or outputs.get("test", "")
            return output_text
        else:
            return "抱歉，無法獲取搜尋結果。"

    except requests.exceptions.RequestException as e:
        return f"API 請求失敗：{e}"

def handle_search_request(user_id, search_query):
    # 調用搜尋 API，並在完成後更新對話
    def search_and_update():
        search_result = call_dify_workflow(search_query)
        # 將搜尋結果作為系統訊息加入對話記錄
        update_user_session(user_id, "system", f"[SEARCH_RESULT]{search_result}")
        # 通知用戶搜尋結果已更新
        send_search_result_to_user(user_id)

    # 使用 threading 模組非同步地調用搜尋 API
    threading.Thread(target=search_and_update).start()

def send_search_result_to_user(user_id):
    # 取得最新的對話記錄，讓 AI 生成新的回覆
    messages = get_user_session(user_id)
    response = client.chat.completions.create(
        model="gpt-4",
        messages=messages
    )
    reply_text = response.choices[0].message.content.strip()
    # 將 AI 回應加入對話記錄
    update_user_session(user_id, "assistant", reply_text)
    # 發送訊息給用戶
    line_bot_api.push_message(user_id, TextSendMessage(text=reply_text))

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

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
        # 清除該用戶的對話記錄
        user_sessions.pop(user_id, None)
        reply_text = "已開始新的對話！請輸入您的法律問題。"
        message = TextSendMessage(text=reply_text)
        line_bot_api.reply_message(event.reply_token, message)
        return

    # 將用戶訊息加入對話記錄
    update_user_session(user_id, "user", user_message)

    # 呼叫 OpenAI API 並取得回應
    try:
        response = client.chat.completions.create(
            model="gpt-4",
            messages=get_user_session(user_id)
        )

        # 取得回應文字
        reply_text = response.choices[0].message.content.strip()

        # 將 AI 回應加入對話記錄
        update_user_session(user_id, "assistant", reply_text)

        # 檢查是否包含搜尋請求
        search_pattern = r"\[SEARCH\](.*)"
        match = re.search(search_pattern, reply_text)
        if match:
            search_query = match.group(1).strip()
            # 通知用戶正在搜尋
            initial_reply = "好的，我正在為您查詢相關資訊，請稍候。"
            message = TextSendMessage(text=initial_reply)
            line_bot_api.reply_message(event.reply_token, message)
            # 非同步地處理搜尋請求
            handle_search_request(user_id, search_query)
        else:
            # 正常回覆用戶
            message = TextSendMessage(text=reply_text)
            line_bot_api.reply_message(event.reply_token, message)

    except Exception as e:
        reply_text = f"抱歉，發生錯誤：{str(e)}"
        message = TextSendMessage(text=reply_text)
        line_bot_api.reply_message(event.reply_token, message)

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
