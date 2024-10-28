from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage, QuickReply, QuickReplyButton, MessageAction
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from openai import OpenAI
import os
import time
import logging

# 初始化 Flask app 和日誌系統
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# 初始化 LINE Bot 和 OpenAI 客戶端
line_bot_api = LineBotApi(os.environ['CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(os.environ['CHANNEL_SECRET'])
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# 用戶對話儲存
user_sessions = {}
SESSION_TIMEOUT = 30 * 60  # 30分鐘

SYSTEM_PROMPT = {
    "role": "system",
    "content": (
        "你是一個法律AI助理。你有一個工具，可以呼叫 `Search API` 搜尋資訊。"
        "完成搜索後，你會收到以 `[SEARCH_RESULT]` 開頭的結果，並應根據此結果產生回覆。"
        "請根據最新的搜尋結果進行回答，並避免使用過期資訊。"
    )
}

MAX_SESSION_LENGTH = 10  # 限制對話最多保留 10 則訊息

def get_user_session(user_id):
    """取得或初始化用戶的對話記錄"""
    if user_id not in user_sessions or time.time() - user_sessions[user_id]['last_time'] > SESSION_TIMEOUT:
        user_sessions[user_id] = {'messages': [SYSTEM_PROMPT], 'last_time': time.time()}
    messages = user_sessions[user_id]['messages']

    # 裁剪過長的訊息
    if len(messages) > MAX_SESSION_LENGTH:
        messages = messages[-MAX_SESSION_LENGTH:]
    user_sessions[user_id]['messages'] = messages
    return messages

def update_user_session(user_id, role, content):
    """更新用戶的對話記錄"""
    messages = get_user_session(user_id)
    messages.append({"role": role, "content": content})
    user_sessions[user_id]['last_time'] = time.time()

def send_push_message(user_id, text):
    """推播訊息給用戶"""
    try:
        line_bot_api.push_message(user_id, TextSendMessage(text=text))
    except LineBotApiError as e:
        logging.error(f"推播訊息失敗: {e}")

def store_search_result(user_id, search_result):
    """將搜尋結果存入對話紀錄（不直接回應用戶）"""
    update_user_session(user_id, "system", f"[SEARCH_RESULT] {search_result}")
    send_push_message(user_id, "已完成搜尋，系統會根據結果回應您的問題。")

def handle_search_request(user_id, user_message):
    """處理搜尋請求"""
    # 模擬搜尋結果（可替換為實際 API 呼叫）
    search_result = "這是模擬的搜尋結果：..."
    store_search_result(user_id, search_result)

    # 通知用戶搜尋已完成
    send_push_message(user_id, "搜尋完成，系統將根據結果進一步回應。")

@app.route("/callback", methods=['POST'])
def callback():
    """處理 LINE Webhook 請求"""
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    app.logger.info(f"Request body: {body}")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text.strip()

    # 更新用戶的對話
    update_user_session(user_id, "user", user_message)

    if "[SEARCH]" in user_message:
        # 處理搜尋請求
        handle_search_request(user_id, user_message)
    else:
        try:
            # 呼叫 OpenAI 取得回應
            response = client.chat.completions.create(
                model="gpt-4",
                messages=get_user_session(user_id)
            )
            reply_text = response.choices[0].message.content.strip()
            update_user_session(user_id, "assistant", reply_text)

            # 使用 reply_message 回應用戶
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=reply_text,
                    quick_reply=QuickReply(items=[
                        QuickReplyButton(action=MessageAction(label="開始新對話", text="開始新對話")),
                        QuickReplyButton(action=MessageAction(label="搜尋資料庫", text="[SEARCH]"))
                    ])
                )
            )
        except LineBotApiError as e:
            # 失敗時使用推播訊息
            logging.error(f"回應失敗，改用推播訊息: {e}")
            send_push_message(user_id, f"AI 回應發生錯誤，請稍後再試：{str(e)}")
        except Exception as e:
            # 處理未知錯誤
            send_push_message(user_id, f"發生未知錯誤：{str(e)}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
