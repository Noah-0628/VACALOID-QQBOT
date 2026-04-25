from nonebot import require
require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

from .generate_style_profile import generate_style

@scheduler.scheduled_job("cron", hour=3, minute=0)
def daily_style_update():
    generate_style()