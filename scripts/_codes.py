#!/usr/bin/env python3
"""
Exit Code Contract — 脚本退出码契约（scripts/ 下所有脚本共用）

为什么要统一
    这些脚本经常被上层编排（cross_platform_search.py、maigret 适配器、CI）。
    只回 0/1 的工具无法区分「没登录」和「被封 IP」，上层只能把所有失败一起重试，
    等于主动放大风控。把失败面拆成可判定的几类，编排层才谈得上策略。

退出码表
    0   ok                    全部成功
    2   usage                 命令行参数 / 输入不合法（含无输入），重试无意义
    3   partial               有结果但有失败或未命中项，细看 JSONL
    10  not_authenticated     缺登录态（cookie / token / API key）
    11  verification_required 被验证码、人机校验、风控页挡住
    12  rate_limited          触发频率限制
    13  ip_blocked            地域 / IP 黑名单（含地理围栏）
    14  signature_error       签名或参数校验失败（x-s / a_bogus / w_rid）
    15  unsupported           接口不存在，或平台不支持该操作
    16  upstream_error        上游返回非预期内容（5xx、HTML 风控页、结构变更）
    17  dependency_missing    缺第三方包 / 缺数据文件 / 数据文件损坏

用法
    from _codes import Exit, classify_http, emit

    if not db_path.exists():
        return emit(Exit.DEPENDENCY_MISSING, f"找不到 {db_path}")

    code = classify_http(resp.status_code, dict(resp.headers), resp.text[:2048])

注意
    - 与 argparse 对齐：它自己用 2 报参数错误，所以 usage 也取 2，不另立新值。
    - 不要用 1。1 代表「未分类」，出现即说明某处没接契约。
    - 上层重试策略：12 / 13 退避重试；10 / 11 / 14 换凭据或转人工；其余不重试。
    - 本模块不依赖第三方包，只用标准库，可被任何脚本安全导入。
"""

from __future__ import annotations

import json
import sys
from typing import Any


class Exit:
    """退出码。用类属性而不是 enum，方便直接与 int 比较、写进 JSON。"""

    OK = 0
    USAGE = 2
    PARTIAL = 3
    NOT_AUTHENTICATED = 10
    VERIFICATION_REQUIRED = 11
    RATE_LIMITED = 12
    IP_BLOCKED = 13
    SIGNATURE_ERROR = 14
    UNSUPPORTED = 15
    UPSTREAM_ERROR = 16
    DEPENDENCY_MISSING = 17


NAMES: dict[int, str] = {
    Exit.OK: "ok",
    Exit.USAGE: "usage",
    Exit.PARTIAL: "partial",
    Exit.NOT_AUTHENTICATED: "not_authenticated",
    Exit.VERIFICATION_REQUIRED: "verification_required",
    Exit.RATE_LIMITED: "rate_limited",
    Exit.IP_BLOCKED: "ip_blocked",
    Exit.SIGNATURE_ERROR: "signature_error",
    Exit.UNSUPPORTED: "unsupported",
    Exit.UPSTREAM_ERROR: "upstream_error",
    Exit.DEPENDENCY_MISSING: "dependency_missing",
}

# 重试策略表：True = 值得退避重试
RETRYABLE: dict[int, bool] = {
    Exit.RATE_LIMITED: True,
    Exit.IP_BLOCKED: True,
    Exit.UPSTREAM_ERROR: True,
}


def name_of(code: int) -> str:
    """退出码 → 契约名。未登记的值返回 unknown，便于上层告警。"""
    return NAMES.get(code, "unknown")


def is_retryable(code: int) -> bool:
    return RETRYABLE.get(code, False)


