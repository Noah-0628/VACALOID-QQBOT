from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, MessageEvent

ping = on_command("ping")
cao = on_command("草")

@ping.handle()
async def handle_ping(bot: Bot, event: MessageEvent):
    await ping.finish("pong")

@cao.handle()
async def handle_cao(bot: Bot, event: MessageEvent):
    await cao.finish("不能骂人哦")