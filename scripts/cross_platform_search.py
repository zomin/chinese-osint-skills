#!/usr/bin/env python3
"""
Cross-Platform Username Search — 跨平台用户名批量搜索

结合 Maigret + Sherlock + QQ/B站API，一次性搜索用户名在多个平台的存在情况。

用法：
    python cross_platform_search.py --nickname "target_username"
    python cross_platform_search.py --nickname "目标昵称" --json results.json
    python cross_platform_search.py --nickname "target" --skip-maigret  # 跳过Maigret（较慢）
"""

import argparse
import json
import subprocess
import sys
import time
import requests
from datetime import datetime


def check_qq_avatar(qq: str) -> dict:
    """检查QQ号是否有头像（判断QQ号是否存在）"""
    url = f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"
    try:
        resp = requests.get(url, timeout=10)
        return {
            "platform": "QQ",
            "url": url,
            "exists": resp.status_code == 200 and len(resp.content) > 1000,
            "size": len(resp.content),
        }
    except Exception as e:
        return {"platform": "QQ", "exists": False, "error": str(e)}


def search_bilibili(username: str) -> dict:
    """B站用户搜索"""
    try:
        # 先获取cookie
        session = requests.Session()
        session.get("https://www.bilibili.com", timeout=10)

        resp = session.get(
            "https://api.bilibili.com/x/web-interface/search/type",
            params={"search_type": "bili_user", "keyword": username},
            headers={"Referer": "https://www.bilibili.com"},
            timeout=10,
        )
        data = resp.json()

        results = []
        if data.get("data") and data["data"].get("result"):
            for user in data["data"]["result"]:
                results.append({
                    "mid": user.get("mid"),
                    "uname": user.get("uname"),
                    "sign": user.get("usign", ""),
                    "fans": user.get("fans", 0),
                    "videos": user.get("videos", 0),
                    "level": user.get("level"),
                    "url": f"https://space.bilibili.com/{user.get('mid')}",
                })

        return {
            "platform": "Bilibili",
            "results": results,
            "count": len(results),
        }
    except Exception as e:
        return {"platform": "Bilibili", "results": [], "error": str(e)}


def run_maigret(username: str) -> dict:
    """运行Maigret跨平台搜索"""
    print(f"\n[*] 运行 Maigret（210+平台，可能需要1-3分钟）...")
    try:
        result = subprocess.run(
            ["maigret", username, "--no-color", "--timeout", "15", "--top-sites", "200",
             "--json", "--report", f"/tmp/maigret_{username}"],
            capture_output=True, text=True, timeout=180
        )

        # 尝试读取JSON报告
        import glob
        json_files = glob.glob(f"/tmp/maigret_{username}*.json")
        if json_files:
            with open(json_files[0], 'r') as f:
                report = json.load(f)
                sites = report.get("sites", {})
                found = {
                    name: data.get("url_user", "")
                    for name, data in sites.items()
                    if data.get("status") == "claimed"
                }
                return {
                    "platform": "Maigret (210+)",
                    "found_count": len(found),
                    "found": found,
                }

        return {"platform": "Maigret", "found_count": 0, "found": {}}
    except FileNotFoundError:
        print("[-] Maigret 未安装。运行: pip install maigret")
        return {"platform": "Maigret", "error": "not installed"}
    except subprocess.TimeoutExpired:
        print("[-] Maigret 超时")
        return {"platform": "Maigret", "error": "timeout"}
    except Exception as e:
        return {"platform": "Maigret", "error": str(e)}


def run_sherlock(username: str) -> dict:
    """运行Sherlock跨平台搜索"""
    print(f"\n[*] 运行 Sherlock（400+平台）...")
    try:
        result = subprocess.run(
            ["sherlock", username, "--no-color", "--timeout", "10", "--print-found"],
            capture_output=True, text=True, timeout=120
        )
        found = {}
        for line in result.stdout.split('\n'):
            if '+' in line and 'http' in line:
                parts = line.strip().split()
                url = [p for p in parts if p.startswith('http')]
                name = parts[0] if parts else "unknown"
                if url:
                    found[name] = url[0]

        return {
            "platform": "Sherlock (400+)",
            "found_count": len(found),
            "found": found,
        }
    except FileNotFoundError:
        print("[-] Sherlock 未安装。运行: pip install sherlock-project")
        return {"platform": "Sherlock", "error": "not installed"}
    except subprocess.TimeoutExpired:
        print("[-] Sherlock 超时")
        return {"platform": "Sherlock", "error": "timeout"}
    except Exception as e:
        return {"platform": "Sherlock", "error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="跨平台用户名批量搜索")
    parser.add_argument("--nickname", required=True, help="目标用户名/昵称")
    parser.add_argument("--json", dest="json_output", help="结果保存为JSON")
    parser.add_argument("--skip-maigret", action="store_true", help="跳过Maigret（较慢）")
    parser.add_argument("--skip-sherlock", action="store_true", help="跳过Sherlock")
    args = parser.parse_args()

    start_time = time.time()
    results = {
        "query": args.nickname,
        "timestamp": datetime.now().isoformat(),
        "tools": []
    }

    print(f"=" * 60)
    print(f"  跨平台用户名搜索: {args.nickname}")
    print(f"=" * 60)

    # 1. B站（最快，直接API）
    print(f"\n[*] 搜索 B站...")
    bili = search_bilibili(args.nickname)
    results['tools'].append(bili)
    if bili.get('count', 0) > 0:
        print(f"[+] B站找到 {bili['count']} 个用户:")
        for u in bili['results'][:5]:
            print(f"    - {u['uname']} (粉丝:{u['fans']}) {u['url']}")
    else:
        print(f"[-] B站未找到")

    # 2. Maigret
    if not args.skip_maigret:
        mg = run_maigret(args.nickname)
        results['tools'].append(mg)
        if mg.get('found_count', 0) > 0:
            print(f"\n[+] Maigret 找到 {mg['found_count']} 个平台:")
            for name, url in list(mg['found'].items())[:10]:
                print(f"    - {name}: {url}")
        else:
            print(f"\n[-] Maigret 未找到")

    # 3. Sherlock
    if not args.skip_sherlock:
        sh = run_sherlock(args.nickname)
        results['tools'].append(sh)
        if sh.get('found_count', 0) > 0:
            print(f"\n[+] Sherlock 找到 {sh['found_count']} 个平台:")
            for name, url in list(sh['found'].items())[:10]:
                print(f"    - {name}: {url}")
        else:
            print(f"\n[-] Sherlock 未找到")

    elapsed = time.time() - start_time
    results['elapsed_seconds'] = round(elapsed, 1)

    # 汇总
    total_found = sum(
        t.get('found_count', len(t.get('found', {})))
        for t in results['tools']
        if 'found' in t or 'found_count' in t
    )
    print(f"\n{'=' * 60}")
    print(f"  搜索完成 | 耗时 {elapsed:.1f}s | 共找到 {total_found} 个平台")
    print(f"{'=' * 60}")

    # 保存JSON
    if args.json_output:
        with open(args.json_output, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"[+] 结果已保存: {args.json_output}")


if __name__ == "__main__":
    main()
