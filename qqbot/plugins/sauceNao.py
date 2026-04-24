import asyncio
import httpx
from nonebot import get_driver, on_command, on_message, get_bot
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, GroupMessageEvent
from nonebot.log import logger
from nonebot.rule import Rule

# ---------- 配置 ----------
config = get_driver().config
SAUCENAO_API_KEY = getattr(config, "saucenao_api_key", None)
SAUCENAO_URL = "https://saucenao.com/search.php"
TIMEOUT = 30
SIMILARITY_THRESHOLD = 70  # 低于此值认为识别度低

if not SAUCENAO_API_KEY:
    logger.warning("主人忘记配置api了，搜图功能无法使用")

# ---------- 状态管理 ----------
waiting_users = {}
TIMEOUT_WAIT = 30

def get_user_key(event: MessageEvent) -> tuple:
    if isinstance(event, GroupMessageEvent):
        return (event.user_id, event.group_id)
    else:
        return (event.user_id, None)

async def remove_waiting(user_key):
    if user_key in waiting_users:
        del waiting_users[user_key]

async def _timeout_remove(user_key, event):
    await asyncio.sleep(TIMEOUT_WAIT)
    if user_key not in waiting_users:
        return
    del waiting_users[user_key]
    try:
        bot = get_bot()
        if isinstance(event, GroupMessageEvent):
            await bot.send_group_msg(
                group_id=event.group_id,
                message=f"[CQ:at,qq={event.user_id}] 识图超时，请重新发送 .识图"
            )
        else:
            await bot.send_private_msg(
                user_id=event.user_id,
                message="识图超时，请重新发送 .识图"
            )
    except Exception:
        pass

# ---------- SauceNAO 搜索 ----------
async def search_saucenao(img_url: str) -> dict:
    """返回 (最高相似度, 格式化字符串, 是否低识别度)"""
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(
                SAUCENAO_URL,
                data={
                    "api_key": SAUCENAO_API_KEY,
                    "url": img_url,
                    "output_type": 2,
                    "numres": 3,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"SauceNAO 请求失败: {e}")
            return (0, f"搜图服务异常：{e}", False)

    results = data.get("results", [])
    if not results:
        return (0, "没有找到相似图片。", False)

    # 提取最高相似度
    max_sim = 0
    reply_lines = []
    for idx, res in enumerate(results[:3], 1):
        header = res.get("header", {})
        data_part = res.get("data", {})
        similarity = float(header.get("similarity", 0))
        max_sim = max(max_sim, similarity)

        title = data_part.get("title", "未知作品")
        author = data_part.get("author", "未知作者")
        ext_urls = data_part.get("ext_urls", [])

        line = f"{idx}. 【{similarity:.1f}%】\n   作品：{title}\n   作者：{author}"
        if ext_urls:
            # 优先显示 Pixiv 链接
            pixiv_link = next((u for u in ext_urls if "pixiv.net" in u), ext_urls[0])
            line += f"\n   链接：{pixiv_link}"
        reply_lines.append(line)

    reply = "🔍 SauceNAO 识图结果：\n" + "\n\n".join(reply_lines)
    low_similarity = max_sim < SIMILARITY_THRESHOLD
    return (max_sim, reply, low_similarity)

# ---------- 命令 ----------
search_cmd = on_command("识图", aliases={"搜图", "saucenao"}, priority=5, block=True)

@search_cmd.handle()
async def handle_search_cmd(bot: Bot, event: MessageEvent):
    if not SAUCENAO_API_KEY:
        await search_cmd.finish("搜图服务未开启，请联系诺亚。")

    user_key = get_user_key(event)
    if user_key in waiting_users:
        await remove_waiting(user_key)

    waiting_users[user_key] = asyncio.create_task(_timeout_remove(user_key, event))
    await search_cmd.send("宝宝请发送一张图片（30秒内有效）")

# ---------- 图片处理 ----------
async def is_waiting_and_has_image(event: MessageEvent) -> bool:
    user_key = get_user_key(event)
    if user_key not in waiting_users:
        return False
    for seg in event.message:
        if seg.type == "image":
            return True
    return False

image_handler = on_message(Rule(is_waiting_and_has_image), priority=10, block=True)

@image_handler.handle()
async def handle_image(bot: Bot, event: MessageEvent):
    user_key = get_user_key(event)

    if user_key in waiting_users:
        waiting_users[user_key].cancel()
        del waiting_users[user_key]

    # 提取图片 URL
    img_url = None
    for seg in event.message:
        if seg.type == "image":
            img_url = seg.data.get("url")
            break

    if not img_url:
        await image_handler.finish("没找到图片呢，请重新发送/识图")

    # 搜索
    max_sim, result_text, low_similarity = await search_saucenao(img_url)

    # 如果识别度低，追加建议
    if low_similarity:
        result_text += "\n\n⚠️ 识别度比较低哦，建议用更清晰的完整图片重新搜索。"
        # 自动提供 Yandex 链接（作为备选）
        yandex_link = f"https://yandex.com/images/search?rpt=imageview&url={img_url}"
        result_text += f"\n🔗 备选：<a href='{yandex_link}'>Yandex 相似图片搜索</a>"

    # 群聊格式化
    if isinstance(event, GroupMessageEvent):
        try:
            member_info = await bot.get_group_member_info(
                group_id=event.group_id,
                user_id=event.user_id
            )
            nickname = member_info.get("card") or member_info.get("nickname") or str(event.user_id)
        except Exception:
            nickname = str(event.user_id)

        superusers = getattr(config, "superusers", [])
        is_owner = str(event.user_id) in [str(uid) for uid in superusers]

        if is_owner:
            result_text = f"✨ 主人 ✨\n{result_text}"
        else:
            result_text = f"@{nickname}宝宝\n{result_text}"

    await image_handler.finish(result_text)