#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""
Exit Code Contract Test — 退出码契约的单元测试

用法：
    python tests/test_codes.py
    python -m unittest discover -s tests -p "test_*.py"

覆盖 scripts/_codes.py 的四件事：码表一致性、classify_http 的每条分支、
is_retryable 的重试语义、emit 输出的 JSON 形状。纯标准库，无需网络。

退出码：0 全部通过 / 1 有用例失败。

MIT License.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _codes import (  # noqa: E402
    NAMES,
    RETRYABLE,
    Exit,
    classify_http,
    emit,
    is_retryable,
    name_of,
)


class TestCodeTable(unittest.TestCase):
    """码表本身必须自洽：不能有 1，不能重复，名字不能空。"""

    def test_no_unclassified_one(self) -> None:
        # 1 保留给「没接契约」的情形，契约里任何码都不许是 1
        self.assertNotIn(1, NAMES)
        self.assertFalse(any(code == 1 for code in NAMES))

    def test_codes_are_unique(self) -> None:
        codes = [
            Exit.OK, Exit.USAGE, Exit.PARTIAL,
            Exit.NOT_AUTHENTICATED, Exit.VERIFICATION_REQUIRED, Exit.RATE_LIMITED,
            Exit.IP_BLOCKED, Exit.SIGNATURE_ERROR, Exit.UNSUPPORTED,
            Exit.UPSTREAM_ERROR, Exit.DEPENDENCY_MISSING,
        ]
        self.assertEqual(len(codes), len(set(codes)))
        for code in codes:
            self.assertIn(code, NAMES, f"码 {code} 未登记进 NAMES")

    def test_names_are_snake_case(self) -> None:
        for code, name in NAMES.items():
            self.assertEqual(name, name.lower(), f"{code} 的名字有大写")
            self.assertNotIn(" ", name, f"{code} 的名字含空格")

    def test_zero_is_ok(self) -> None:
        self.assertEqual(Exit.OK, 0)
        self.assertEqual(name_of(0), "ok")

    def test_usage_matches_argparse(self) -> None:
        # argparse 参数错误固定退出 2，契约必须与之对齐，否则上层无法区分
        self.assertEqual(Exit.USAGE, 2)
        self.assertEqual(name_of(2), "usage")

    def test_unknown_code_degrades(self) -> None:
        self.assertEqual(name_of(99), "unknown")
        self.assertEqual(name_of(1), "unknown")


class TestRetry(unittest.TestCase):
    def test_only_transient_codes_retry(self) -> None:
        retryable = {c for c in NAMES if RETRYABLE.get(c)}
        self.assertEqual(retryable, {Exit.RATE_LIMITED, Exit.IP_BLOCKED, Exit.UPSTREAM_ERROR})

    def test_is_retryable_matches_table(self) -> None:
        for code in NAMES:
            self.assertEqual(is_retryable(code), RETRYABLE.get(code, False))

    def test_is_retryable_unknown_is_false(self) -> None:
        self.assertFalse(is_retryable(99))


