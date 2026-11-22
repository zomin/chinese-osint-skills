# 贡献指南 / Contributing

> 本文件描述**本仓库当前的实际约定**，不是理想规范。提交前请先读 [README.md](README.md) 与 [DISCLAIMER.md](DISCLAIMER.md)。

## 1. 仓库结构（现状）

```
chinese-osint-skills/
├── AGENTS.md                      # 通用 agent 规则（无 frontmatter）
├── CLAUDE.md                      # Claude Code 版（HTML 注释头 + 同正文）
├── SKILL.md                       # Hermes Agent 版（YAML frontmatter + 同正文）
├── README.md
├── LICENSE                        # MIT
├── DISCLAIMER.md
├── install.sh                     # 8 平台一键安装
├── requirements.txt
├── .github/copilot-instructions.md
├── docs/platform-traps.md
├── scripts/*.py
└── tests/                         # 夹具 runner（本仓库新增）
```

### 1.1 四份规则文件必须同步 —— 这是最容易漏的一条

`AGENTS.md` 与 `.github/copilot-instructions.md` 目前**字节级完全相同**（SHA-256 `6F001EAB…C422E`）。`CLAUDE.md` 与 `SKILL.md` 是同一份正文再加不同的文件头：

| 文件 | 文件头 | 正文 |
|---|---|---|
| `AGENTS.md` | 无 | 同一份 |
| `.github/copilot-instructions.md` | 无 | 同一份 |
| `CLAUDE.md` | 两行 HTML 注释 | 同一份 |
| `SKILL.md` | YAML frontmatter（`name` / `description` / `version` / `author` / `license`） | 同一份 |

所以**任何一条能力、平台、陷阱的增删，都要在四处同步改**，否则 Copilot 用户和 Claude 用户会看到不一致的规则。改完用下面这条自查：

```bash
sha256sum AGENTS.md .github/copilot-instructions.md
diff <(tail -n +2 CLAUDE.md | tail -n +2) AGENTS.md   # 正文应一致
```

### 1.2 新增脚本要同时改三处

1. `scripts/<name>.py` —— 脚本本体
2. `README.md` 的**功能矩阵表**与**项目结构树**
3. 上面 1.1 的四份规则文件里对应的 Tier 段落

漏掉第 2、3 步的 PR 会被要求补。

## 2. 脚本约定

从现有脚本（`qq_avatar.py`、`qq_info.py`、`cross_platform_search.py`、`weibo_scraper.py`、`avatar_analysis.py`）归纳：

```python
#!/usr/bin/env python3
"""
English Name — 一句话中文说明

用法：
    python script_name.py --arg value
    python script_name.py --arg value --flag

注意：
    这里写会让使用者踩坑的事实（限流、失效时间、字段语义）。
"""

import argparse
import sys
```

硬性要求：

- **文件名单词下划线**：`id_card_parse.py`，不用 `id-card-parse.py`
- **shebang 第一行**，docstring 第二段起写 `English Name — 中文说明 / 用法 / 注意`
- **入口用 `argparse`**，`main()` 返回退出码，`sys.exit(main())`
- **常量用 UPPER_SNAKE**（`QQ_AVATAR_URL`、`SIZE_MAP`）
- **类型标注可选但鼓励**（现有脚本部分有）
- **中文注释可直接写**，仓库正文就是中文
- **新增第三方依赖要进 `requirements.txt`**；只通过 CLI 调用的外部工具（`maigret`、`sherlock-project`）按现有写法注释掉，不列入安装依赖

### 2.1 Python 版本

`requirements.txt` 与四份规则文件都写 **Python 3.8+**。若你的脚本用到 3.9+ 的运行时能力（`str.removeprefix`、`Path.is_relative_to`、`zoneinfo`、内置泛型下标在非注解位置等），有两种做法：

- 加 `from __future__ import annotations` 让注解延迟求值，且确认没有 3.9+ 的运行时 API 调用；
- 或在 PR 里明确提议把最低版本抬到 3.10，并同步改 `requirements.txt` 与四份规则文件——这是需要维护者拍板的变更，不要静默改。

### 2.2 数据类脚本的额外要求

凡是依赖**外部数据文件**（号段库、区划表、字典）的脚本：

- 数据**不随仓库分发**，脚本里给出获取方式与许可
- 输出必须带 `dataVersion` / `observed_at` 一类字段，让使用者能判断结论是否过期
- 数据缺失时**报错退出**，不要静默返回空结果

