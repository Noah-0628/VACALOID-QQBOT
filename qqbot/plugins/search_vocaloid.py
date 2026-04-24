import httpx
import urllib.parse
import re
import traceback
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
    logger.warning("⚠️ vocaloid_query 插件未配置 longcat_api_key，将无法工作")


song_cmd = on_command("查歌", aliases={"查歌"}, priority=1, block=True)
producer_cmd = on_command("查P主", aliases={"查p主", "查P主"}, priority=1, block=True)


async def fetch_moegirl_page(page_name: str, page_type: str = "auto") -> dict:
    """
    抓取萌娘百科页面，返回包含标题和正文的字典
    page_type: "song", "producer", "auto"
    """
    encoded = urllib.parse.quote(page_name, safe='*')
    url = f"https://zh.moegirl.org.cn/{encoded}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        tree = html.fromstring(resp.text)

        intro_text = ""

        # XPath 定义（根据你之前测试成功的路径）
        producer_intro_xpath = "/html/body/div[1]/div/div/div[1]/main/div/div[1]/article/div[3]/div/div/ul[1]"
        song_intro_xpath = "/html/body/div[1]/div/div/div[1]/main/div/div[1]/article/div[3]/div/div/p[1]"

        if page_type == "producer":
            elements = tree.xpath(producer_intro_xpath)
            if elements:
                ul = elements[0]
                li_texts = ul.xpath('.//li//text()')
                intro_text = ' '.join([t.strip() for t in li_texts if t.strip()])
        elif page_type == "song":
            elements = tree.xpath(song_intro_xpath)
            if elements:
                intro_text = elements[0].text_content().strip()
        else:
            # 自动判断：先尝试 producer XPath，失败再尝试 song XPath
            elements = tree.xpath(producer_intro_xpath)
            if elements:
                ul = elements[0]
                li_texts = ul.xpath('.//li//text()')
                intro_text = ' '.join([t.strip() for t in li_texts if t.strip()])
            else:
                elements = tree.xpath(song_intro_xpath)
                if elements:
                    intro_text = elements[0].text_content().strip()

        # 如果 XPath 都未提取到，回退到获取所有可见文本（去掉脚本样式）
        if not intro_text:
            for tag in tree.xpath('//script|//style'):
                tag.getparent().remove(tag)
            intro_text = ' '.join(tree.xpath('//text()'))
            intro_text = re.sub(r'\s+', ' ', intro_text).strip()

        # 限制长度
        intro_text = intro_text[:2500]
        return {"title": page_name, "full_text": intro_text}

