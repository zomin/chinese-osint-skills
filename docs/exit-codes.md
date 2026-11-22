# 退出码契约（Exit Code Contract）

> 适用对象：`scripts/` 下所有脚本。
> 实现：`scripts/_codes.py`（纯标准库，可被任何脚本安全导入）。
> 自检：`python scripts/_codes.py` 打印完整码表；`python tests/test_codes.py` 跑 29 条单元测试。

## 为什么需要一个契约

这些脚本的真实用法不是人手一个个敲，而是被上层编排：`cross_platform_search.py` 调它们、maigret 适配器调它们、CI 调它们。

只回 `0/1` 的工具有一个结构性缺陷：**上层无法区分「没登录」和「被封 IP」**。两者都表现成 `1`，于是编排层只能一律重试——而对着风控重试，等于主动把 IP 打进黑名单。这是最贵的失败，且完全没有产出。

把失败面拆成可判定的几类之后，上层才能谈策略：这一类退避、那一类换凭据、另一类直接转人工。

## 码表

| 码 | 契约名 | 含义 | 上层该怎么办 |
|---:|---|---|---|
| 0 | `ok` | 全部成功 | 继续 |
| 2 | `usage` | 命令行参数或输入不合法（含无输入） | 不重试，修调用 |
| 3 | `partial` | 有结果，但有失败或未命中项 | 不重试，逐条看 JSONL |
| 10 | `not_authenticated` | 缺登录态（cookie / token / API key） | 换凭据，或转人工 |
| 11 | `verification_required` | 被验证码、人机校验、风控页挡住 | 转人工，**不要连击** |
| 12 | `rate_limited` | 触发频率限制 | 退避重试 |
| 13 | `ip_blocked` | 地域 / IP 黑名单，含地理围栏 | 换出口 IP 再重试 |
| 14 | `signature_error` | 签名或参数校验失败（`x-s` / `a_bogus` / `w_rid`） | 更新签名实现，重试无意义 |
| 15 | `unsupported` | 接口不存在，或平台不支持该操作 | 不重试，改路径 |
| 16 | `upstream_error` | 上游返回非预期内容（5xx、HTML 风控页、结构变更） | 可重试，但先看日志 |
| 17 | `dependency_missing` | 缺第三方包 / 缺数据文件 / 数据文件损坏 | 不重试，装依赖或换数据 |

三条设计约束：

1. **`usage` 取 2，不另立新值**。`argparse` 自己遇到参数错误就退 2，我们再造一个 3 之外的「参数错」只会让上层多写一条分支。
2. **不许用 1**。`1` 保留给「未分类」，它的出现本身就说明某个脚本还没接契约——这比它代表某种具体失败更有用。
3. **`partial` 是正常结果，不是失败**。批量输入里有一条解析不出来，不该让整条管道挂掉。

## 语义边界：进程码判「输入可解析」，数据质量写在 record 里

这是最容易做错的地方，单独写一节。

以 `id_card_parse.py` 为例：`11010519491231002X` 是格式与校验位都合法的证件，但它的出生年份是 1949，而内置区划快照的起点是 **2002-12**。按「地址没解析到区县就算失败」判，几乎每一张 2002 年前赋号的老证都会让进程以非 0 退出——编排层面对成批历史数据将完全不可用。

所以判定拆成两层：

- **进程退出码**：`usable == total` 才回 `0`，否则 `3`。`usable` 只看「这条记录能不能被解析」（`valid_format and checksum_ok`，手机号是 `match != "invalid"`）。
- **记录内容**：`level`（`address` / `checksum` / `format`）、`warnings[]`、`address.status`（`resolved` / `ambiguous` / `unresolved`）、`confidence.level` 表达精度与不确定性。

stderr 摘要行同时给出两个计数，例如：

```
共 2 条，结构与校验通过 1，地址已定位到区县 0（数据集 gb2260-snapshots+china-division@2.7.0，…）
```

这样「拿到 0 但地址没定位」和「拿到 3 有坏数据」一眼可分。

## 结构化错误行

所有 `emit()` 输出都写成 **stderr 上的一行 JSON**，不是人类可读的长句：

```json
{"exit": 13, "error": {"code": "ip_blocked", "message": "ip 被封", "hint": "换出口 IP"}}
```

字段约定：

- `exit`：与进程退出码一致，方便只解析 stderr 的调用方
- `error.code`：契约名（`name_of()` 得到），未登记的码回 `"unknown"`
- `error.message`：中文短语，给人看
- `error.*`：`emit(code, msg, **extra)` 的额外字段，放机器要的上下文（`path`、`input`、`hint`）

