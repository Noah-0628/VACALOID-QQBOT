import os
import time
import shutil
import random
import asyncio
import httpx
import json
import uuid

from nonebot import on_message, on_command, get_driver, logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, GroupMessageEvent, PrivateMessageEvent, MessageSegment
from nonebot.rule import to_me
from nonebot.exception import FinishedException
from pixivpy3 import AppPixivAPI

# ---------- 读取配置 ----------
config = get_driver().config

PIXIV_REFRESH_TOKEN = getattr(config, "pixiv_refresh_token", None)
if not PIXIV_REFRESH_TOKEN:
    logger.warning("未配置 PIXIV_REFRESH_TOKEN，Pixiv 插件将无法工作")

# 管理员 QQ
admin_raw = getattr(config, "pixiv_admin_qq", None)
if admin_raw is not None:
    try:
        PIXIV_ADMIN_QQ = int(admin_raw)
    except (ValueError, TypeError):
        PIXIV_ADMIN_QQ = None
else:
    PIXIV_ADMIN_QQ = None

# NapCat 临时目录
NAPCAT_TEMP_DIR = getattr(config, "napcat_temp_dir", r"D:\QQFiles\NapCat\temp")
if not os.path.exists(NAPCAT_TEMP_DIR):
    os.makedirs(NAPCAT_TEMP_DIR, exist_ok=True)

COOLDOWN_SECONDS = getattr(config, "pixiv_cooldown_seconds", 45)
RECALL_SECONDS = getattr(config, "pixiv_recall_seconds", 45)

# 白名单用户
whitelist_raw = getattr(config, "pixiv_whitelist_users", [])
if whitelist_raw is None:
    whitelist_raw = []
if isinstance(whitelist_raw, int):
    whitelist_raw = [whitelist_raw]
elif isinstance(whitelist_raw, str):
    whitelist_raw = [int(uid.strip()) for uid in whitelist_raw.split(",") if uid.strip().isdigit()]
elif isinstance(whitelist_raw, (list, tuple)):
    whitelist_raw = [int(uid) for uid in whitelist_raw if uid is not None]
WHITELIST_USERS = set(whitelist_raw)

# 缓存目录
PLUGIN_ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(PLUGIN_ROOT, "cache", "pixiv_download")
os.makedirs(CACHE_DIR, exist_ok=True)

TOKEN_PATH = os.path.join(PLUGIN_ROOT, "cache", "pixiv_token.json")
os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)

# ---------- 初始化 Pixiv API ----------
api = AppPixivAPI()
if PIXIV_REFRESH_TOKEN:
    try:
        api.auth(refresh_token=PIXIV_REFRESH_TOKEN)
        logger.info("Pixiv API 初始化成功")
    except Exception as e:
        logger.error(f"Pixiv API 初始化失败: {e}")

# ---------- 辅助函数 ----------
def save_access_token(token: str):
    try:
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            json.dump({"access_token": token, "timestamp": int(time.time())}, f)
        logger.debug(f"access_token 已保存: {TOKEN_PATH}")
    except Exception as e:
        logger.error(f"保存 access_token 失败: {e}")

cooldowns = {}

async def check_cooldown(bot: Bot, event: MessageEvent) -> bool:
    uid = event.user_id
    if uid in WHITELIST_USERS:
        await bot.send(event, f"主人！已为您跳过冷却机制~ 👑")
        return True

    now = time.time()
    if uid in cooldowns and now - cooldowns[uid] < COOLDOWN_SECONDS:
        remaining = int(COOLDOWN_SECONDS - (now - cooldowns[uid]))
        await bot.send(event, f"宝宝你要稍等一下，请等待 {remaining} 秒后再试。")
        return False

    cooldowns[uid] = now
    return True

def is_sensitive(illust) -> bool:
    if illust.sanity_level >= 6:
        return True

    sensitive_tags = {
        "肢体切断", "重口", "兽交", "血腥", "血", "gore",
        "流血", "肢体切断", "猎奇", "内脏", "分尸",
        "残肢", "暴力", "病娇", "SM", "拷问", "刑具",
        "guro", "断肢", "兽交", "异种奸", "触手", "怪物娘", "兽人", "非人", "異種姦",
        "催眠", "洗脑", "药物", "猥亵", "非自愿", "偷拍",
        "虫子", "昆虫娘", "寄生", "便器", "排泄", "孕"
    }

    tag_names = set()
    if illust.tags:
        try:
            tag_names = {t.name for t in illust.tags if t and hasattr(t, "name")}
        except Exception:
            pass

    return not sensitive_tags.isdisjoint(tag_names)

