import os
import time
import json
import asyncio
from collections import defaultdict
from typing import Dict, List, Tuple, Optional

import httpx
from nonebot import on_message, get_driver, logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.rule import Rule

# ===================== 配置 =====================
config = get_driver().config

# 智谱 API（门控用）
ZHIPU_API_KEY = getattr(config, "zhipu_api_key", None)
ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
ZHIPU_MODEL = "GLM-4-Flash"

# 门控输出文件路径
GATE_RESULT_PATH = r"E:\qqbot\qqbot\plugins\cache\data\gate_result.json"

# 行为参数
BUFFER_MAX_SIZE = 5                # 触发判断所需的缓冲消息数
BUFFER_TIME_WINDOW = 9999            # 缓冲时间窗口（秒）
GROUP_COOLDOWN = 90                # 同一群氛围触发后冷却秒数
SINGLE_COOLDOWN = 90               # 单条接话冷却秒数（若氛围未触发时尝试单条）
SINGLE_CHECK_MIN_INTERVAL = 10     # 两次单条判断最小间隔（秒）
DAILY_LIMIT = 100                  # 每日总主动插话次数上限
DAY_RESET_HOUR = 4                 # 每日重置时间（凌晨4点）

if not ZHIPU_API_KEY:
    logger.warning("gate: 未配置 zhipu_api_key，门控无法工作")

# ===================== 全局状态 =====================
message_buffer: Dict[int, List[Tuple[int, str, float]]] = defaultdict(list)
last_vibe_time: Dict[int, float] = {}
last_single_time: Dict[int, float] = {}
last_single_check_time: Dict[int, float] = {}
daily_active_count = 0
daily_reset_timestamp = 0.0
group_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

# API 锁（智谱免费版1并发）
zhipu_lock = asyncio.Lock()