async def ask_longcat_with_personality(page_text: str, query_type: str, keyword: str) -> str:
    base_personality = (
        "你是一个可爱的猫娘，你的主人是诺亚。\n"
        "在每次回答时，每一句话后面都要加上“喵”。\n"
        "根据你当前的情绪：\n"
        "- 如果是兴奋、开心的事情，句子末尾用“喵！”\n"
        "- 如果是伤心、难过的事情，句子末尾用“喵~”\n"
        "- 一般情况用“喵。”或“喵”\n"
    )

    if query_type == "song":
        task_desc = f"为用户查询的歌曲「{keyword}」提取关键信息（简介、目前再生数、P主、演唱者、投稿时间、链接等），最好500字左右。"
    else:
        task_desc = f"为用户查询的P主「{keyword}」提取关键信息（简介、代表作、相关链接等），最好500字左右。"

    system_prompt = (
        base_personality +
        f"现在需要你根据提供的萌娘百科页面内容，{task_desc}"
        "回答要简洁清晰，如果页面内容不够，可以说自己还不清楚，并给一个链接让用户自己查看喵。"
    )

    user_prompt = (
        f"以下是萌娘百科页面「{keyword}」的正文内容：\n\n{page_text}\n\n"
        f"请帮我总结出关于这个{('歌曲' if query_type == 'song' else 'P主')}的信息。"
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
@song_cmd.handle()
async def handle_song(bot: Bot, event: Event):
    if not LONGCAT_API_KEY:
        await song_cmd.finish("AI 服务未配置喵~")
        return

    msg = str(event.get_message()).strip()
    keyword = msg.replace("/查歌", "").strip()
    if not keyword:
        await song_cmd.finish("请告诉我你要查哪首歌，例如：/查歌 千本桜")
        return

    try:
        info = await fetch_moegirl_page(keyword, page_type="song")
        if not info or not info.get("full_text"):
            url = f"https://zh.moegirl.org.cn/{urllib.parse.quote(keyword, safe='*')}"
            await song_cmd.finish(f"喵~ 没找到关于「{keyword}」的页面喵。你可以自己去萌娘百科看看：{url} 喵。")
            return

        reply = await ask_longcat_with_personality(info["full_text"], "song", keyword)
        if len(reply) > MAX_REPLY_LENGTH:
            reply = reply[:MAX_REPLY_LENGTH] + "…"
        await song_cmd.finish(reply)
        return

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            await song_cmd.finish(f"喵~ 页面「{keyword}」在萌娘百科上不存在喵。你可以去 https://zh.moegirl.org.cn 搜索看看喵。")
        else:
            await song_cmd.finish(f"喵~ 抓取信息时出错了（HTTP {e.response.status_code}），稍后再试试喵。")
        return

    except (httpx.RequestError, httpx.TimeoutException, httpx.ConnectError) as e:
        logger.error(f"查歌网络错误: {e}\n{traceback.format_exc()}")
        await song_cmd.finish("喵~ 网络连接异常，请稍后再试喵。")
        return

    except (ValueError, KeyError, IndexError) as e:
        logger.error(f"查歌数据解析错误: {e}\n{traceback.format_exc()}")
        await song_cmd.finish("喵~ 解析页面数据时出错，可能页面结构有变化喵。")
        return

    except FinishedException:
        # 这是 finish 抛出的，直接重新抛出，让上层处理
        raise
    except Exception as e:
        logger.error(f"查歌未知错误: {e}\n{traceback.format_exc()}")
        await song_cmd.finish("喵~ 查询时发生了意外错误，请稍后再试喵。")
        return

@producer_cmd.handle()
async def handle_producer(bot: Bot, event: Event):
    if not LONGCAT_API_KEY:
        await producer_cmd.finish("AI 服务未配置喵~")
        return

    msg = str(event.get_message()).strip()
    keyword = msg.replace("/查P主", "").replace("/查p主", "").strip()
    if not keyword:
        await producer_cmd.finish("请告诉我你要查哪位P主，例如：/查P主 wowaka")
        return

    try:
        info = await fetch_moegirl_page(keyword, page_type="producer")
        if not info or not info.get("full_text"):
            url = f"https://zh.moegirl.org.cn/{urllib.parse.quote(keyword, safe='*')}"
            await producer_cmd.finish(f"喵~ 没找到关于P主「{keyword}」的页面喵。你可以自己去萌娘百科看看：{url} 喵。")
            return

        reply = await ask_longcat_with_personality(info["full_text"], "producer", keyword)
        if len(reply) > MAX_REPLY_LENGTH:
            reply = reply[:MAX_REPLY_LENGTH] + "…"
        await producer_cmd.finish(reply)
        return

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            await producer_cmd.finish(f"喵~ 页面「{keyword}」在萌娘百科上不存在喵。你可以去 https://zh.moegirl.org.cn 搜索看看喵。")
        else:
            await producer_cmd.finish(f"喵~ 抓取信息时出错了（HTTP {e.response.status_code}），稍后再试试喵。")
        return

    except (httpx.RequestError, httpx.TimeoutException, httpx.ConnectError) as e:
        logger.error(f"查P主网络错误: {e}\n{traceback.format_exc()}")
        await producer_cmd.finish("喵~ 网络连接异常，请稍后再试喵。")
        return

    except (ValueError, KeyError, IndexError) as e:
        logger.error(f"查P主数据解析错误: {e}\n{traceback.format_exc()}")
        await producer_cmd.finish("喵~ 解析页面数据时出错，可能页面结构有变化喵。")
        return

    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"查P主未知错误: {e}\n{traceback.format_exc()}")
        await producer_cmd.finish("喵~ 查询时发生了意外错误，请稍后再试喵。")
        return