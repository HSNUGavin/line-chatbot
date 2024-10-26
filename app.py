from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import *
from openai import OpenAI
import os
import time

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
    "content": "你是一個法律AI智能助理，會回答任何與法律有關的疑難雜症。"
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

    # 初始化對話
    user_sessions[user_id] = {'messages': [SYSTEM_PROMPT], 'last_time': current_time}
    return user_sessions[user_id]['messages']

def update_user_session(user_id, role, content):
    """更新用戶的對話記錄"""
    messages = get_user_session(user_id)
    messages.append({"role": role, "content": content})
    user_sessions[user_id]['last_time'] = time.time()  # 更新互動時間

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
    user_id = event.source.user_id  # 取得用戶 ID
    user_message = event.message.text

    if user_message.lower() == "開始新對話":
        # 清除該用戶的對話記錄
        user_sessions.pop(user_id, None)
        reply_text = "已開始新的對話！請輸入您的法律問題。"
    else:
        # 將用戶訊息加入對話記錄
        update_user_session(user_id, "user", user_message)

        # 呼叫 OpenAI API 並取得回應
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",  # 使用 GPT-4o-mini 模型
                messages=get_user_session(user_id)
            )
            reply_text = response['choices'][0]['message']['content'].strip()
            update_user_session(user_id, "assistant", reply_text)

        except Exception as e:
            reply_text = f"抱歉，發生錯誤：{str(e)}"

    # 傳送回應給 LINE 使用者，附加 Quick Reply 選項
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
