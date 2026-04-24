import httpx
from nonebot import on_message, get_driver, logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, GroupMessageEvent
from nonebot.rule import to_me

# ---------- 读取配置 ----------
config = get_driver().config

admin_raw = getattr(config, "admin_qq_list", None)
if admin_raw is None:
    admin_raw = getattr(config, "ADMIN_QQ_LIST", None)  # 兼容大写

# 简单转换为整数（若为字符串）
if admin_raw is not None:
    try:
        admin_qq = int(admin_raw)
    except (ValueError, TypeError):
        admin_qq = None
else:
    admin_qq = None

superusers = [admin_qq] if admin_qq else []  # 保持列表格式，便于后续判断
logger.info(f"AI 插件已加载，管理员 QQ: {admin_qq}")

# 其他配置
LONGCAT_API_KEY = getattr(config, "longcat_api_key", None)
LONGCAT_URL = "https://api.longcat.chat/openai/v1/chat/completions"
MAX_REPLY_LENGTH = getattr(config, "max_reply_length", 200)

if not LONGCAT_API_KEY:
    logger.warning("警告：未配置 longcat_api_key，AI 对话插件将不会工作")

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
        logger.debug(f"群聊消息，user_id={event.user_id}, admin_qq={admin_qq}, is_owner={is_owner}")
    else:
        is_owner = event.user_id == admin_qq
        logger.debug(f"私聊消息，user_id={event.user_id}, admin_qq={admin_qq}, is_owner={is_owner}")


    if is_owner:
        system_prompt = base_prompt + "现在和你对话的是你的主人诺亚，请用更亲昵、撒娇的语气回应他喵。"
    else:
        system_prompt = base_prompt + "现在和你对话的是普通朋友，请保持礼貌但依然可爱的语气喵。"

    # ---------- 调用 API ----------
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

    # 字数限制
    if len(reply) > MAX_REPLY_LENGTH:
        reply = reply[:MAX_REPLY_LENGTH] + "…"

    # ---------- 群聊添加前缀 ----------
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

    # 私聊直接发送，不加前缀
    await ai.finish(reply)