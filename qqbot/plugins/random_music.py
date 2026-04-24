import httpx
import re
import traceback
from urllib.parse import urlparse
from nonebot import on_command, get_driver, logger
from nonebot.adapters.onebot.v11 import Bot, Event
from nonebot.exception import FinishedException
from lxml import html

# ---------- 配置 ----------
config = get_driver().config
LONGCAT_API_KEY = getattr(config, "longcat_api_key", None)
LONGCAT_URL = "https://api.longcat.chat/openai/v1/chat/completions"
MAX_REPLY_LENGTH = getattr(config, "max_reply_length", 500)

if not LONGCAT_API_KEY:
    logger.warning("⚠️ random_song 插件未配置 longcat_api_key，将无法工作")

# ---------- 命令定义 ----------
random_cmd = on_command("随机歌曲", aliases={"随机歌曲"}, priority=1, block=True)

# ---------- 辅助函数 ----------
async def fetch_vocawiki_random_text() -> str:
    """
    获取 voca.wiki 随机歌曲页面的文本（前 2500 字符）及外部视频链接，
    返回拼接后的文本。
    """
    random_url = "https://voca.wiki/index.php?title=Special:Random"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get(random_url, headers=headers)
        resp.raise_for_status()
        tree = html.fromstring(resp.text)

        # 提取标题（歌曲名）
        title_elem = tree.xpath('//h1[@id="firstHeading"]')
        title = title_elem[0].text_content().strip() if title_elem else "未知"

        # 获取主要内容区域
        content_div = tree.xpath('//div[@id="mw-content-text"]')
        if not content_div:
            body = tree.xpath('//body')
            content = body[0] if body else None
        else:
            content = content_div[0]

        if content is None:
            return ""

        # 移除脚本、样式等干扰元素
        for tag in content.xpath('.//script|.//style|.//nav|.//header|.//footer'):
            if tag.getparent() is not None:
                tag.getparent().remove(tag)

        # 提取纯文本
        text = content.text_content()
        text = re.sub(r'\s+', ' ', text).strip()

        # ---------- 链接提取（基于域名，确保是真正的视频平台）----------
        links = {"niconico": None, "youtube": None, "bilibili": None}
        for a in tree.xpath('//a[@href]'):
            href = a.get('href', '')
            if not href.startswith(('http://', 'https://')):
                continue
            parsed = urlparse(href)
            domain = parsed.netloc.lower()
            if 'nicovideo' in domain or 'niconico' in domain:
                if links["niconico"] is None:
                    links["niconico"] = href
            elif 'youtube' in domain:
                if links["youtube"] is None:
                    links["youtube"] = href
            elif 'bilibili' in domain:
                if links["bilibili"] is None:
                    links["bilibili"] = href
        # ----------------------------------------------------------------

        # 组合文本（标题 + 正文前 2500 字符 + 链接）
        full_text = f"歌曲名：{title}\n\n{text[:2500]}"
        link_lines = []
        for platform, url in links.items():
            if url:
                link_lines.append(f"{platform}：{url}")
        if link_lines:
            full_text += "\n\n链接：\n" + "\n".join(link_lines)

        return full_text

async def ask_longcat_for_song(page_text: str) -> str:
    """调用 LongCat AI，用猫娘风格总结随机歌曲信息"""
    system_prompt = (
        "你是一个可爱的猫娘，你的主人是诺亚。\n"
        "在每次回答时，每一句话后面都要加上“喵”。\n"
        "根据你当前的情绪：\n"
        "- 如果是兴奋、开心的事情，句子末尾用“喵！”\n"
        "- 如果是伤心、难过的事情，句子末尾用“喵~”\n"
        "- 一般情况用“喵。”或“喵”\n"
        "现在需要你根据提供的网页内容，为用户随机推荐的歌曲总结信息（包括曲名、P主、投稿日期、简介、链接等）。\n"
        "回答要简洁清晰，文字部分尽量在300字以内，如果页面内容不够，可以说自己还不清楚喵。"
    )
    user_prompt = (
        f"以下是 voca.wiki 随机歌曲页面的内容：\n\n{page_text}\n\n"
        "请帮我总结出这首歌曲的信息喵。"
    )

    headers = {
        "Authorization": f"Bearer {LONGCAT_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "LongCat-Flash-Chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": 500,
        "temperature": 0.7
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(LONGCAT_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

# ---------- 命令处理 ----------
@random_cmd.handle()
async def handle_random(bot: Bot, event: Event):
    if not LONGCAT_API_KEY:
        await random_cmd.finish("AI 服务未配置喵~")
        return

    try:
        # 1. 获取随机歌曲页面文本
        page_text = await fetch_vocawiki_random_text()
        if not page_text:
            await random_cmd.finish("喵~ 没能抓到随机歌曲的页面，稍后再试试喵。")
            return

        # 2. 调用 AI 生成总结
        reply = await ask_longcat_for_song(page_text)
        if len(reply) > MAX_REPLY_LENGTH:
            reply = reply[:MAX_REPLY_LENGTH] + "…"

        await random_cmd.finish(reply)
        return

    except httpx.HTTPStatusError as e:
        await random_cmd.finish(f"喵~ 抓取随机歌曲时出错了（HTTP {e.response.status_code}），稍后再试试喵。")
        return
    except (httpx.RequestError, httpx.TimeoutException, httpx.ConnectError) as e:
        logger.error(f"随机歌曲网络错误: {e}\n{traceback.format_exc()}")
        await random_cmd.finish("喵~ 网络连接异常，请稍后再试喵。")
        return
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"随机歌曲未知错误: {e}\n{traceback.format_exc()}")
        await random_cmd.finish("喵~ 查询时发生了意外错误，请稍后再试喵。")
        return