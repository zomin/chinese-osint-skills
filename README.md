# Chinese OSINT 🇨🇳🔍

> 基于实战验证的中国互联网OSINT技能库 —— 从有限信息（姓名、手机号、QQ号、昵称）出发，系统化搜索目标人员的公开数字足迹。

## 为什么需要这个项目？

现有的OSINT工具（如 Sherlock、Maigret、GhostTrack）主要面向国际平台，对中国互联网生态支持薄弱。本项目填补这一空白：

- **610+ 平台覆盖**：Maigret(210+) + Sherlock(400+) + 中国平台专项策略
- **分层搜索策略**：从直接API → 搜索引擎 → 浏览器自动化 → 手动验证，效率递减但覆盖递增
- **实战验证的陷阱库**：每个平台都有独立的反爬策略和数据陷阱，我们踩过坑并记录下来
- **跨平台交叉验证方法论**：解决"同名同姓"问题，通过多维度排除假阳性

## 功能矩阵

| 能力 | 方法 | 覆盖平台 |
|------|------|----------|
| QQ资料获取 | CDN/API直取 | QQ、QQ空间 |
| 用户名枚举 | Maigret + Sherlock | 610+ 国际平台 |
| 社交媒体搜索 | Playwright + Bing/百度 | 抖音/微博/小红书/B站等 |
| 微博内容抓取 | Playwright一次性窗口 | 微博（含region_name定位） |
| 企业关联查询 | 搜索引擎快照 | 天眼查/爱企查 |
| 手机号归属 | 离线号段解析 | 全国运营商 |
| 头像分析 | PIL+numpy像素分析 | 判断真人/插画/动漫 |
| 反向图片搜索 | 百度识图/Yandex | 跨平台头像溯源 |

## 快速开始

### 环境要求

- Python 3.8+
- pip

### 安装依赖

```bash
# 核心工具
pip install maigret sherlock-project

# 浏览器自动化（微博等平台抓取需要）
pip install playwright playwright-stealth
playwright install chromium

# 头像分析
pip install pillow numpy

# HTTP请求和HTML解析
pip install requests beautifulsoup4
```

### 使用示例

```bash
# 1. 跨平台用户名搜索
maigret "target_username" --top-sites 200
sherlock "target_username" --print-found

# 2. QQ头像获取
python scripts/qq_avatar.py --qq 123456789

# 3. 头像分析（判断真人 vs 动漫）
python scripts/avatar_analysis.py --image avatar.jpg
python scripts/avatar_analysis.py --url "https://q1.qlogo.cn/g?b=qq&nk=123456789&s=640"

# 4. 微博一次性窗口抓取
python scripts/weibo_scraper.py --uid 1234567890

# 5. 跨平台批量搜索（Maigret + Sherlock + B站）
python scripts/cross_platform_search.py --nickname "target_username"
```

## 搜索策略分层

```
Tier 1    ━━  直接API（秒级，无需浏览器）
              QQ头像 / B站用户搜索 / 手机号归属

Tier 2    ━━  Playwright浏览器自动化（通用最有效）
              Bing/百度搜索 / DuckDuckGo HTML

Tier 2.5  ━━  微博一次性窗口（首次加载有效）
              Playwright + stealth 拦截API响应

Tier 3    ━━  本地curl（几乎不可用）
              全站CAPTCHA / 空结果

Tier 3.5  ━━  各平台直接访问
              天眼查地理围栏 / 小红书需登录 / 抖音有限
```

**核心原则**：永远从Tier 1开始，逐层下沉。不要在Tier 3浪费时间。

## 项目结构

```
chinese-osint/
├── SKILL.md                          # 核心技能文档（完整方法论）
├── README.md                         # 本文件
├── LICENSE                           # MIT License
├── DISCLAIMER.md                     # 免责声明
├── requirements.txt                  # Python依赖
├── scripts/
│   ├── qq_avatar.py                  # QQ头像获取
│   ├── avatar_analysis.py            # 头像像素分析（真人/插画判断）
│   ├── weibo_scraper.py              # 微博一次性窗口抓取
│   └── cross_platform_search.py      # 跨平台用户名批量搜索
└── docs/
    └── platform-traps.md             # 各平台反爬陷阱详解
```

## 关键发现（实战总结）

### ✅ 可靠的方法
- QQ头像CDN无防盗链，直接URL可获取
- Playwright + stealth 可搜索 Bing/百度，绕过大部分CAPTCHA
- 微博 `region_name` 字段是极强的定位信号
- B站空间页面完全公开，无需登录
- Maigret + Sherlock 可快速枚举610+平台

### ❌ 不要浪费时间的事
- 本地curl任何中国搜索引擎 → 全CAPTCHA
- QQ昵称API（2026年4月起全面要求登录）
- 微博连续请求 → 一次性窗口，第二次必定触发验证码
- 天眼查/爱企查从海外IP → 法律地理围栏，不可绕过
- "番番寻客宝"企业关联 → 数据污染，不代表真实工作关系

## 与 AI Agent 集成

本项目的 SKILL.md 可被 AI Agent 系统加载为技能指令：

**Hermes Agent**：
```bash
git clone https://github.com/<username>/chinese-osint.git \
  ~/.hermes/skills/research/chinese-osint
```

**其他 Agent**：将 SKILL.md 内容作为 system prompt 或知识库文档加载即可。

## 贡献指南

欢迎贡献！以下是最需要帮助的方向：

1. **新平台适配**：小红书、闲鱼、知乎等平台的搜索策略
2. **API变化更新**：各平台API的封堵情况和替代方案
3. **脚本改进**：更好的错误处理、并发支持、结果格式化
4. **文档补充**：更多实战案例、陷阱记录
5. **国际化**：英文文档翻译

请阅读 [DISCLAIMER.md](DISCLAIMER.md) 后提交PR。

## ⚠️ 免责声明

**本项目仅供合法用途**。使用本工具时，你必须：

- ✅ 遵守当地法律法规
- ✅ 仅搜索公开可获取的信息
- ✅ 尊重他人隐私权
- ✅ 用于安全研究、找人、防诈骗等合法目的
- ❌ 不得用于骚扰、跟踪、人肉搜索或其他非法活动
- ❌ 不得获取或传播非公开的个人信息

详见 [DISCLAIMER.md](DISCLAIMER.md)。

## License

MIT License - 详见 [LICENSE](LICENSE)
