#!/usr/bin/env python3
"""
Avatar Analysis — 通过像素分析判断头像类型（真人照片 / 插画 / 动漫）

原理：
  - unique_colors > 50000  → 照片级色彩丰富度
  - skin_pct > 20%         → 高肤色占比（真人）
  - edge_intensity > 8     → 照片级边缘细节

用法：
    python avatar_analysis.py --image avatar.jpg
    python avatar_analysis.py --url "https://example.com/avatar.jpg"
"""

import argparse
import sys

try:
    from PIL import Image, ImageFilter
    import numpy as np
except ImportError:
    print("请安装依赖: pip install pillow numpy")
    sys.exit(1)


def analyze_avatar(image_path: str = None, image_url: str = None):
    """分析头像类型"""

    if image_url:
        import requests
        import io
        resp = requests.get(image_url, timeout=15)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content))
    elif image_path:
        img = Image.open(image_path)
    else:
        print("请提供 --image 或 --url 参数")
        return

    # 统一转RGB
    if img.mode != "RGB":
        img = img.convert("RGB")

    arr = np.array(img)
    h, w, _ = arr.shape

    print(f"[*] 图片信息: {w}x{h}")

    # 1. 色彩丰富度
    unique_colors = len(np.unique(arr.reshape(-1, 3), axis=0))

    # 2. 肤色占比（近似：RGB范围内的肤色像素）
    skin_mask = (
        (arr[:, :, 0] > 150) & (arr[:, :, 0] < 255) &
        (arr[:, :, 1] > 80) & (arr[:, :, 1] < 200) &
        (arr[:, :, 2] > 50) & (arr[:, :, 2] < 180)
    )
    skin_pct = skin_mask.sum() / (h * w) * 100

    # 3. 边缘密度（Sobel-like）
    gray = np.array(img.convert("L"))
    edges = np.array(img.filter(ImageFilter.FIND_EDGES))
    edge_intensity = edges.mean()

    # 4. 亮度分布
    brightness = gray.mean()
    brightness_std = gray.std()

    print(f"\n[+] 分析结果:")
    print(f"    色彩丰富度: {unique_colors:,} unique colors")
    print(f"    肤色占比:   {skin_pct:.1f}%")
    print(f"    边缘强度:   {edge_intensity:.1f}")
    print(f"    平均亮度:   {brightness:.1f} (σ={brightness_std:.1f})")

    # 判断逻辑
    scores = {"photo": 0, "illustration": 0, "anime": 0}

    # 色彩判断
    if unique_colors > 50000:
        scores["photo"] += 2
    elif unique_colors > 10000:
        scores["illustration"] += 1
        scores["photo"] += 1
    else:
        scores["anime"] += 2

    # 肤色判断
    if skin_pct > 30:
        scores["photo"] += 2
    elif skin_pct > 15:
        scores["photo"] += 1
        scores["illustration"] += 1
    else:
        scores["anime"] += 1

    # 边缘判断
    if edge_intensity > 12:
        scores["photo"] += 2
    elif edge_intensity > 6:
        scores["illustration"] += 1
        scores["photo"] += 1
    else:
        scores["anime"] += 1

    # 结论
    best = max(scores, key=scores.get)
    type_names = {
        "photo": "真人照片",
        "illustration": "插画/设计图",
        "anime": "动漫/卡通"
    }
    confidence = scores[best] / sum(scores.values()) * 100

    print(f"\n[>] 判断结果: {type_names[best]} (置信度 {confidence:.0f}%)")
    print(f"    评分明细: 照片={scores['photo']} 插画={scores['illustration']} 动漫={scores['anime']}")

    if best == "photo" and confidence < 60:
        print(f"\n[!] 注意: 置信度较低，街拍/时尚照片可能被误判为二次元风格")
        print(f"    建议人工确认")

    return {"type": best, "confidence": confidence, "scores": scores}


def main():
    parser = argparse.ArgumentParser(description="头像类型分析工具")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--image", help="本地图片路径")
    group.add_argument("--url", help="图片URL")
    args = parser.parse_args()

    analyze_avatar(image_path=args.image, image_url=args.url)


if __name__ == "__main__":
    main()
