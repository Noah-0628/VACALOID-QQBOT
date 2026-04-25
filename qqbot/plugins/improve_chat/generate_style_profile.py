# generate_style_profile_ai.py
import sqlite3
import os
import time
import requests
from nonebot import logger  # 使用 nonebot 的日志

# ===================== 配置 =====================
DB_PATH = r"E:\qqbot\qqbot\plugins\cache\data\chat_history.db"
OUTPUT_PATH = r"E:\qqbot\qqbot\plugins\cache\data\style_profile.txt"
ZHIPU_API_KEY = "70f047de5f7f4fb8b8f9bf45cf9d4f31.bSFwkMbWvnN6kWOc"  # 你的智谱密钥
ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
ZHIPU_MODEL = "GLM-4-Flash"

SYSTEM_PROMPT = (
    "你是一个群聊风格分析专家。你会收到一段群聊记录（每条一行）。\n"
    "请从中提取这个群的说话风格，包括：\n"
    "1. 高频出现的、有完整意思的口语/梗（不要切割成无意义的碎片，例如直接保留'老冯飞了'，而不是拆成'老冯'和'飞了'）。\n"
    "2. 常见的结尾语气词（如了、啊、呢、罢了、喵等）。\n"
    "3. 句式习惯（爱用反问、直白吐槽、语气夸张等）。\n"
    "4. 整体说话节奏（短促、碎片化、长句等）。\n\n"
    "请根据该群的说话风格，模仿生成 3 句最具代表性的群聊语句。要求："
    "每句独立成行，以 “- ” 开头。\n"
    "必须使用群内出现过的高频词或梗，但不得直接复制真实的聊天记录。\n"
    "语气、句长、吐槽方式要与该群高度一致。\n"
    "请用以下格式输出（不要额外解释）：\n"
    "本群日常聊天风格：\n"
    "- 高频词汇：XXX、XXX、XXX\n"
    "- 常见结尾语气词：XXX、XXX\n"
    "- 说话习惯：XXX\n"
    "- 整体节奏：XXX"
    "- 模拟的拟人语气"
)

# ===================== 核心函数 =====================
def load_recent_messages(db_path: str, days: int = 7, limit: int = 200) -> list:
    """读取最近 days 天内的最近 limit 条消息内容"""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        since = time.time() - days * 86400
        cursor.execute(
            "SELECT content FROM messages WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT ?",
            (since, limit)
        )
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows if row[0].strip()]
    except Exception as e:
        logger.error(f"读取数据库失败: {e}")
        return []

def call_zhipu(messages: list) -> str:
    """调用智谱 GLM-4.7-Flash 同步请求，返回生成的文本"""
    headers = {
        "Authorization": f"Bearer {ZHIPU_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": ZHIPU_MODEL,
        "messages": messages,
        "max_tokens": 500,
        "temperature": 0.3
    }
    try:
        resp = requests.post(ZHIPU_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except requests.exceptions.RequestException as e:
        logger.error(f"智谱 API 请求失败: {e}")
        return ""
    except KeyError:
        logger.error(f"智谱 API 返回异常: {resp.text}")
        return ""

def generate_style():
    """主逻辑：提取风格并写入 style_profile.txt"""
    logger.info("开始生成群聊风格档案...")
    texts = load_recent_messages(DB_PATH, days=7, limit=200)
    if len(texts) < 5:
        logger.warning("消息太少（<5条），无法生成风格档案")
        return

    # 拼接消息，限制总长度避免 token 爆炸
    chat_text = "\n".join(texts[:200])
    if len(chat_text) > 6000:
        chat_text = chat_text[:6000]

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"以下是群聊记录：\n{chat_text}"}
    ]

    result = call_zhipu(messages)
    if not result:
        logger.error("调用智谱 API 失败，风格档案生成中断")
        return

    # 确保输出目录存在
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(result)

    logger.info(f"风格档案已保存至 {OUTPUT_PATH}")
    logger.info(f"生成内容:\n{result}")

# 支持直接运行此脚本
if __name__ == "__main__":
    generate_style()