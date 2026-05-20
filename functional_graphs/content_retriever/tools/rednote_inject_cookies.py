"""
一次性 Cookie 注入脚本：把 rednote.com 的 cookies 写入 Playwright Chrome profile。
跑一次即可，之后 content_retriever pipeline 直接使用保存的 profile。

用法：python3 tools/rednote_inject_cookies.py
"""
import asyncio
from playwright.async_api import async_playwright

CHROME_DATA_DIR = "/home/kingy/Foundation/EdenGateway/rednote-chrome-data"
CHROME_BIN = "/usr/bin/google-chrome"

# 你的 rednote.com cookies（两个域名都注入，确保兼容）
COOKIES = [
    {
        "name": "id_token",
        "value": "VjEAAPP2Aov5ukHHCzIvd5482pmw5L8O7Cf05ZvqHX+VbALQk6kTUaFgW0S5FJKVyhgfMOn7/JD2y+rjkMW4rSO4j9Lhchh4ZgL2Pm9zVFt5wh8RJ3+7k0EeeOaXRq/LPYdpmGpq",
        "domain": ".rednote.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
    },
    {
        "name": "web_session",
        "value": "040069b1c2fc7e562c63669b39384b2b545fa6",
        "domain": ".rednote.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
    },
    {
        "name": "x-rednote-holderctry",
        "value": "US",
        "domain": ".rednote.com",
        "path": "/",
        "secure": True,
        "httpOnly": False,
    },
    {
        "name": "x-rednote-datactry",
        "value": "SG",
        "domain": ".rednote.com",
        "path": "/",
        "secure": True,
        "httpOnly": False,
    },
    # 同样注入 xiaohongshu.com（下载时用）
    {
        "name": "web_session",
        "value": "040069b1c2fc7e562c63669b39384b2b545fa6",
        "domain": ".xiaohongshu.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
    },
    {
        "name": "id_token",
        "value": "VjEAAPP2Aov5ukHHCzIvd5482pmw5L8O7Cf05ZvqHX+VbALQk6kTUaFgW0S5FJKVyhgfMOn7/JD2y+rjkMW4rSO4j9Lhchh4ZgL2Pm9zVFt5wh8RJ3+7k0EeeOaXRq/LPYdpmGpq",
        "domain": ".xiaohongshu.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
    },
]


async def main():
    print(f"Chrome profile: {CHROME_DATA_DIR}")
    print("注入 cookies 中...")

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=CHROME_DATA_DIR,
            executable_path=CHROME_BIN,
            headless=True,
            args=["--headless=new", "--no-sandbox", "--disable-dev-shm-usage"],
            viewport={"width": 1280, "height": 900},
        )

        await ctx.add_cookies(COOKIES)

        # 访问 rednote.com 验证 cookies 生效
        page = await ctx.new_page()
        await page.goto("https://www.rednote.com/", timeout=20000)
        await asyncio.sleep(2)

        # 检查登录态：看页面是否有用户信息
        title = await page.title()
        cookies_now = await ctx.cookies("https://www.rednote.com")
        names = [c["name"] for c in cookies_now]

        print(f"页面标题: {title}")
        print(f"已存储 cookies: {names}")

        logged_in = "web_session" in names
        if logged_in:
            print("✅ Cookie 注入成功，登录态已保存到 Chrome profile")
        else:
            print("❌ Cookie 未正确注入，请检查")

        await ctx.close()

    print(f"\n完成。Profile 路径：{CHROME_DATA_DIR}")
    print("现在可以运行 pipeline：")
    print("  cd /home/kingy/Foundation/VoidDraft")
    print("  python3 -m pipelines.content_retriever.run \\")
    print("    --config functional_graphs/content_retriever/configs/rednote_example.yaml \\")
    print("    --max-posts 3")


if __name__ == "__main__":
    asyncio.run(main())