# ===================== 工具函数 =====================
def _next_reset_time() -> float:
    now = time.time()
    today4am = (int(now // 86400)) * 86400 + DAY_RESET_HOUR * 3600
    if now >= today4am:
        return today4am + 86400
    return today4am

def get_or_reset_daily_count() -> int:
    global daily_active_count, daily_reset_timestamp
    now = time.time()
    if now >= daily_reset_timestamp:
        daily_active_count = 0
        daily_reset_timestamp = _next_reset_time()
        logger.info("gate: 每日主动插话计数已重置")
    return daily_active_count

def format_buffer(buffer: List[Tuple[int, str, float]]) -> str:
    """将缓冲消息格式化为门控分析用的文本"""
    lines = []
    for user_id, msg, _ in buffer:
        lines.append(f"群友{user_id % 1000}: {msg}")
    return "\n".join(lines)

async def call_zhipu(messages: list, max_tokens: int = 100, temperature: float = 0.1) -> str:
    """调用智谱 API，受全局锁保护"""
    if not ZHIPU_API_KEY:
        return ""
    headers = {
        "Authorization": f"Bearer {ZHIPU_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": ZHIPU_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature
    }
    async with zhipu_lock:
        async with httpx.AsyncClient(timeout=30.0) as client:
            for attempt in range(2):
                try:
                    resp = await client.post(ZHIPU_URL, json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    return data["choices"][0]["message"]["content"].strip()
                except Exception as e:
                    logger.error(f"gate: 智谱 API 失败 (尝试 {attempt+1}): {e}")
                    if attempt == 0:
                        await asyncio.sleep(2)
            return ""

def write_gate_result(data: dict):
    """将门控结果写入 JSON 文件，覆盖之前的内容"""
    try:
        os.makedirs(os.path.dirname(GATE_RESULT_PATH), exist_ok=True)
        with open(GATE_RESULT_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        logger.info(f"gate: 已写入门控结果 -> scene={data.get('scene')}, topic={data.get('topic')}")
    except Exception as e:
        logger.error(f"gate: 写入结果文件失败: {e}")

async def judge(chat_history: str) -> Optional[dict]:
    """门控核心判断，返回可解析的字典"""
    system = (
        "你是一个群聊分析助手。你将收到一段群聊记录。\n"
        "请判断：\n"
        "1. 如果收到多条消息（>=2条），这群人是否正围绕同一话题进行情绪化讨论（吐槽/夸赞/玩梗）。\n"
        "   如果是，返回JSON：{\"scene\":\"group_vibe\",\"should_join\":true,\"topic\":\"关键词\",\"mood\":\"吐槽/夸赞/玩梗/其他\"}\n"
        "   如果不是，返回JSON：{\"scene\":\"group_vibe\",\"should_join\":false}\n"
        "2. 如果只收到一条消息，判断是否适合一个可爱的猫娘主动接话（提问/感叹/抛梗/有可吐槽的点）。\n"
        "   适合：{\"scene\":\"single_reply\",\"should_join\":true,\"topic\":\"消息主题\"}\n"
        "   不适合：{\"scene\":\"single_reply\",\"should_join\":false}\n"
        "只返回JSON，不要附加解释。"
    )
    msgs = [
        {"role": "system", "content": system},
        {"role": "user", "content": chat_history}
    ]
    raw = await call_zhipu(msgs, max_tokens=80, temperature=0.1)
    if not raw:
        return None
    try:
        raw = raw.strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1:
            raw = raw[start:end+1]
        return json.loads(raw)
    except Exception:
        logger.error(f"gate: JSON解析失败: {raw}")
        return None

# ===================== 主流程 =====================
async def process_message(bot: Bot, event: GroupMessageEvent):
    group_id = event.group_id
    user_id = event.user_id
    text = event.get_plaintext().strip()

    # 过滤无效消息
    if len(text) < 4 or any(w in text for w in ["[CQ:", "http"]):
        return

    lock = group_locks[group_id]
    if lock.locked():
        return
    async with lock:
        now = time.time()
        buffer = message_buffer[group_id]
        buffer.append((user_id, text, now))

        # 清理过期消息（保留60秒内）
        cutoff = now - 60
        buffer[:] = [(u, m, t) for u, m, t in buffer if t > cutoff]

        # ---------- 氛围跟风判断 ----------
        if len(buffer) >= BUFFER_MAX_SIZE:
            first_time = buffer[0][2]
            if now - first_time <= BUFFER_TIME_WINDOW:
                # 冷却与限额检查
                if group_id in last_vibe_time and (now - last_vibe_time[group_id]) < GROUP_COOLDOWN:
                    pass
                elif get_or_reset_daily_count() >= DAILY_LIMIT:
                    logger.info("gate: 今日主动插话已达上限")
                    return
                else:
                    chat_text = format_buffer(buffer)
                    result = await judge(chat_text)
                    if result and result.get("should_join") and result.get("scene") == "group_vibe":
                        # 写出门控结果
                        write_gate_result({
                            "group_id": group_id,
                            "scene": "group_vibe",
                            "topic": result.get("topic", "当前话题"),
                            "mood": result.get("mood", "讨论"),
                            "chat_context": chat_text,
                            "timestamp": now
                        })
                        # 更新冷却与计数
                        last_vibe_time[group_id] = now
                        global daily_active_count
                        daily_active_count += 1
                        # 清空缓冲，避免重复触发
                        message_buffer[group_id] = []
                        return
                # 无论是否触发，重置缓冲（保留最后2条作为种子）
                message_buffer[group_id] = buffer[-2:]
                return

        # ---------- 单条消息接话 ----------
        last_single = last_single_time.get(group_id, 0)
        last_check = last_single_check_time.get(group_id, 0)
        if (now - last_single) < SINGLE_COOLDOWN or (now - last_check) < SINGLE_CHECK_MIN_INTERVAL:
            return
        last_single_check_time[group_id] = now

        if get_or_reset_daily_count() >= DAILY_LIMIT:
            return

        if buffer:
            last_msg = buffer[-1][1]
            result = await judge(f"群友{user_id % 1000}: {last_msg}")
            if result and result.get("should_join") and result.get("scene") == "single_reply":
                write_gate_result({
                    "group_id": group_id,
                    "scene": "single_reply",
                    "topic": result.get("topic", last_msg[:10]),
                    "chat_context": last_msg,
                    "timestamp": now
                })
                last_single_time[group_id] = now
                daily_active_count += 1
                message_buffer[group_id] = []  # 接话后清空缓冲

# ===================== 事件监听 =====================
async def _not_to_me(event: GroupMessageEvent) -> bool:
    return not event.is_tome()

gate_rule = Rule(_not_to_me)
gate_listener = on_message(rule=gate_rule, priority=5, block=False)

@gate_listener.handle()
async def handle_gate(bot: Bot, event: GroupMessageEvent):
    await process_message(bot, event)