用 `emit()` 而不要手写 `json.dumps`，原因是它保证三件事：单行、`ensure_ascii=False`、`flush`。

## `classify_http()`：把 HTTP 响应对到码

适配器不必每次都自己写判定，直接调：

```python
from _codes import classify_http, Exit
code = classify_http(resp.status_code, dict(resp.headers), resp.text[:2048])
if code != Exit.OK:
    return emit(code, f"{url} 分类为 {code}")
```

判定顺序（前者优先）：

| 条件 | 判为 |
|---|---|
| `461` / `471` | `verification_required`（小红书风控） |
| `412` | `verification_required`（B 站拦截） |
| `429` | `rate_limited` |
| `>= 500` | `upstream_error` |
| `401` / `403` + 地区/封禁词 | `ip_blocked` |
| `401` / `403` | `not_authenticated` |
| body 含验证码/人机验证/`captcha`/访问过于频繁 | `verification_required` |
| body 含 `-799`/请求被拦截/签名/`x-s`/`a_bogus`/`w_rid`/非法请求 | `signature_error` |
| body 含地区限制/`ip banned`/`banned` | `ip_blocked` |
| body 含未登录/`csrf`/`token expired`/`cookie` | `not_authenticated` |
| 期望 JSON 但 `Content-Type` 含 `html` | `upstream_error` |
| `404` | `unsupported` |
| `200` | `ok` |
| 其余 | `upstream_error` |

两个必须记住的实测事实：

- **B 站搜索带 cookie 会返回 200，但 body 是风控 HTML 页**。只看状态码会把它当成功，于是把风控页当数据存进结果集。所以要看 `Content-Type`，`classify_http` 默认 `expect_json=True` 就是为这个。
- **`/x/space/acc/info` 首次请求即回 `code:-799`**，这是「没签名」，不是真的频率限制。判成 `rate_limited` 去退避重试会白等。

抓 HTML 页面（比如微博那个一次性窗口）要显式传 `expect_json=False`，否则正常的 HTML 会被误判成 `upstream_error`。

## 重试策略

```python
from _codes import is_retryable
if is_retryable(code):
    time.sleep(min(30, 5 * 2 ** (n - 1)))
```

表里只有三个码标为可重试：`rate_limited`、`ip_blocked`、`upstream_error`。

其余一律不自动重试，理由各不同：

- `not_authenticated` / `verification_required` / `signature_error`：重试不会改变结果，只会加重风控
- `usage` / `unsupported` / `dependency_missing`：是调用方或环境的问题，重试是浪费
- `partial`：已经拿到结果，重试等于重复劳动

## 编码：`force_utf8_streams()`

Windows 控制台默认 cp936。`print("✅")` 会直接抛：

```
UnicodeEncodeError: 'gbk' codec can't encode character '\u274c' in position 2: illegal multibyte sequence
```

JSONL 是给机器读的，输出必须与终端代码页无关。所以每个脚本的 `main()` 第一行调一次：

```python
def main():
    force_utf8_streams()
    ...
```

`errors="backslashreplace"` 是兜底：宁可输出 `\u274c` 的字面转义，也不要因为一个 emoji 让整条管道崩掉。

## 新增脚本的接入模板

```python
#!/usr/bin/env python3
"""
English Name — 一句话中文说明

用法：
    python script_name.py --arg value

注意：
    ...
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _codes import Exit, emit, force_utf8_streams


def main() -> int:
    force_utf8_streams()
    parser = argparse.ArgumentParser(description="...")
    args = parser.parse_args()

    try:
        data = Path(args.input).read_text(encoding="utf-8")
    except OSError as exc:
        return emit(Exit.USAGE, f"读不到 {args.input}：{exc}", path=args.input)

    if not data.strip():
        return emit(Exit.USAGE, "没有输入")

    ...
    return Exit.OK if all_ok else Exit.PARTIAL


if __name__ == "__main__":
    sys.exit(main())
```

五个小脚本（`qq_avatar.py` 等）在 `scripts/` 同级，`sys.path` 已是脚本目录，但仍建议照抄上面这段引导，保证从任意 cwd 调用都能导入。

## 与上游既有行为的差异

本次接入改动了两处会被感知到的行为：

1. `id_card_parse.py` / `phone_locate.py` 原来「地址或归属地没解析出来」会退非 0，现在退 `0`，不确定性写进 record。批处理历史数据不再中断。
2. 原来缺依赖、数据文件损坏一律退 `1`，现在分别是 `17`，且 stderr 是结构化 JSON 行。

如果你的调用方依赖旧的 `1`，改成判断 `!= 0` 或读 `error.code` 即可；`1` 在契约里已不再由任何脚本产生。

---

MIT License.