class TestClassifyHttp(unittest.TestCase):
    def test_success(self) -> None:
        self.assertEqual(classify_http(200, body='{"code":0}'), Exit.OK)

    def test_rate_limited(self) -> None:
        self.assertEqual(classify_http(429), Exit.RATE_LIMITED)

    def test_server_errors(self) -> None:
        for status in (500, 502, 503):
            self.assertEqual(classify_http(status), Exit.UPSTREAM_ERROR)

    def test_xhs_risk_control(self) -> None:
        # 小红书 461/471 是签名或风控失败，不是普通 4xx
        self.assertEqual(classify_http(461), Exit.VERIFICATION_REQUIRED)
        self.assertEqual(classify_http(471), Exit.VERIFICATION_REQUIRED)

    def test_bili_412(self) -> None:
        self.assertEqual(classify_http(412), Exit.VERIFICATION_REQUIRED)

    def test_not_found_unsupported(self) -> None:
        self.assertEqual(classify_http(404), Exit.UNSUPPORTED)

    def test_401_is_not_authenticated(self) -> None:
        self.assertEqual(classify_http(401), Exit.NOT_AUTHENTICATED)

    def test_403_default_not_authenticated(self) -> None:
        self.assertEqual(classify_http(403), Exit.NOT_AUTHENTICATED)

    def test_403_with_region_word_is_ip_blocked(self) -> None:
        self.assertEqual(classify_http(403, body="该内容因地区限制不可见"), Exit.IP_BLOCKED)
        self.assertEqual(classify_http(403, body="your ip has been banned"), Exit.IP_BLOCKED)

    def test_captcha_body_verification(self) -> None:
        for body in ("请完成验证码", "人机验证", "captcha required", "访问过于频繁"):
            self.assertEqual(
                classify_http(200, body=body), Exit.VERIFICATION_REQUIRED, body
            )

    def test_signature_words(self) -> None:
        for body in ("code:-799", "请求被拦截", "签名校验失败", "x-s missing", "a_bogus", "w_rid", "非法请求"):
            self.assertEqual(classify_http(200, body=body), Exit.SIGNATURE_ERROR, body)

    def test_not_logged_in_words(self) -> None:
        for body in ("未登录", "csrf token missing", "token expired"):
            self.assertEqual(classify_http(200, body=body), Exit.NOT_AUTHENTICATED, body)

    def test_html_instead_of_json(self) -> None:
        # B站带 cookie 搜索会返回 200 但 body 是风控 HTML —— 必须判上游异常而不是成功
        self.assertEqual(
            classify_http(200, headers={"Content-Type": "text/html; charset=utf-8"}, body="<html>"),
            Exit.UPSTREAM_ERROR,
        )

    def test_html_allowed_when_not_expecting_json(self) -> None:
        self.assertEqual(
            classify_http(
                200,
                headers={"Content-Type": "text/html"},
                body="<html>ok</html>",
                expect_json=False,
            ),
            Exit.OK,
        )

    def test_headers_case_insensitive(self) -> None:
        self.assertEqual(
            classify_http(200, headers={"content-type": "text/html"}, body="<html>"),
            Exit.UPSTREAM_ERROR,
        )

    def test_none_body_is_safe(self) -> None:
        self.assertEqual(classify_http(200), Exit.OK)
        self.assertEqual(classify_http(200, headers=None, body=None), Exit.OK)

    def test_other_status_upstream(self) -> None:
        for status in (400, 405, 302, 418):
            self.assertEqual(classify_http(status), Exit.UPSTREAM_ERROR, status)


class TestEmit(unittest.TestCase):
    def _capture(self, code: int, message: str, **extra) -> tuple[int, dict]:
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            returned = emit(code, message, **extra)
        line = buf.getvalue().strip()
        self.assertEqual(len(line.splitlines()), 1, "emit 只能输出一行")
        return returned, json.loads(line)

    def test_emit_shape(self) -> None:
        returned, payload = self._capture(Exit.IP_BLOCKED, "ip 被封")
        self.assertEqual(returned, Exit.IP_BLOCKED)
        self.assertEqual(payload["exit"], Exit.IP_BLOCKED)
        self.assertEqual(payload["error"]["code"], "ip_blocked")
        self.assertEqual(payload["error"]["message"], "ip 被封")

    def test_emit_extra_fields(self) -> None:
        _, payload = self._capture(Exit.USAGE, "参数不对", hint="看 --help", input="abc")
        self.assertEqual(payload["error"]["hint"], "看 --help")
        self.assertEqual(payload["error"]["input"], "abc")

    def test_emit_is_valid_json_even_with_unicode(self) -> None:
        _, payload = self._capture(Exit.PARTIAL, "共 2 条，1 条无法解析")
        self.assertIn("共 2 条", payload["error"]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
