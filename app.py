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
        "你是一個法律AI助理。你有一個工具，可以呼叫 Search API 搜尋資訊。"
        "請在需要時使用 [SEARCH] 指令，例如：[SEARCH]請搜尋最新的離婚法規。"
        "完成搜索後，你會收到以 [SEARCH_RESULT] 開頭的結果，並應將其整合進回覆中。"
        "請注意: 用戶可能會反覆詢問類似問題，請依照最新的搜尋結果進行回覆，避免使用過期資訊。"
    )
}

def get_user_session(user_id):
    """取得或初始化用戶的對話記錄"""
    if user_id not in user_sessions or time.time() - user_sessions[user_id]['last_time'] > SESSION_TIMEOUT:
        user_sessions[user_id] = {'messages': [SYSTEM_PROMPT], 'last_time': time.time()}
    return user_sessions[user_id]['messages']

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

def integrate_search_result(user_id, search_result):
    """將搜尋結果整合進對話記錄並推送結果"""
    update_user_session(user_id, "system", f"[SEARCH_RESULT] {search_result}")
    send_push_message(user_id, f"搜尋結果如下：\n{search_result}")

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

    # 更新用戶對話
    update_user_session(user_id, "user", user_message)

    if "[SEARCH]" in user_message:
        # 模擬搜尋結果（可替換成實際 API 呼叫）
        search_result = "這是模擬的搜尋結果，內容如下：..."
        integrate_search_result(user_id, search_result)
    else:
        try:
            # 呼叫 OpenAI 取得回應
            response = client.chat.completions.create(
                model="gpt-4o-mini",
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
            # 失敗時使用 push_message
            logging.error(f"回應失敗，改用推播訊息: {e}")
            send_push_message(user_id, f"AI 回應發生錯誤，請稍後再試：{str(e)}")
        except Exception as e:
            send_push_message(user_id, f"發生未知錯誤：{str(e)}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)