async def send_images(bot: Bot, event: MessageEvent, user_id: str, illusts: list):
    user_dir = os.path.join(CACHE_DIR, user_id)
    os.makedirs(user_dir, exist_ok=True)

    max_retry = 5
    retry_delay = 1
    sent_temp_paths = []

    for attempt in range(1, max_retry + 1):
        messages = []
        message_ids = []

        async with httpx.AsyncClient() as client:
            for illust in illusts:
                url = (
                    illust.meta_single_page.get("original_image_url")
                    if illust.meta_single_page else None
                )
                if not url and illust.meta_pages:
                    url = illust.meta_pages[0].image_urls.get("original")
                if not url and illust.image_urls:
                    url = (
                        illust.image_urls.get("original")
                        or illust.image_urls.get("large")
                        or illust.image_urls.get("medium")
                    )
                if not url:
                    logger.warning(f"插图 {illust.id} 无可用图片链接，已跳过。")
                    continue

                try:
                    r = await client.get(url, headers={"Referer": "https://www.pixiv.net/"}, timeout=15)
                    content_type = r.headers.get("Content-Type", "")
                    if "jpeg" in content_type:
                        ext = ".jpg"
                    elif "png" in content_type:
                        ext = ".png"
                    elif "gif" in content_type:
                        ext = ".gif"
                    else:
                        logger.warning(f"非法格式: {content_type} 跳过")
                        continue

                    filename = f"{illust.id}{ext}"
                    path = os.path.join(user_dir, filename)
                    with open(path, "wb") as f:
                        f.write(r.content)

                    temp_filename = f"{uuid.uuid4().hex}{ext}"
                    temp_path = os.path.join(NAPCAT_TEMP_DIR, temp_filename)
                    shutil.copy(path, temp_path)
                    sent_temp_paths.append(temp_path)

                    messages.append(MessageSegment.image(f"file://{temp_path}"))
                except Exception as e:
                    logger.error(f"下载失败: {e}")
                    continue

        if messages:
            await bot.send(event=event, message=f"宝宝已为你找到 {len(illusts)} 张插图，{RECALL_SECONDS} 秒后将撤回。")

            for msg in messages:
                try:
                    result = await bot.send(event=event, message=msg)
                    if isinstance(result, dict) and "message_id" in result:
                        message_ids.append(result["message_id"])
                except Exception as e:
                    logger.error(f"图片发送失败：{e}")

            # 仅在群聊中创建撤回任务（私聊无法撤回）
            if isinstance(event, GroupMessageEvent):
                async def recall_and_cleanup():
                    await asyncio.sleep(RECALL_SECONDS)
                    for mid in message_ids:
                        try:
                            await bot.call_api("delete_msg", message_id=mid)
                            logger.debug(f"已撤回消息 {mid}")
                        except Exception as e:
                            logger.error(f"撤回失败：{e}")
                    for temp_path in sent_temp_paths:
                        try:
                            if os.path.exists(temp_path):
                                os.remove(temp_path)
                        except Exception:
                            pass
                asyncio.create_task(recall_and_cleanup())

            break
        else:
            logger.warning(f"第 {attempt}/{max_retry} 次尝试失败，准备重试...")
            await asyncio.sleep(retry_delay)
    else:
        await bot.send(event=event, message="❌ 图片全部发送失败，Pixiv 图片格式异常或网络错误")

    await asyncio.sleep(1)
    shutil.rmtree(user_dir, ignore_errors=True)

async def periodic_token_refresh():
    while True:
        await asyncio.sleep(25 * 60)
        try:
            if PIXIV_REFRESH_TOKEN:
                api.auth(refresh_token=PIXIV_REFRESH_TOKEN)
                save_access_token(api.access_token)
                logger.debug("定时刷新 access_token 成功")
        except Exception as e:
            logger.error(f"定时刷新失败: {e}")

driver = get_driver()

@driver.on_startup
async def on_startup():
    try:
        if PIXIV_REFRESH_TOKEN:
            api.auth(refresh_token=PIXIV_REFRESH_TOKEN)
            save_access_token(api.access_token)
            logger.info("Pixiv 插件启动时 access_token 写入成功")
    except Exception as e:
        logger.error(f"Pixiv 插件启动时刷新 token 失败: {e}")
    asyncio.create_task(periodic_token_refresh())

