import sqlite3

db_path = r"E:\qqbot\qqbot\plugins\cache\data\chat_history.db"
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute("SELECT group_id, user_hash, content, timestamp FROM messages")
rows = cursor.fetchall()
for row in rows:
    print(row)
conn.close()