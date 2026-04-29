# 各平台反爬陷阱详解

> 基于2026年4月实战测试，各平台的反爬策略和应对方法。

## 搜索引擎类

| 平台 | 反爬方式 | 应对策略 |
|------|----------|----------|
| 百度 | CAPTCHA（滑块/点选） | Playwright + stealth 偶尔可用 |
| 必应中国 | 偶发CAPTCHA | Playwright + stealth 可靠 |
| 搜狗 | 空结果 | 无解 |
| 360搜索 | 较宽松 | 从服务器IP偶尔可用，但结果质量低 |
| DuckDuckGo HTML | 无CAPTCHA但中文结果差 | URL编码+requests直接请求，成功率约50% |
| Yandex | CAPTCHA | 无解 |

**DuckDuckGo HTML 搜索示例**（无需浏览器）：
```python
import requests, urllib.parse
from bs4 import BeautifulSoup

query = urllib.parse.quote("目标昵称")
resp = requests.get(
    f"https://html.duckduckgo.com/html/?q={query}",
    headers={"User-Agent": "Mozilla/5.0"},
    timeout=10
)
soup = BeautifulSoup(resp.text, 'html.parser')
for r in soup.select('.result'):
    title = r.select_one('.result__title a')
    snippet = r.select_one('.result__snippet')
    if title:
        print(f"{title.text}: {snippet.text if snippet else ''}")
```

## 社交平台类

### 微博 (m.weibo.cn)
- **一次窗口**：首次页面加载成功，第二次必触发验证码
- **防盗链**：sinaimg.cn图片直接下载返回403
- **应对**：拦截API响应获取数据；`page.screenshot()` 截图保存图片
- **关键字段**：`region_name`（"发布于 XX"）是定位利器

### 抖音 (douyin.com)
- IP属地可见（如"陕西"）
- 粉丝数/获赞数需登录
- API返回 `{"aweme_list":null}`
- 应对：搜索引擎 `site:douyin.com` 获取有限信息

### 小红书 (xiaohongshu.com)
- 搜索功能完全需登录
- API返回 `{"code":-1}`
- 应对：Playwright 搜索 Bing `site:xiaohongshu.com`

### B站 (bilibili.com)
- **空间页面公开**：space.bilibili.com无需登录
- **API需cookie**：搜索API返回-352错误
- 应对：先访问首页获取cookie，再调搜索API

### QQ空间 (qzone.qq.com)
- 封闭空间=空白页
- 昵称API 2026年4月起要求登录
- 应对：头像CDN仍可用；让用户手动查看

## 企业信息类

### 天眼查/爱企查
- **法律地理围栏**：非大陆IP显示"当前地区暂不支持访问"
- 这不是技术反爬，是法律合规限制，**不可绕过**
- 应对：通过搜索引擎快照获取有限信息

### 番番寻客宝/百度企业黄页
- 手机号关联企业结果多为**数据污染**
- 判断标准：企业地域是否匹配目标活动轨迹
- 跨省/跨行业的企业关联 → 大概率是爬虫自动聚合

## 通用反爬策略

### Playwright + Stealth 配置
```python
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

async def create_stealth_page(browser):
    stealth = Stealth()
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36",
        viewport={"width": 414, "height": 896}
    )
    page = await context.new_page()
    await stealth.apply(page)
    return page
```

### 验证码应对
- **不要反复重试** — 每次失败都会加严
- **换session** — 关闭浏览器重新启动
- **一次窗口** — 拿到数据就停，不要贪心翻页

### 请求频率建议
- 单平台：每次请求间隔 ≥3秒
- 搜索引擎：每分钟 ≤5次
- 微博：只做一次，不要连续请求