# ---------- 命令定义 ----------
# 修改点1：帮助命令规则改为 MessageEvent（不再限制群聊）
def pixiv_help_rule(event: MessageEvent) -> bool:
    return event.get_plaintext().strip().lower() == ".pixiv help"

pixiv_help = on_message(rule=pixiv_help_rule, priority=1, block=True)

@pixiv_help.handle()
async def handle_pixiv_help(bot: Bot, event: MessageEvent):
    help_text = (
        "🎨 Pixiv 插图指令帮助\n\n"
        "📥 插图获取示例(支持默认、day、week、month)：\n"
        ".pixiv hot miku\n"
        "↑ 获取 Pixiv 最热门关键词为“miku”的1张图（默认）\n\n"
        ".pixiv hot miku 3\n"
        "↑ 获取 Pixiv 最热门关键词为“miku”的3张图\n\n"
        ".pixiv id 12345678\n"
        "↑ 获取 Pixiv id为“12345678”的图\n\n"
        ".pixiv r\n"
        "↑ 获取 Pixiv 日榜中1张图（默认）\n\n"
        ".pixiv r miku 3\n"
        "↑ 随机获取3张关键词为“miku”的插图（按最新排序）\n\n"
        ".pixiv r miku week 3\n"
        "↑ 获取“miku”关键词在 Pixiv 周榜中的3张插图\n\n"
        ".pixiv r week 2\n"
        "↑ 获取 Pixiv 周榜中2张随机图（不带关键词）\n\n"
        "👤 用户作品获取示例：\n"
        ".pixiv u おむたつ/omutatsu\n"
        "↑ 获取用户 おむたつ/omutatsu 的最新1张作品（默认 latest）\n\n"
        ".pixiv u おむたつ/omutatsu random 2\n"
        "↑ 随机获取用户 おむたつ/omutatsu 的2张作品\n\n"
        ".pixiv u おむたつ/omutatsu latest 3\n"
        "↑ 获取用户 おむたつ/omutatsu 的最新3张作品\n\n"
        "🏷️ 多tag获取示例：\n"
        ".pixiv tag r 百合 黑丝 3\n"
        "↑ 随机获取关键词为“百合 黑丝”的3张图\n\n"
        ".pixiv tag hot 百合 黑丝 3\n"
        "📌 补充说明：\n"
        "• 每人请求有冷却限制（默认20秒）\n"
        "• 插件会自动过滤 R-18 / 敏感内容\n"
        "• 插图将在 60 秒后自动撤回（仅限群聊）\n"
    )
    await bot.send(event=event, message=help_text)

# 修改点2：ID 命令规则改为 MessageEvent
def pixiv_id_rule(event: MessageEvent) -> bool:
    return event.get_plaintext().strip().lower().startswith(".pixiv id")

pixiv_id = on_message(rule=pixiv_id_rule, priority=1, block=True)

@pixiv_id.handle()
async def handle_pixiv_id(bot: Bot, event: MessageEvent):
    if not await check_cooldown(bot, event):
        return
    text = event.get_plaintext().strip()
    parts = text.split()
    if len(parts) < 3 or not parts[2].isdigit():
        await bot.send(event=event, message="❗ 格式错误，应为 `.pixiv id [作品ID]`")
        return
    illust_id = int(parts[2])
    try:
        illust = api.illust_detail(illust_id).illust
        if not illust:
            await bot.send(event=event, message=f"⚠️ 未找到作品 ID 为 {illust_id} 的插图")
            return
        if is_sensitive(illust):
            await bot.send(event=event, message="⚠️ 该作品被判断为敏感内容，已过滤")
            return
        await send_images(bot, event, str(event.user_id), [illust])
    except Exception as e:
        logger.error(f"通过ID获取插图失败: {e}")
        await bot.send(event=event, message=f"❌ 获取插图失败：{e}")

# 修改点3：随机/用户命令规则改为 MessageEvent
def pixiv_rule(event: MessageEvent) -> bool:
    text = event.get_plaintext().strip().lower()
    if text.startswith(".pixiv r18"):
        return False
    return text.startswith(".pixiv r") or text.startswith(".pixiv u")

pixiv_handler = on_message(rule=pixiv_rule, priority=1, block=True)

