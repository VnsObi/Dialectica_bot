import os
import aiohttp
from loguru import logger

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

async def send_slack_alert(message: str):
    """Sends an alert to Slack, if configured. Addresses Gap 6."""
    if not SLACK_WEBHOOK_URL:
        return
    try:
        async with aiohttp.ClientSession() as session:
            payload = {"text": f"🚨 *Dialectica Bot Alert* 🚨\n{message}"}
            await session.post(SLACK_WEBHOOK_URL, json=payload, timeout=5.0)
    except Exception as e:
        logger.error(f"Failed to send Slack alert: {e}")

def get_slack_sink():
    """Returns a loguru sink function for critical alerts."""
    import asyncio
    def slack_sink(message):
        record = message.record
        if record["level"].name == "CRITICAL" and SLACK_WEBHOOK_URL:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(send_slack_alert(record["message"]))
            except RuntimeError:
                pass # No running loop
    return slack_sink
