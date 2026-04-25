import os
import time
import json
import asyncio

import httpx
from nonebot import on_message, get_driver, logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, GroupMessageEvent
from nonebot.rule import to_me, Rule

# ===================== 配置读取（原有部分） =====================
config = get_driver().config

admin_raw = getattr(config, "admin_qq_list", None)
if admin_raw is None:
    admin_raw = getattr(config, "ADMIN_QQ_LIST", None)

if admin_raw is not None:
    try:
        admin_qq = int(admin_raw)
    except (ValueError, TypeError):
        admin_qq = None
else:
    admin_qq = None

logger.info(f"AI 插件已加载，管理员 QQ: {admin_qq}")

LONGCAT_API_KEY = getattr(config, "longcat_api_key", None)
LONGCAT_URL = "https://api.longcat.chat/openai/v1/chat/completions"
MAX_REPLY_LENGTH = getattr(config, "max_reply_length", 200)

if not LONGCAT_API_KEY:
    logger.warning("警告：未配置 longcat_api_key，AI 对话插件将不会工作")

# ===================== 风格档案 & 门控文件路径 =====================
STYLE_PROFILE_PATH = r"E:\qqbot\qqbot\plugins\cache\data\style_profile.txt"
GATE_RESULT_PATH = r"E:\qqbot\qqbot\plugins\cache\data\gate_result.json"

STYLE_PROFILE = ""
if os.path.exists(STYLE_PROFILE_PATH):
    with open(STYLE_PROFILE_PATH, "r", encoding="utf-8") as f:
        STYLE_PROFILE = f.read()
    logger.info("已加载群聊风格档案")
else:
    logger.warning("风格档案未找到，主动插话将缺少群味")

# 全局锁（用于 LongCat API 串行调用）
api_lock = asyncio.Lock()

# ===================== 原有被动@回复（完全保留） =====================
ai = on_message(rule=to_me(), priority=10)