@pixiv_handler.handle()
async def handle_pixiv_command(bot: Bot, event: MessageEvent):
    uid = event.user_id
    now = time.time()
    text = event.get_plaintext().strip()

    if uid in cooldowns and now - cooldowns[uid] < COOLDOWN_SECONDS:
        await bot.send(event=event, message=f"宝宝需要稍等一下哦，请等待 {int(COOLDOWN_SECONDS - (now - cooldowns[uid]))} 秒后再试。")
        return
    cooldowns[uid] = now

    try:
        if text.startswith(".pixiv r"):
            await handle_pixiv_random(bot, event, text)
        elif text.startswith(".pixiv u"):
            await handle_pixiv_user(bot, event, text)
        else:
            await bot.send(event=event, message="❗ 宝宝不是这样召唤的。使用.pixiv help 查看详情")
    except FinishedException:
        raise
    except Exception as e:
        await bot.send(event=event, message=f"❌ 插件处理失败：{e}")

async def handle_pixiv_random(bot: Bot, event: MessageEvent, text: str):
    parts = text.split()
    tag, num = None, 1
    mode = None
    max_pages = 15

    for part in parts[2:]:
        if part.isdigit():
            num = min(int(part), 6)
        elif part.lower() in {"day", "week", "month"}:
            mode = part.lower()
        else:
            tag = part

    illusts = []
    try:
        if mode and not tag:
            res = api.illust_ranking(mode=mode)
            pages = 0
            while res and res.illusts and pages < max_pages:
                illusts.extend([i for i in res.illusts if not is_sensitive(i)])
                pages += 1
                if res.next_url:
                    next_qs = api.parse_qs(res.next_url)
                    res = api.illust_ranking(**next_qs)
                else:
                    break
        elif tag:
            res = api.search_illust(tag)
            pages = 0
            while res and res.illusts and pages < max_pages:
                illusts.extend([i for i in res.illusts if not is_sensitive(i)])
                pages += 1
                if res.next_url:
                    next_qs = api.parse_qs(res.next_url)
                    res = api.search_illust(**next_qs)
                else:
                    break
        else:
            res = api.illust_ranking("week")
            pages = 0
            while res and res.illusts and pages < max_pages:
                illusts.extend([i for i in res.illusts if not is_sensitive(i)])
                pages += 1
                if res.next_url:
                    next_qs = api.parse_qs(res.next_url)
                    res = api.illust_ranking(**next_qs)
                else:
                    break

        if not illusts:
            await bot.send(event=event, message="⚠️ 未找到相关插图。")
            return

        selected = random.sample(illusts, min(num, len(illusts)))
        await send_images(bot, event, str(event.user_id), selected)

    except Exception as e:
        logger.error(f"随机获取插图失败: {e}")
        await bot.send(event=event, message=f"❌ 获取插图失败：{e}")

async def handle_pixiv_user(bot: Bot, event: MessageEvent, text: str):
    parts = text.strip().split()
    if len(parts) < 3:
        await bot.send(event=event, message="❗ 宝宝不是这样召唤的。使用.pixiv help 查看详情")
        return

    username = parts[2]
    mode = "latest"
    num = 1

    for p in parts[3:]:
        if p.isdigit():
            num = min(int(p), 6)
        elif p.lower() == "random":
            mode = "random"

    try:
        result = api.search_user(username)
        if not result.user_previews:
            await bot.send(event=event, message=f"❌ 未找到用户「{username}」。")
            return

        user = result.user_previews[0].user

        illusts = []
        max_pages = 10
        offset = 0
        seen_ids = set()

        for _ in range(max_pages):
            res = api.user_illusts(user.id, offset=offset)
            page_illusts = [i for i in res.illusts if not is_sensitive(i) and i.id not in seen_ids]
            if not page_illusts:
                break
            illusts.extend(page_illusts)
            seen_ids.update(i.id for i in page_illusts)
            if not res.next_url:
                break
            offset += len(res.illusts)

        if not illusts:
            await bot.send(event=event, message="⚠️ 该用户暂无非 R-18 / 敏感作品。")
            return

        selected = random.sample(illusts, min(num, len(illusts))) if mode == "random" else illusts[:num]
        await send_images(bot, event, str(event.user_id), selected)

    except Exception as e:
        logger.error(f"获取用户作品失败: {e}")
        await bot.send(event=event, message=f"❌ 获取失败：{e}")


def pixiv_hot_rule(event: MessageEvent) -> bool:
    return event.get_plaintext().strip().lower().startswith(".pixiv hot")