### 2.3 退出码契约（硬性）

`scripts/` 下所有脚本必须遵守 [`docs/exit-codes.md`](docs/exit-codes.md)，实现见 `scripts/_codes.py`。

三条底线：

- **不许用 `exit 1`**。`1` 代表「未分类」，它的出现就等于某处没接契约。所有失败路径改用 `_codes.Exit` 里登记的码。
- **`main()` 第一行调 `force_utf8_streams()`**。Windows 控制台 cp936 下 `print("✅")` 会抛 `UnicodeEncodeError`，会直接打崩管道。
- **失败写结构化行**：`return emit(Exit.RATE_LIMITED, "触发限流", url=url)`，不要手写中文长句 —— 上层要能用 `json.loads` 取 `error.code`。

判定语义：**进程退出码只回答「输入能不能被解析」，数据精度与不确定性写进 record**（`level` / `warnings[]` / `confidence.level`）。批量输入里有一条坏数据应回 `3`（`partial`）而不是崩溃；只有全部记录都无法解析才算失败。

新增脚本的接入模板与 `classify_http()` 的完整判定表见 `docs/exit-codes.md`。改动契约本身（增删码）需要维护者拍板，不要在 PR 里静默改 `_codes.py` 的码值。

## 3. 文档约定

- 放 `docs/`，文件名小写连字符（`platform-traps.md`、`id-card.md`、`public-records.md`）
- 开头一段说清**这份文档解决什么问题**，不要只罗列
- 表格优先于长段落；表格前留空行
- **不许编造**：站点路径、接口参数、字段名、判例案号，凡未实测的一律标 `[需实测确认]`
- 记录**失效时间**：本仓库的陷阱文档以「基于 2026 年 4 月实战测试」开头，这是正确做法，新文档请沿用

## 4. 测试

仓库原本没有测试。现在 `tests/` 提供一套**只用标准库**的夹具 runner：

```bash
python tests/run-fixtures.py            # 全部
python tests/run-fixtures.py id_card_parse -v
```

- 夹具放 `tests/fixtures/<脚本名>/<场景>.json`
- **只用合成数据**，不要提交真实证件号、真实手机号、真实姓名
- 依赖外部二进制数据的脚本（如 `phone_locate.py`），夹具用**文本描述**生成临时库，不要提交二进制

## 5. PR 要求

### 5.1 正文必须包含「数据来源与可信度」

| 论断 | 来源 | 级别 | 核对方式 |
|---|---|---|---|

级别定义见 §6。没有这张表的 PR 会被要求补。

### 5.2 提交前自查

- [ ] 脚本能在 Python 3.8 语义下导入（或已在 PR 里说明版本要求）
- [ ] `python tests/run-fixtures.py` 全绿
- [ ] `README.md` 功能矩阵与项目结构树已更新
- [ ] 四份规则文件（`AGENTS.md` / `CLAUDE.md` / `SKILL.md` / `.github/copilot-instructions.md`）已同步
- [ ] 新增依赖已进 `requirements.txt`
- [ ] 没有真实个人信息（证件号、手机号、姓名、地址、头像）
- [ ] 没有抓取结果、没有第三方受限数据
- [ ] 与 [DISCLAIMER.md](DISCLAIMER.md) 的「仅公开信息」条款不冲突

## 6. 数据与结论的分级

| 级别 | 含义 | 在文档里怎么用 |
|---|---|---|
| A | 国标、法条、可复现的算法 | 可直接作为结论 |
| B | 官方站点实测观察、有日期 | 可下结论，须标观察日期 |
| C | 商业库/第三方聚合 | 只能当线索 |
| D | 泄露数据检索 | 不许作为结论，只能转述其存在性 |
| E | 推测、经验、未验证 | 只能写成待验证问题 |

**不要用 D 级数据支撑任何事实判断。** 泄露库互相转卖、版本重叠、时效不可控，且买卖与检索在《刑法》第二百五十三条之一范围内。

## 7. 不接受的内容

- 需要登录态或过验证码才能拿数据的抓取逻辑（微信、小红书、抖音私域、QQ空间好友内容等）
- 一条命令串多站点自动汇总的「一键开盒」流程
- 任何依赖泄露数据源的功能
- 真实个人信息的样本
- 绕过地域合规限制的方法（如海外 IP 访问天眼查/爱企查，本仓库已明确记录为不可绕过）

## 8. License

提交即同意以 [MIT](LICENSE) 授权。