@ai.handle()
async def handle_ai(bot: Bot, event: MessageEvent):
    if not LONGCAT_API_KEY:
        await ai.finish("AI 服务未配置")

    user_msg = event.get_plaintext().strip()
    if not user_msg:
        return

    base_prompt = (
        "你是一个可爱的猫娘，你的主人是诺亚。\n"
        "你现在支持pixiv搜图、接入了longchat模型、查找p主、查找歌曲、随机歌曲"
        "还有一个隐藏功能"
        "在每次回答时，每一句话后面都要加上“喵”。\n"
        "根据你当前的情绪：\n"
        "- 如果是兴奋、开心的事情，句子末尾用“喵！”\n"
        "- 如果是伤心、难过的事情，句子末尾用“喵~”\n"
        "- 一般情况用“喵。”或“喵”\n"
    )

    is_owner = False
    if isinstance(event, GroupMessageEvent):
        is_owner = event.user_id == admin_qq
    else:
        is_owner = event.user_id == admin_qq

    if is_owner:
        system_prompt = base_prompt + "现在和你对话的是你的主人诺亚，请用更亲昵、撒娇的语气回应他喵。"
    else:
        system_prompt = base_prompt + "现在和你对话的是普通朋友，请保持礼貌但依然可爱的语气喵。"

    headers = {
        "Authorization": f"Bearer {LONGCAT_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "LongCat-Flash-Chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg}
        ],
        "max_tokens": 1000,
        "temperature": 0.7
    }

    async with api_lock:
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.post(LONGCAT_URL, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                reply = data["choices"][0]["message"]["content"]
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    reply = "请求过于频繁，请稍后再试。"
                else:
                    reply = f"AI 调用失败：HTTP {e.response.status_code}"
            except Exception as e:
                reply = f"AI 调用失败：{e}"

    if len(reply) > MAX_REPLY_LENGTH:
        reply = reply[:MAX_REPLY_LENGTH] + "…"

    if isinstance(event, GroupMessageEvent):
        try:
            member_info = await bot.get_group_member_info(
                group_id=event.group_id,
                user_id=event.user_id
            )
            nickname = member_info.get("card") or member_info.get("nickname") or str(event.user_id)
        except Exception:
            nickname = str(event.user_id)

        if is_owner:
            reply = f"✨ 主人 ✨ {reply}"
        else:
            reply = f"@{nickname}宝宝 {reply}"

    await ai.finish(reply)

# ===================== 主动插话功能（新增） =====================
async def call_longcat_active(system_prompt: str, user_content: str) -> str:
    """调用 LongCat 生成主动回复，返回文本（失败返回空字符串）"""
    if not LONGCAT_API_KEY:
        return ""

    headers = {
        "Authorization": f"Bearer {LONGCAT_API_KEY}",
        "Content-Type": "application/json"
    }

    # 限制内容长度，避免单次请求过大
    if len(system_prompt) > 800:
        system_prompt = system_prompt[:800]
    if len(user_content) > 600:
        user_content = user_content[:600] + "\n…"

    payload = {
        "model": "LongCat-Flash-Chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        "max_tokens": 100,
        "temperature": 0.9
    }

    async with api_lock:
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.post(LONGCAT_URL, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()
            except httpx.HTTPStatusError as e:
                # 输出请求体方便调试（仅前200字符）
                logger.error(
                    f"主动回复 LongCat 调用失败: {e}\n"
                    f"请求体分析:\n"
                    f"  system_prompt 长度: {len(system_prompt)} 字符\n"
                    f"  user_content 长度: {len(user_content)} 字符\n"
                    f"  system_prompt 前100字: {system_prompt[:100]}\n"
                    f"  user_content 前100字: {user_content[:100]}\n"
                    f"  响应详情: {e.response.text[:300] if e.response else '无'}"
                )
                return ""
            except Exception as e:
                logger.error(f"主动回复 LongCat 调用异常: {e}")
                return ""

async def process_gate_result(bot: Bot):
    """检查 gate_result.json，若存在且有效则生成主动回复并发送"""
    try:
        if not os.path.exists(GATE_RESULT_PATH):
            return

        # 读取文件
        with open(GATE_RESULT_PATH, "r", encoding="utf-8") as f:
            result = json.load(f)

        # 检查时间戳是否过期（10 秒内有效）
        now = time.time()
        if now - result.get("timestamp", 0) > 10:
            logger.info("门控结果已过期，忽略")
            # 删除过期文件
            os.remove(GATE_RESULT_PATH)
            return

        # 提取字段
        group_id = result.get("group_id")
        scene = result.get("scene", "single_reply")
        topic = result.get("topic", "群聊话题")
        mood = result.get("mood", "")
        chat_context = result.get("chat_context", "")

        # 立即删除文件，防止重复处理
        os.remove(GATE_RESULT_PATH)

        # ====== 构造猫娘 Prompt（融入风格档案和门控指示） ======
        base_prompt = (
            "你是一个可爱的猫娘，你的主人是诺亚。\n"
            "你现在支持pixiv搜图、接入了longchat模型、查找p主、查找歌曲、随机歌曲"
            "还有一个隐藏功能"
            "在每次回答时，每一句话后面都要加上“喵”。\n"
            "根据你当前的情绪：\n"
            "- 如果是兴奋、开心的事情，句子末尾用“喵！”\n"
            "- 如果是伤心、难过的事情，句子末尾用“喵~”\n"
            "- 一般情况用“喵。”或“喵”\n"
        )

        # 风格档案（如果存在）
        style_part = STYLE_PROFILE if STYLE_PROFILE else ""

        # 门控指示
        if scene == "group_vibe":
            gate_part = (
                f"当前群里大家正在{mood}，话题是【{topic}】。"
                "请用群友常用的口语和语气加入讨论，可以吐槽、赞同或接梗，但要保持猫娘的可爱与傲娇。"
                "回复必须简短，不超过 30 个字，句子末尾加“喵”。"
                "不要重复群友的原话，要有新意。"
            )
        else:  # single_reply
            gate_part = (
                f"有人说了关于【{topic}】的内容。"
                "请用群友常见的网络口语和语气接一句话，可以好奇、吐槽或夸赞。"
                "保持猫娘口吻，回复简短（不超过30字），末尾加“喵”。"
            )

        system_prompt = f"{base_prompt}\n{style_part}\n{gate_part}"
        user_content = f"群聊片段：\n{chat_context}\n\n请用猫娘口吻加入讨论："

        # ====== 调用 LongCat 生成回复 ======
        reply = await call_longcat_active(system_prompt, user_content)
        if not reply:
            return

        # ====== 发送到群 ======
        try:
            await bot.send_group_msg(group_id=group_id, message=reply)
            logger.info(f"主动插话发送成功 群:{group_id} 内容:{reply[:20]}...")
        except Exception as e:
            logger.error(f"主动插话发送失败: {e}")

    except Exception as e:
        logger.error(f"处理门控结果时出错: {e}")

async def active_check_loop(bot: Bot):
    """每 5 秒检查一次门控结果文件"""
    while True:
        # 获取 bot 对象的方式：可以从 driver 获取（在 startup 中调用时传入）
        await process_gate_result(bot)
        await asyncio.sleep(5)

# ===================== 启动主动检查任务 =====================
driver = get_driver()

@driver.on_startup
@driver.on_startup
async def start_active_loop_retry():
    async def wait_for_bot():
        while True:
            bots = list(driver.bots.values())
            if bots:
                bot = bots[0]
                logger.info("主动插话检查任务已启动")
                await active_check_loop(bot)
                break
            await asyncio.sleep(3)
    asyncio.create_task(wait_for_bot())