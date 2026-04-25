import requests

# ===== 请替换为你的真实 API Key =====
API_KEY = ""

URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}
payload = {
    "model": "GLM-4-Flash",
    "messages": [{"role": "user", "content": "你好，这是一条测试消息"}],
    "max_tokens": 50,
    "temperature": 0.1
}

try:
    resp = requests.post(URL, json=payload, headers=headers, timeout=10)
    print(f"HTTP 状态码: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        print("✅ 调用成功！回复内容：")
        print(data["choices"][0]["message"]["content"])
    else:
        print(f"❌ 调用失败，返回信息：\n{resp.text}")
except Exception as e:
    print(f"❌ 请求异常: {e}")
