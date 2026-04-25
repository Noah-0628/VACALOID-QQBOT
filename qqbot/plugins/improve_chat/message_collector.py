import time
import asyncio
import sqlite3
from collections import deque
from typing import Deque, Tuple

from nonebot import get_driver, on_message, logger
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Bot

# ================= 配置 =================
DATABASE_PATH = r"E:\qqbot\qqbot\plugins\cache\data\chat_history.db"
FLUSH_INTERVAL = 30        # 写入间隔（秒），5 分钟
MAX_FLUSH_COUNT = 100       # 每次最多写入条数
MAX_RETAIN_DAYS = 30        # 消息保留天数
MSG_MIN_LEN = 2             # 最短有效字数
MSG_MAX_LEN = 80            # 最长保留字数（截断）
USER_ID_SUFFIX_LEN = 4      # QQ 号尾数长度（用于脱敏）

# ================= 全局状态 =================
msg_queue: Deque[Tuple[int, int, str, float]] = deque()  # (group_id, user_id, content, timestamp)
queue_lock = asyncio.Lock()
db_lock = asyncio.Lock()
_shutdown_flag = False

driver = get_driver()

# ================= 工具函数 =================
def get_db():
    """获取数据库连接（开启 WAL 模式提高并发）"""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            user_hash TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    logger.info("消息采集数据库初始化完毕")

def clean_old():
    """删除超过保留期限的消息"""
    cutoff = time.time() - MAX_RETAIN_DAYS * 86400
    try:
        conn = get_db()
        conn.execute("DELETE FROM messages WHERE timestamp < ?", (cutoff,))
        conn.commit()
        conn.close()
        logger.info("已完成过期消息清理")
    except Exception as e:
        logger.error(f"清理旧消息失败: {e}")

async def flush_queue():
    """将队列中的消息批量写入数据库"""
    global msg_queue
    async with queue_lock:
        if not msg_queue:
            return
        # 取出最多 MAX_FLUSH_COUNT 条
        batch = []
        while msg_queue and len(batch) < MAX_FLUSH_COUNT:
            batch.append(msg_queue.popleft())
        if not batch:
            return

    # 写入数据库（加锁避免并发写问题）
    async with db_lock:
        try:
            conn = get_db()
            conn.executemany(
                "INSERT INTO messages (group_id, user_hash, content, timestamp) VALUES (?,?,?,?)",
                batch
            )
            conn.commit()
            conn.close()
            logger.debug(f"批量写入 {len(batch)} 条消息")
        except Exception as e:
            logger.error(f"批量写入失败: {e}")
            # 写入失败将数据放回队列头部（简单粗暴避免丢失）
            async with queue_lock:
                msg_queue.extendleft(reversed(batch))

async def flush_loop():
    """每 FLUSH_INTERVAL 秒执行一次写入"""
    while not _shutdown_flag:
        await asyncio.sleep(FLUSH_INTERVAL)
        await flush_queue()
        # 顺便清理一次旧数据（每天清理一次即可，这里每次循环都清也没关系，效率损耗极小）
        await asyncio.get_event_loop().run_in_executor(None, clean_old)

# ================= 消息事件 =================
def make_user_hash(user_id: int) -> str:
    """用 QQ 号尾数生成脱敏标识"""
    return str(user_id % (10 ** USER_ID_SUFFIX_LEN)).zfill(USER_ID_SUFFIX_LEN)

def should_collect(event: GroupMessageEvent, bot: Bot) -> bool:
    """判断消息是否应该被采集"""
    # 忽略机器人自己的消息
    if event.user_id == event.self_id:
        return False
    # 忽略包含 CQ 码的消息
    raw = event.get_message()
    if any(seg.type != "text" for seg in raw):
        return False
    text = event.get_plaintext().strip()
    if not text:
        return False
    # 长度过滤
    if len(text) < MSG_MIN_LEN or len(text) > MSG_MAX_LEN:
        # 如果超过最大长度，仍可保留，但截断（所以可以在后面处理）
        pass
    # 忽略纯网址
    if "http" in text:
        return False
    return True

collector = on_message(priority=200, block=False)  # 极低优先级，不干扰任何功能

@collector.handle()
async def handle_collect(event: GroupMessageEvent, bot: Bot):
    if not should_collect(event, bot):
        return
    text = event.get_plaintext().strip()
    # 截断过长的文本
    if len(text) > MSG_MAX_LEN:
        text = text[:MSG_MAX_LEN]
    group_id = event.group_id
    user_id = event.user_id
    timestamp = event.time if event.time else time.time()
    user_hash = make_user_hash(user_id)
    # 入队
    async with queue_lock:
        msg_queue.append((group_id, user_hash, text, timestamp))

# ================= 生命周期 =================
async def start_collector():
    """插件启动时初始化数据库并启动定时写入任务"""
    init_db()
    # 清理一次旧数据
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, clean_old)
    # 启动后台循环
    task = asyncio.create_task(flush_loop())
    # 保存任务引用以便关闭
    driver._collector_flush_task = task

async def stop_collector():
    """关闭前将剩余消息全部写入"""
    global _shutdown_flag
    _shutdown_flag = True
    # 取消后台任务
    task = getattr(driver, "_collector_flush_task", None)
    if task:
        task.cancel()
    # 最后一次强制写入
    await flush_queue()
    logger.info("消息采集插件已关闭，剩余消息已入库")

driver.on_startup(start_collector)
driver.on_shutdown(stop_collector)