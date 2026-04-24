import asyncio
import httpx
from datetime import datetime, timedelta

API_URL = "https://vocadb.net/api/songs"

async def get_top_song_by_date(year: int, month: int, day: int):
    """获取指定日期投稿的评分最高的歌曲（只需1次请求）"""
    target_date = datetime(year, month, day)
    date_start = target_date.isoformat()
    date_end = (target_date + timedelta(days=1)).isoformat()

    params = {
        "sort": "RatingScore",      # 按评分降序
        "maxResults": 1,            # 只取第一名
        "dateAfter": date_start,
        "dateBefore": date_end,
        "fields": "Artists,PVs",
        "status": "Finished",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(API_URL, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        return items[0] if items else None

async def main():
    year, month, day = 2010, 2, 14   # 情人节
    print(f"查询 {year}年{month}月{day}日 评分最高的歌曲...")
    song = await get_top_song_by_date(year, month, day)
    if not song:
        print("当天没有歌曲。")
        return

    name = song.get("name", "未知")
    artist = song.get("artistString", "未知")
    rating = song.get("ratingScore", 0)
    publish = song.get("publishDate", "未知")
    # 提取 PV 链接
    pvs = song.get("pvServices", [])
    url = next((pv.get("url") for pv in pvs if pv.get("service") in ["NicoNicoDouga", "Youtube"]), "")
    print(f"\n🎵 {name} / {artist}")
    print(f"📅 {publish[:10]} | 评分：{rating:.0f}")
    if url:
        print(f"🔗 {url}")

if __name__ == "__main__":
    asyncio.run(main())