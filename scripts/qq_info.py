#!/usr/bin/env python3
"""
QQ Info Fetcher — 获取QQ详细信息（注册时间、等级、昵称等）

支持两种方式：
  1. 第三方API精确查询（需API key）
  2. 号段估算（无需任何key，精度为年份）

用法：
    # API精确查询（需配置key）
    python qq_info.py --qq 123456789 --key YOUR_API_KEY

    # 号段估算（无需key）
    python qq_info.py --qq 123456789 --estimate

    # 同时获取头像
    python qq_info.py --qq 123456789 --estimate --avatar

配置API Key：
    方式一：命令行 --key 参数
    方式二：环境变量 QQ_API_KEY
    方式三：当前目录 .env 文件中写入 QQ_API_KEY=xxx

支持的API（按优先级）：
    - 小渡API (openapi.dwo.cc)：免费，精确到秒，需注册获取ckey
      注册地址：https://api.dwo.cc/api/137
    - 76.al：免费，需注册获取key
      文档地址：https://api.76.al/doc/21
    - 52api.cn：免费10000次/天，需实名
      文档地址：https://www.52api.cn/doc/65
"""

import argparse
import json
import os
import sys
from datetime import datetime


# ============ 号段估算（无需API） ============

DIGIT_RANGES = [
    (5, "1999年", "最早批次"),
    (6, "2000-2001年", "5位号用完后推出"),
    (7, "2001-2003年", "QQ用户快速增长期"),
    (8, "2003-2005年", "大规模普及期"),
    (9, "2005-2009年", "2006年开始流行"),
    (10, "2009年至今", "2008年底开始发放"),
    (11, "2012年至今", "部分新注册用户"),
]


def estimate_registration(qq: str) -> dict:
    """根据QQ号位数估算注册时间"""
    digits = len(qq)

    # 找到匹配的号段
    period = "未知"
    note = ""
    for d, p, n in DIGIT_RANGES:
        if digits == d:
            period = p
            note = n
            break
    elif digits >= 12:
        period = "2015年以后"
        note = "较新的QQ号"

    return {
        "qq": qq,
        "digits": digits,
        "method": "号段估算",
        "estimated_period": period,
        "note": note,
        "accuracy": "粗略（±1-2年）",
        "warning": "QQ号为随机分配，回收号/靓号可能偏离此估算",
    }


# ============ 第三方API查询 ============

def query_dwo_api(qq: str, key: str) -> dict:
    """查询小渡API (openapi.dwo.cc)"""
    import requests

    url = "https://openapi.dwo.cc/api/fh_zcsj"
    params = {"qq": qq, "key": key}

    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()

        if data.get("code") == 200 or data.get("success"):
            result = data.get("data", data.get("result", {}))
            return {
                "qq": qq,
                "method": "小渡API",
                "nickname": result.get("nickname", ""),
                "level": result.get("level", ""),
                "qid": result.get("qid", ""),
                "signature": result.get("signature", ""),
                "registration_time": result.get("registration_time", ""),
                "registration_days": result.get("registration_days", ""),
                "avatar_last_modified": result.get("avatar_last_modified", ""),
                "vip_status": result.get("vip_status", ""),
                "active_days": result.get("active_days", ""),
                "raw": data,
            }
        else:
            return {"qq": qq, "method": "小渡API", "error": data.get("msg", data.get("message", "未知错误")), "raw": data}
    except Exception as e:
        return {"qq": qq, "method": "小渡API", "error": str(e)}


def query_76al_api(qq: str, key: str) -> dict:
    """查询 76.al API"""
    import requests

    url = "https://api.76.al/api/qq/query"
    params = {"qq": qq, "key": key}

    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()

        if data.get("code") == 200 or data.get("success"):
            result = data.get("data", data)
            return {
                "qq": qq,
                "method": "76.al API",
                "registration_time": result.get("registration_time", result.get("reg_time", "")),
                "qq_age": result.get("qq_age", result.get("q龄", "")),
                "raw": data,
            }
        else:
            return {"qq": qq, "method": "76.al API", "error": data.get("msg", "查询失败"), "raw": data}
    except Exception as e:
        return {"qq": qq, "method": "76.al API", "error": str(e)}


def query_api(qq: str, key: str = None, api: str = "dwo") -> dict:
    """统一API查询入口"""
    # 从环境变量或.env文件读取key
    if not key:
        key = os.environ.get("QQ_API_KEY", "")
        if not key:
            try:
                with open(".env") as f:
                    for line in f:
                        if line.strip().startswith("QQ_API_KEY="):
                            key = line.strip().split("=", 1)[1].strip("\"'")
                            break
            except FileNotFoundError:
                pass

    if not key:
        return {
            "qq": qq,
            "error": "未提供API key。请使用 --key 参数、设置 QQ_API_KEY 环境变量、或在 .env 文件中配置。"
                     "\n注册获取key: https://api.dwo.cc/api/137",
        }

    if api == "76al":
        return query_76al_api(qq, key)
    else:
        return query_dwo_api(qq, key)


