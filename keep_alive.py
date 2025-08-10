import asyncio
from telegram import Bot

async def keep_alive():
    bot = Bot(token="8061166709:AAHIbdxBrEdE1aEdO3cHEUV_Y84Cqjs6npU")
    while True:
        await asyncio.sleep(300)  # هر 5 دقیقه
        await bot.get_me()  # یک درخواست ساده برای فعال نگه داشتن

# در تابع اصلی
if __name__ == "__main__":
    from concurrent.futures import ThreadPoolExecutor
    executor = ThreadPoolExecutor()
    executor.submit(asyncio.run, keep_alive())
    
    application.run_polling()