def force_utf8_streams() -> None:
    """把 stdout/stderr 钉成 UTF-8。

    Windows 控制台代码页（cp936 等）会把中文和 emoji 按 GBK 写进管道：轻则下游
    按 UTF-8 读就炸，重则 print 直接抛 UnicodeEncodeError。JSONL 是给机器读的，
    必须与终端设置无关。每次 ``main()`` 开头调用一次。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # 被重定向成非 TextIOWrapper 时跳过
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (ValueError, OSError):
            pass


def emit(code: int, message: str, **extra: Any) -> int:
    """往 stderr 写一行结构化错误，并返回退出码。

    错误统一成单行 JSON，编排层用 json.loads 就能取到 code 与字段，
    不必再解析人类可读的中文提示。
    """
    payload: dict[str, Any] = {
        "error": {"code": name_of(code), "message": message},
    }
    if extra:
        payload["error"].update(extra)
    sys.stderr.write(json.dumps({"exit": code, **payload}, ensure_ascii=False) + "\n")
    sys.stderr.flush()
    return code


_VERIFY_MARKERS = (
    "验证码",
    "人机验证",
    "安全验证",
    "请完成验证",
    "captcha",
    "are you a robot",
    "访问过于频繁",
    "operation too frequent",
    "unusual traffic",
)

_AUTH_MARKERS = (
    "请先登录",
    "登录后",
    "未登录",
    "not logged in",
    "login required",
    "unauthorized",
    "invalid csrf",
    "csrf",
    "token expired",
    "cookie",
)

_BLOCK_MARKERS = (
    "地区限制",
    "仅限中国大陆",
    "geo",
    "region",
    "forbidden by region",
    "ip 已被封",
    "ip banned",
    "banned",
    "服务不可用",
)

_SIGN_MARKERS = (
    "-799",
    "请求被拦截",
    "签名",
    "signature",
    "非法请求",
    "invalid request",
    "x-s",
    "a_bogus",
    "w_rid",
)


def classify_http(
    status: int,
    headers: dict | None = None,
    body: str | None = None,
    expect_json: bool = True,
) -> int:
    """把一次 HTTP 响应对到退出码。

    这是启发式分类，不是协议：目的是让上层能区分「该退避」和「该换凭据」。
    调用方拿到 0 之外的码时，仍应把 body 片段一起记日志，便于人工复核。

    ``expect_json`` 默认 True：该接口本应回 JSON，却回了 ``text/html``，几乎一定是
    风控页或被重定向到登录页（B 站搜索带 cookie 时就是这样，状态码仍是 200）。
    抓 HTML 页面的场景显式传 False，避免误判。
    """
    body_l = (body or "").lower()
    ctype = ""
    if headers:
        for key, value in headers.items():
            if key.lower() == "content-type":
                ctype = str(value).lower()
                break

    if status in (461, 471):  # 小红书风控
        return Exit.VERIFICATION_REQUIRED
    if status == 412:  # B 站等：请求被拦截
        return Exit.VERIFICATION_REQUIRED
    if status == 429:
        return Exit.RATE_LIMITED
    if status >= 500:
        return Exit.UPSTREAM_ERROR
    if status in (401, 403):
        if any(m in body_l for m in _BLOCK_MARKERS):
            return Exit.IP_BLOCKED
        return Exit.NOT_AUTHENTICATED

    if any(m in body_l for m in _VERIFY_MARKERS):
        return Exit.VERIFICATION_REQUIRED
    if any(m in body_l for m in _SIGN_MARKERS):
        return Exit.SIGNATURE_ERROR
    if any(m in body_l for m in _BLOCK_MARKERS):
        return Exit.IP_BLOCKED
    if any(m in body_l for m in _AUTH_MARKERS):
        return Exit.NOT_AUTHENTICATED

    # 期望 JSON 却拿到 HTML：几乎一定是风控页或被重定向到登录页
    if expect_json and ctype and "html" in ctype:
        return Exit.UPSTREAM_ERROR
    if status == 404:
        return Exit.UNSUPPORTED
    if status == 200:
        return Exit.OK
    return Exit.UPSTREAM_ERROR


def main(argv: list[str]) -> int:
    """命令行自检：打印契约表，方便文档与排查。"""
    print(json.dumps(
        {
            "exit_codes": {str(k): v for k, v in sorted(NAMES.items())},
            "retryable": {str(k): v for k, v in sorted(RETRYABLE.items())},
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