# ============ 头像获取 ============

def fetch_avatar(qq: str) -> dict:
    """获取QQ头像信息"""
    import requests

    url = f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200 and len(resp.content) > 1000:
            last_modified = resp.headers.get("Last-Modified", "")
            path = f"qq_avatar_{qq}.jpg"
            with open(path, "wb") as f:
                f.write(resp.content)
            return {
                "avatar_url": url,
                "avatar_size": len(resp.content),
                "avatar_last_modified": last_modified,
                "avatar_saved_to": path,
            }
        else:
            return {"avatar_url": url, "avatar_exists": False}
    except Exception as e:
        return {"avatar_error": str(e)}


# ============ 输出格式化 ============

def print_result(result: dict):
    """格式化输出结果"""
    print(f"\n{'=' * 50}")

    if "error" in result:
        print(f"  QQ: {result['qq']}")
        print(f"  ❌ {result['error']}")
        if "raw" in result:
            print(f"  原始响应: {json.dumps(result['raw'], ensure_ascii=False, indent=2)}")
        return

    if result.get("method") == "号段估算":
        print(f"  QQ号段估算")
        print(f"{'─' * 50}")
        print(f"  QQ号:     {result['qq']} ({result['digits']}位)")
        print(f"  估算时段: {result['estimated_period']}")
        print(f"  说明:     {result['note']}")
        print(f"  精度:     {result['accuracy']}")
        print(f"  ⚠️  {result['warning']}")
    else:
        print(f"  QQ详细信息 (来源: {result.get('method', 'API')})")
        print(f"{'─' * 50}")
        fields = [
            ("昵称", "nickname"),
            ("等级", "level"),
            ("QID", "qid"),
            ("签名", "signature"),
            ("注册时间", "registration_time"),
            ("注册天数", "registration_days"),
            ("头像修改时间", "avatar_last_modified"),
            ("会员状态", "vip_status"),
            ("活跃天数", "active_days"),
        ]
        for label, key in fields:
            val = result.get(key)
            if val:
                print(f"  {label}: {val}")

    # 头像信息
    avatar_fields = [("avatar_url", "头像URL"), ("avatar_size", "头像大小"),
                     ("avatar_last_modified", "头像更新"), ("avatar_saved_to", "已保存")]
    has_avatar = any(result.get(k) for k, _ in avatar_fields)
    if has_avatar:
        print(f"\n  头像信息:")
        for key, label in avatar_fields:
            val = result.get(key)
            if val:
                if key == "avatar_size":
                    print(f"    {label}: {val:,} bytes ({val/1024:.1f} KB)")
                else:
                    print(f"    {label}: {val}")

    print(f"{'=' * 50}")


def main():
    parser = argparse.ArgumentParser(
        description="QQ信息查询工具（注册时间、等级、昵称等）",
        epilog="示例:\n"
               "  python qq_info.py --qq 123456789 --estimate          # 号段估算\n"
               "  python qq_info.py --qq 123456789 --key YOUR_KEY      # API精确查询\n"
               "  python qq_info.py --qq 123456789 --key YOUR_KEY --avatar  # 含头像\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--qq", required=True, help="QQ号")
    parser.add_argument("--key", help="API key（也可通过 QQ_API_KEY 环境变量设置）")
    parser.add_argument("--api", choices=["dwo", "76al"], default="dwo", help="API源（默认dwo）")
    parser.add_argument("--estimate", action="store_true", help="使用号段估算（无需API key）")
    parser.add_argument("--avatar", action="store_true", help="同时获取头像")
    parser.add_argument("--json", dest="json_output", help="结果保存为JSON")
    args = parser.parse_args()

    result = {}

    # 号段估算
    if args.estimate:
        result = estimate_registration(args.qq)
        print_result(result)

    # API精确查询
    if not args.estimate or args.key:
        api_result = query_api(args.qq, args.key, args.api)
        print_result(api_result)
        result = api_result

    # 头像
    if args.avatar:
        avatar_info = fetch_avatar(args.qq)
        result.update(avatar_info)
        print_result(result)

    # 保存JSON
    if args.json_output:
        with open(args.json_output, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n[+] 结果已保存: {args.json_output}")


if __name__ == "__main__":
    main()
