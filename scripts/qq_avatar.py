#!/usr/bin/env python3
"""
QQ Avatar Fetcher — 获取QQ头像并提取元数据

用法：
    python qq_avatar.py --qq 123456789
    python qq_avatar.py --qq 123456789 --size 100  # 小尺寸
    python qq_avatar.py --qq 123456789 --save avatar.jpg

注意：QQ头像CDN无防盗链限制，可直接访问。
"""

import argparse
import requests
import sys
from datetime import datetime


QQ_AVATAR_URL = "https://q1.qlogo.cn/g?b=qq&nk={qq}&s={size}"
QZONE_AVATAR_URL = "https://qlogo3.store.qq.com/qzone/{qq}/{qq}/640"

SIZE_MAP = {
    "40": 1,    # 40x40
    "100": 2,   # 100x100
    "140": 3,   # 140x140
    "640": 640, # 640x640 (原图)
}


def fetch_qq_avatar(qq: str, size: str = "640", save_path: str = None):
    """获取QQ头像"""
    size_code = SIZE_MAP.get(size, 640)
    url = QQ_AVATAR_URL.format(qq=qq, size=size_code)

    print(f"[*] 获取QQ头像: {qq}")
    print(f"    URL: {url}")

    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "unknown")
        content_length = len(resp.content)
        last_modified = resp.headers.get("Last-Modified", "unknown")

        print(f"\n[+] 头像信息:")
        print(f"    类型: {content_type}")
        print(f"    大小: {content_length} bytes ({content_length/1024:.1f} KB)")
        print(f"    最后更新: {last_modified}")

        if save_path:
            with open(save_path, "wb") as f:
                f.write(resp.content)
            print(f"\n[+] 已保存到: {save_path}")
        else:
            default_path = f"qq_avatar_{qq}.jpg"
            with open(default_path, "wb") as f:
                f.write(resp.content)
            print(f"\n[+] 已保存到: {default_path}")

        return True

    except requests.RequestException as e:
        print(f"\n[-] 获取失败: {e}")
        return False


def fetch_qzone_avatar(qq: str, save_path: str = None):
    """获取QQ空间头像（可能与QQ头像不同）"""
    url = QZONE_AVATAR_URL.format(qq=qq)

    print(f"\n[*] 尝试获取QQ空间头像: {qq}")
    print(f"    URL: {url}")

    try:
        resp = requests.get(url, timeout=10, allow_redirects=True)

        if resp.status_code == 200 and len(resp.content) > 1000:
            path = save_path or f"qzone_avatar_{qq}.jpg"
            with open(path, "wb") as f:
                f.write(resp.content)
            print(f"[+] QQ空间头像已保存: {path} ({len(resp.content)} bytes)")
            return True
        else:
            print(f"[-] QQ空间头像不可用 (HTTP {resp.status_code}, {len(resp.content)} bytes)")
            return False
    except requests.RequestException as e:
        print(f"[-] 获取失败: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="QQ头像获取工具")
    parser.add_argument("--qq", required=True, help="QQ号")
    parser.add_argument("--size", default="640", choices=["40", "100", "140", "640"], help="头像尺寸")
    parser.add_argument("--save", help="保存路径")
    parser.add_argument("--qzone", action="store_true", help="同时获取QQ空间头像")
    args = parser.parse_args()

    fetch_qq_avatar(args.qq, args.size, args.save)

    if args.qzone:
        fetch_qzone_avatar(args.qq)


if __name__ == "__main__":
    main()
