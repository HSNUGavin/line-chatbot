import os
import time
import uuid
import logging
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Dict, List, Optional
from datetime import datetime, timedelta

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, 
    QuickReply, QuickReplyButton, MessageAction
)
from openai import OpenAI
import requests
from tenacity import retry, stop_after_attempt, wait_exponential

# 基礎配置
app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# API clients
line_bot_api = LineBotApi(os.environ['CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(os.environ['CHANNEL_SECRET'])
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# 常量配置
SESSION_TIMEOUT = 30 * 60  # 30 minutes
MAX_REQUESTS_PER_MINUTE = 20
DIFY_API_URL = "https://api.dify.ai/v1/workflows/run"
DIFY_API_KEY = os.environ.get("DIFY_API_KEY")

SYSTEM_PROMPT = {
    "role": "system",
    "content": (
        "你是一個法律AI助理。你有一個工具，可以呼叫 `Search API` 搜尋資訊。"
        "請在需要時使用 `[SEARCH]` 指令，例如：`[SEARCH]請搜尋最新的離婚法規`。"
        "完成搜索後，你會收到以 `[SEARCH_RESULT]` 開頭的結果，並應將其整合進回覆中。"
        "請注意: 用戶可能會反覆詢問類似問題，請依照最新的搜尋結果進行回覆，避免使用過期資訊。"
    )
}

@dataclass
class UserSession:
    conversation_id: str
    messages: List[Dict]
    last_time: float
    current_topic: Optional[str] = None
    request_count: int = 0
    last_request_time: float = 0

class MemoryStore:
    def __init__(self):
        self.sessions: Dict[str, UserSession] = {}
        self.rate_limits: Dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_REQUESTS_PER_MINUTE))
        self.metrics: Dict[str, List] = defaultdict(list)
        
    def cleanup_old_sessions(self):
        """清理過期的會話"""
        current_time = time.time()
        expired_keys = [
            k for k, v in self.sessions.items() 
            if current_time - v.last_time > SESSION_TIMEOUT
        ]
        for k in expired_keys:
            del self.sessions[k]

class ConversationManager:
    def __init__(self, store: MemoryStore):
        self.store = store
        
    def get_session(self, user_id: str) -> UserSession:
        """獲取或創建新的會話"""
        self.store.cleanup_old_sessions()
        
        if user_id in self.store.sessions:
            return self.store.sessions[user_id]
            
        new_session = UserSession(
            conversation_id=str(uuid.uuid4()),
            messages=[SYSTEM_PROMPT],
            last_time=time.time()
        )
        self.store.sessions[user_id] = new_session
        return new_session

    def update_session(self, user_id: str, message: str, role: str) -> UserSession:
        """更新會話內容"""
        session = self.get_session(user_id)
        session.messages.append({"role": role, "content": message})
        session.last_time = time.time()
        self.store.sessions[user_id] = session
        return session

class APIHandler:
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10)
    )
    def call_openai(self, messages: List[Dict]) -> str:
        """調用OpenAI API並實現重試機制"""
        try:
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"OpenAI API error: {e}")
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10)
    )
    def call_dify(self, question: str, user_id: str) -> str:
        """調用Dify API並實現重試機制"""
        headers = {
            "Authorization": f"Bearer {DIFY_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "inputs": {"Question": question},
            "response_mode": "blocking",
            "user": user_id
        }
        
        try:
            response = requests.post(DIFY_API_URL, json=payload, headers=headers)
            response.raise_for_status()
            result = response.json()
            
            if result["data"]["status"] == "succeeded":
                return result["data"]["outputs"].get("text", "未找到相關結果。")
            return f"搜尋失敗：{result['data'].get('error', '未知錯誤')}"
        except requests.RequestException as e:
            logger.error(f"Dify API error: {e}")
            raise

class RateLimiter:
    def __init__(self, store: MemoryStore):
        self.store = store

    def is_rate_limited(self, user_id: str) -> bool:
        """檢查用戶是否超過速率限制"""
        current_time = time.time()
        self.store.rate_limits[user_id].append(current_time)
        
        # 清理超過1分鐘的請求記錄
        while (self.store.rate_limits[user_id] and 
               current_time - self.store.rate_limits[user_id][0] > 60):
            self.store.rate_limits[user_id].popleft()
            
        return len(self.store.rate_limits[user_id]) > MAX_REQUESTS_PER_MINUTE

