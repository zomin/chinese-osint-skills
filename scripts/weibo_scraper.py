#!/usr/bin/env python3
"""
Weibo One-Shot Scraper — 微博一次性窗口抓取

⚠️ 核心策略：首次加载是唯一窗口，CAPTCHA触发后整个session作废。
   拦截API响应获取数据，不要翻页，拿首页就走。

用法：
    pip install playwright playwright-stealth
    playwright install chromium

    python weibo_scraper.py --uid 1234567890
    python weibo_scraper.py --uid 1234567890 --output weibo_data.json
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime

try:
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth
except ImportError:
    print("请安装依赖: pip install playwright playwright-stealth")
    print("然后运行: playwright install chromium")
    sys.exit(1)


WEIBO_MOBILE = "https://m.weibo.cn"
CONTAINER_FEED = "107603{uid}"  # 微博列表
CONTAINER_PROFILE = "100505{uid}"  # 用户主页


async def scrape_weibo(uid: str, output_path: str = None):
    """一次性窗口抓取微博数据"""

    captured_data = {
        "uid": uid,
        "scrape_time": datetime.now().isoformat(),
        "profile": {},
        "posts": [],
        "warnings": []
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36",
            viewport={"width": 414, "height": 896}
        )

        # 注入stealth脚本
        stealth = Stealth()
        page = await context.new_page()
        await stealth.apply(page)

        # === 拦截API响应 ===
        async def on_response(resp):
            url = resp.url
            try:
                if 'container/getIndex' in url:
                    data = await resp.json()
                    cards = data.get('data', {}).get('cards', [])

                    for card in cards:
                        # 用户资料
                        if card.get('card_group'):
                            for item in card['card_group']:
                                if item.get('user'):
                                    captured_data['profile'] = item['user']

                        # 微博帖子
                        mblog = card.get('mblog')
                        if mblog:
                            post = {
                                "id": mblog.get("id"),
                                "text": mblog.get("text", ""),
                                "created_at": mblog.get("created_at"),
                                "region_name": mblog.get("region_name", ""),  # ← 定位利器！
                                "source": mblog.get("source", ""),
                                "reposts_count": mblog.get("reposts_count", 0),
                                "comments_count": mblog.get("comments_count", 0),
                                "attitudes_count": mblog.get("attitudes_count", 0),
                                "is_original": not bool(mblog.get("retweeted_status")),
                                "pic_ids": mblog.get("pic_ids", []),
                            }
                            captured_data['posts'].append(post)

            except Exception as e:
                captured_data['warnings'].append(f"Response parse error: {e}")

        page.on('response', on_response)

        # === 先访问首页建立cookie ===
        print("[*] 访问微博首页建立cookie...")
        await page.goto(WEIBO_MOBILE, wait_until='domcontentloaded', timeout=15000)
        await asyncio.sleep(2)

        # === 访问目标用户主页（唯一一次机会）===
        profile_url = f"{WEIBO_MOBILE}/u/{uid}"
        print(f"[*] 访问用户主页: {profile_url}")
        print("[!] 注意：这是唯一一次窗口，不要刷新或翻页")

        await page.goto(profile_url, wait_until='domcontentloaded', timeout=15000)
        await asyncio.sleep(3)  # 等待API响应

        # === 不要再请求任何微博页面！到此为止 ===

        await browser.close()

    # 输出结果
    print(f"\n[+] 抓取完成:")
    print(f"    用户: {captured_data['profile'].get('screen_name', 'N/A')}")
    print(f"    简介: {captured_data['profile'].get('description', 'N/A')}")
    print(f"    帖子数: {len(captured_data['posts'])}")

    if captured_data['posts']:
        print(f"\n[+] 最近帖子:")
        for i, post in enumerate(captured_data['posts'][:5]):
            region = post['region_name'] or '无地区信息'
            text_preview = post['text'][:50].replace('\n', ' ')
            print(f"    [{i+1}] {region} | {text_preview}...")

    if captured_data['warnings']:
        print(f"\n[!] 警告:")
        for w in captured_data['warnings']:
            print(f"    - {w}")

    # 保存
    path = output_path or f"weibo_{uid}.json"
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(captured_data, f, ensure_ascii=False, indent=2)
    print(f"\n[+] 已保存到: {path}")

    return captured_data


def main():
    parser = argparse.ArgumentParser(description="微博一次性窗口抓取工具")
    parser.add_argument("--uid", required=True, help="微博用户UID")
    parser.add_argument("--output", help="输出JSON路径")
    args = parser.parse_args()

    asyncio.run(scrape_weibo(args.uid, args.output))


if __name__ == "__main__":
    main()