pixiv_hot = on_message(rule=pixiv_hot_rule, priority=1, block=True)

@pixiv_hot.handle()
async def handle_pixiv_hot(bot: Bot, event: MessageEvent):
    if not await check_cooldown(bot, event):
        return
    text = event.get_plaintext().strip()
    parts = text.split()
    if len(parts) < 3:
        await bot.send(event=event, message="❗ 格式错误，应为 `.pixiv hot [关键词] [数量]`")
        return

    tag = parts[2]
    try:
        num = int(parts[3]) if len(parts) > 3 else 1
        num = max(1, min(num, 10))
    except ValueError:
        await bot.send(event=event, message="❗ 数量参数无效，应为数字")
        return

    try:
        res = api.search_illust(tag, search_target="partial_match_for_tags", sort="popular_desc")
        illusts = []
        if res and getattr(res, "illusts", None):
            illusts = [i for i in res.illusts if not is_sensitive(i)]
        if not illusts:
            await bot.send(event=event, message="⚠️ 未找到热门插图。")
            return

        selected = random.sample(illusts, min(num, len(illusts)))
        await send_images(bot, event, str(event.user_id), selected)
    except Exception as e:
        logger.error(f"热门插图获取异常：{e}")
        await bot.send(event=event, message=f"❌ 热门插图获取失败：{e}")


def pixiv_tag_rule(event: MessageEvent) -> bool:
    return event.get_plaintext().strip().lower().startswith(".pixiv tag")

pixiv_tag = on_message(rule=pixiv_tag_rule, priority=1, block=True)

@pixiv_tag.handle()
async def handle_pixiv_tag(bot: Bot, event: MessageEvent):
    if not await check_cooldown(bot, event):
        return

    text = event.get_plaintext().strip()
    parts = text.split()

    if len(parts) < 3:
        await bot.send(event=event,
                       message="❗ 格式错误，应为 `.pixiv tag [hot/r] [关键词...] [可选: week/month/day] [数量]`")
        return

    search_type = parts[2].lower()
    valid_modes = {"day", "week", "month"}
    mode = None
    num = 1
    tag_parts = []

    for part in parts[3:]:
        lower_part = part.lower()
        if lower_part in valid_modes:
            mode = lower_part
        elif part.isdigit():
            num = min(int(part), 6)
        else:
            tag_parts.append(part)

    tag = " ".join(tag_parts)

    if not tag:
        await bot.send(event=event, message="❗ 请输入关键词")
        return

    try:
        if search_type == "hot":
            res = api.search_illust(tag, search_target="partial_match_for_tags", sort="popular_desc")
        elif search_type == "r":
            res = api.search_illust(tag, search_target="partial_match_for_tags", sort="date_desc")
        else:
            await bot.send(event=event, message="❗ 类型仅支持 `hot` 或 `r`")
            return

        illusts = []
        max_pages = 10
        pages = 0

        while res and res.illusts and pages < max_pages:
            illusts.extend([i for i in res.illusts if not is_sensitive(i)])
            if res.next_url:
                res = api.search_illust(**api.parse_qs(res.next_url))
            else:
                break
            pages += 1

        if not illusts:
            await bot.send(event=event, message="⚠️ 没有找到符合条件的插图")
            return

        selected = random.sample(illusts, min(num, len(illusts)))
        await send_images(bot, event, str(event.user_id), selected)

    except Exception as e:
        logger.error(f"tag 搜索异常：{e}")
        await bot.send(event=event, message=f"❌ tag 搜索失败：{e}")

# 修改点6：手动刷新 token 命令规则改为 MessageEvent（不再限制私聊）
def refresh_rule(event: MessageEvent) -> bool:
    return event.get_plaintext().strip().lower() == ".pixiv refresh"

pixiv_refresh = on_message(rule=refresh_rule, priority=1, block=True)

@pixiv_refresh.handle()
async def handle_refresh(bot: Bot, event: MessageEvent):
    if PIXIV_ADMIN_QQ is None or event.user_id != PIXIV_ADMIN_QQ:
        await bot.send(event, "❌ 你无权刷新 access_token。")
        return

    try:
        api.auth(refresh_token=PIXIV_REFRESH_TOKEN)
        save_access_token(api.access_token)
        await bot.send(event, "✅ access_token 刷新成功！")
    except Exception as e:
        await bot.send(event, f"❌ 刷新失败: {e}")