class SearchHandler:
    def __init__(self, store: MemoryStore, conversation_manager: ConversationManager):
        self.store = store
        self.api_handler = APIHandler()
        self.conversation_manager = conversation_manager

    def process_search(self, user_id: str, search_query: str):
        """處理搜索請求並提供進度更新"""
        try:
            # 發送初始等待消息
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text="開始搜尋相關法律資訊...")
            )
            
            time.sleep(2)  # 初始延遲
            
            # 發送進度更新
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text="正在分析相關法規及判例...")
            )
            
            # 調用 Dify API
            search_result = self.api_handler.call_dify(search_query, user_id)
            
            time.sleep(2)  # 處理延遲
            
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text="正在整理搜尋結果...")
            )
            
            # 更新會話內容
            session = self.conversation_manager.get_session(user_id)
            session.messages = [SYSTEM_PROMPT]
            self.conversation_manager.update_session(
                user_id,
                f"[SEARCH_RESULT] {search_result}",
                "system"
            )
            
            time.sleep(2)  # 最終延遲
            
            # 生成 AI 回應
            reply_text = self.api_handler.call_openai(session.messages)
            self.conversation_manager.update_session(user_id, reply_text, "assistant")
            
            # 發送最終結果
            final_message = TextSendMessage(
                text=reply_text,
                quick_reply=self._create_quick_reply_buttons()
            )
            line_bot_api.push_message(user_id, final_message)
            
        except Exception as e:
            logger.error(f"Search processing error: {e}")
            error_message = "抱歉，搜尋過程發生錯誤，請稍後重試。"
            line_bot_api.push_message(
                user_id,
                TextSendMessage(
                    text=error_message,
                    quick_reply=self._create_quick_reply_buttons()
                )
            )

    def _create_quick_reply_buttons(self):
        """創建快速回覆按鈕"""
        return QuickReply(items=[
            QuickReplyButton(
                action=MessageAction(label="開始新對話", text="開始新對話")
            ),
            QuickReplyButton(
                action=MessageAction(label="繼續對話", text="繼續對話")
            ),
            QuickReplyButton(
                action=MessageAction(label="搜尋資料庫", text="我想要搜尋資料庫")
            )
        ])

class LINEBotHandler:
    def __init__(self, store: MemoryStore):
        self.store = store
        self.conversation_manager = ConversationManager(store)
        self.api_handler = APIHandler()
        self.rate_limiter = RateLimiter(store)
        self.search_handler = SearchHandler(store, self.conversation_manager)

    def handle_message(self, event: MessageEvent):
        """處理用戶消息的主要邏輯"""
        start_time = time.time()
        user_id = event.source.user_id
        user_message = event.message.text

        try:
            # 檢查速率限制
            if self.rate_limiter.is_rate_limited(user_id):
                return self._send_rate_limit_message(event)

            # 處理特殊命令
            if user_message.lower() in ["開始新對話", "我想要搜尋資料庫"]:
                return self._handle_special_command(event, user_message)

            # 更新會話並獲取回應
            session = self.conversation_manager.update_session(user_id, user_message, "user")
            reply_text = self._get_ai_response(session)

            # 處理搜索請求
            if "[SEARCH]" in reply_text:
                return self._handle_search_request(event, reply_text, user_id)

            # 發送回應
            self._send_response(event, reply_text)

        except Exception as e:
            logger.error(f"Error handling message: {e}")
            self._send_error_message(event)

    def _send_rate_limit_message(self, event):
        """發送速率限制提示"""
        message = TextSendMessage(text="您的請求次數過多，請稍後再試。")
        line_bot_api.reply_message(event.reply_token, message)

    def _handle_special_command(self, event, command):
        """處理特殊命令"""
        if command.lower() == "開始新對話":
            reply_text = "已開始新的對話！請輸入您的法律問題。"
        else:
            reply_text = "您希望搜尋什麼資料？"
        
        message = TextSendMessage(
            text=reply_text,
            quick_reply=self._create_quick_reply_buttons()
        )
        line_bot_api.reply_message(event.reply_token, message)

    def _get_ai_response(self, session: UserSession) -> str:
        """獲取AI回應"""
        return self.api_handler.call_openai(session.messages)

    def _handle_search_request(self, event, reply_text, user_id):
        """處理搜索請求"""
        search_query = reply_text.split("[SEARCH]")[1].strip()
        initial_response = "我會協助您搜尋相關法律資訊，這可能需要幾秒鐘的時間..."
        line_bot_api.reply_message(
            event.reply_token, 
            TextSendMessage(text=initial_response)
        )
        
        threading.Thread(
            target=self.search_handler.process_search,
            args=(user_id, search_query)
        ).start()

    def _send_response(self, event, reply_text):
        """發送回應消息"""
        message = TextSendMessage(
            text=reply_text,
            quick_reply=self._create_quick_reply_buttons()
        )
        line_bot_api.reply_message(event.reply_token, message)

    def _send_error_message(self, event):
        """發送錯誤消息"""
        message = TextSendMessage(
            text="抱歉，處理您的請求時發生錯誤。請稍後重試。",
            quick_reply=self._create_quick_reply_buttons()
        )
        line_bot_api.reply_message(event.reply_token, message)

    def _create_quick_reply_buttons(self):
        """創建快速回覆按鈕"""
        return QuickReply(items=[
            QuickReplyButton(
                action=MessageAction(label="開始新對話", text="開始新對話")
            ),
            QuickReplyButton(
                action=MessageAction(label="繼續對話", text="繼續對話")
            ),
            QuickReplyButton(
                action=MessageAction(label="搜尋資料庫", text="我想要搜尋資料庫")
            )
        ])

# 初始化全域變數
memory_store = MemoryStore()
line_bot_handler = LINEBotHandler(memory_store)

# Flask routes
@app.route("/callback", methods=['POST'])
def callback():
    """處理LINE Webhook回調"""
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """處理文字消息"""
    line_bot_handler.handle_message(event)

if __name__ == "__main__":
    # 設定 logger
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger(__name__)
    
    # 定期清理過期的會話
    def cleanup_sessions():
        while True:
            try:
                memory_store.cleanup_old_sessions()
                time.sleep(300)  # 每5分鐘清理一次
            except Exception as e:
                logger.error(f"Session cleanup error: {e}")
    
    # 啟動清理執行緒
    cleanup_thread = threading.Thread(target=cleanup_sessions, daemon=True)
    cleanup_thread.start()
    
    # 啟動 Flask 應用
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)