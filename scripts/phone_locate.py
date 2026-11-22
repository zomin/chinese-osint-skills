#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""
Phone Locator — 离线手机号号段归属地查询（phone.dat 二进制格式）

用法：
    python phone_locate.py 13800138000
    cat numbers.txt | python phone_locate.py --db /path/to/phone.dat
    python phone_locate.py --info

注意：
    结果是号码**发卡时**的归属地。2019-11-27 携号转网全国商用之后，号码可以
    在保留原号的前提下更换运营商、跨省迁转，不要据此推断"此人现在在哪"。
    号段库（phone.dat）需使用者自备，不随脚本分发。

用途
    用本地 ``phone.dat`` 号段库把 11 位手机号解析成**发卡时的**省/市/运营商，
    并显式给出数据版本、可信度与携号转网 caveat。纯离线，不联网、不查询任何
    在线接口，也不在模块里内置号码数据。

输入
    默认从 stdin 逐行读取号码（一行一个，``#`` 开头为注释，空行跳过；允许
    ``+86``/``0086``/空格/连字符，解析前统一规范化）。也可用 ``--input <file>``
    或位置参数。``--prefix 1380013`` 可直接查 7 位号段；``--info`` 只打印数据文件
    元信息。

输出
    stdout 输出 **JSONL**（一行一条记录）：
    ``input`` / ``valid`` / ``prefix`` / ``attribution{province,city,carrier}`` /
    ``match``(exact|none|invalid) / ``dataVersion`` / ``dataFile`` / ``dataMtime`` /
    ``confidence{level,reasons}`` / ``caveats[]`` / ``raw`` / ``source`` /
    ``observed_at``（ISO 8601）。诊断信息一律走 stderr。

依赖
    Python >= 3.8（已启用 ``from __future__ import annotations``；本机在 3.14
    实测通过，未在 3.8 实测），仅标准库。需要一份 ``phone.dat``：格式为业界通行的
    ``xluohome/phonedata`` 与 ``ls0f/phone`` 同构布局（头部 8 字节
    ``index_offset + version``，其后记录区，索引区在文件尾部，每条 9 字节）。
    数据文件不随脚本分发（版本频繁变动，且许可随来源不同）；用 ``--db``、
    环境变量 ``CN_PHONE_DB`` 或放到脚本同目录指定。

数据时效
    ``dataVersion`` 来自文件头 4 字节，通常只到年份（如 ``2019``）；``dataMtime``
    是该文件的本地修改时间。**号段库的年份不等于号码的真实签发时间**，两者都
    会随时间失效；每次解析都输出二者，便于判断结论是否过期。

速率
    纯本地查询（整个文件读入内存，二分查前缀），单条约毫秒级，无网络请求，
    不设速率限制。

Caveat
    * **2019-11-27 携号转网全国商用之后，号码可以在保留原号的前提下更换运营商、
      跨省迁转**。本模块给出的是发卡时的初始归属，不是号码当前所在省份或当前
      运营商；任何"此人现在在 X 省"的推断都不成立。
    * 数据版本早于查询对象可能发生的转网时，运营商字段的可信度更低（脚本会在
      ``confidence.reasons`` 里写明版本年份）。
    * 170/171/162/165/167 等虚拟运营商/转售号段，城市粒度常粗到省，主体也可能
      变更；物联网卡、行业卡不遵循个人号段归属规则。
    * 号段不在本地数据集时返回 ``match=none``、``confidence=low``，**不拿相邻号段
      猜**，也不回退到"大概在哪个省"。

MIT License.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _codes import Exit, emit, force_utf8_streams  # noqa: E402  退出码契约与本脚本同目录

__all__ = ["PhoneDat", "PhoneDatError", "LookupResult", "lookup", "find_database"]

HEADER_SIZE = 8
ENTRY_SIZE = 9

#: 携号转网全国商用日期
PORTABILITY_DATE = "2019-11-27"

CAVEAT_PORTABILITY = (
    "号段归属为发卡时的初始归属地与运营商；2019-11-27 携号转网全国商用后，"
    "号码可在保留原号的前提下跨运营商、跨省迁转，归属地/运营商不再等于当前状态"
)
CAVEAT_VIRTUAL = "170/171/162/165/167 等为虚拟运营商或转售号段，城市粒度常粗到省，且可能变更主体"
CAVEAT_IOT = "物联网卡、行业卡与部分政企号段不遵循个人号段归属规则"


class PhoneDatError(RuntimeError):
    """phone.dat 不存在、被截断或结构不合法。"""


@dataclass(frozen=True)
class LookupResult:
    number: str
    valid: bool
    prefix: str | None
    province: str | None
    city: str | None
    carrier: str | None
    match: str  # exact | none | invalid
    data_version: str
    data_file: str
    data_mtime: str | None
    confidence: dict[str, Any]
    caveats: list[str] = field(default_factory=list)
    raw: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "input": self.number,
            "valid": self.valid,
            "prefix": self.prefix,
            "attribution": {
                "province": self.province,
                "city": self.city,
                "carrier": self.carrier,
            },
            "match": self.match,
            "dataVersion": self.data_version,
            "dataFile": self.data_file,
            "dataMtime": self.data_mtime,
            "confidence": self.confidence,
            "caveats": list(self.caveats),
            "raw": self.raw,
        }


def normalize(number: str) -> str:
    """去掉空格、连字符、+86/86 前缀，只留数字。"""
    digits = "".join(ch for ch in str(number or "") if ch.isdigit())
    if len(digits) == 13 and digits.startswith("86"):
        digits = digits[2:]
    elif len(digits) == 15 and digits.startswith("0086"):
        digits = digits[4:]
    return digits


def _decode_component(raw: bytes) -> tuple[str, str | None]:
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding), None
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace"), "记录不是 UTF-8/GBK，已按 latin-1 解码"


class PhoneDat:
    """只读的 phone.dat 查询器；整个文件一次性读入内存（通常 2–3 MB）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise PhoneDatError(f"找不到数据文件：{self.path}")
        self._data = self.path.read_bytes()
        if len(self._data) < HEADER_SIZE:
            raise PhoneDatError(f"文件过短（{len(self._data)} 字节），不是 phone.dat")
        self.index_offset, version_raw = struct.unpack_from("<I4s", self._data, 0)
        if not HEADER_SIZE <= self.index_offset <= len(self._data):
            raise PhoneDatError(
                f"索引偏移 {self.index_offset} 越界（文件 {len(self._data)} 字节），可能不是 phone.dat"
            )
        tail = len(self._data) - self.index_offset
        if tail % ENTRY_SIZE:
            raise PhoneDatError(
                f"索引区长度 {tail} 不是 {ENTRY_SIZE} 的整数倍，文件可能被截断"
            )
        self.count = tail // ENTRY_SIZE
        if self.count == 0:
            raise PhoneDatError("索引区为空")
        self.version = self._decode_version(version_raw)
        self._mtime = datetime.fromtimestamp(
            self.path.stat().st_mtime, tz=timezone.utc
        ).astimezone().isoformat(timespec="seconds")
        self._check_sorted()

    # -- 头部 ---------------------------------------------------------------
    @staticmethod
    def _decode_version(raw: bytes) -> str:
        text = raw.decode("ascii", "replace").strip("\x00 ")
        if not text:
            return "unknown"
        return text

    def _prefix_at(self, i: int) -> int:
        return struct.unpack_from("<I", self._data, self.index_offset + i * ENTRY_SIZE)[0]

    def _check_sorted(self) -> None:
        previous = -1
        # 只抽检首尾与若干采样点，避免在 50 万条索引上做全量校验
        samples = {0, self.count - 1} | {self.count * k // 10 for k in range(1, 10)}
        for i in sorted(samples):
            value = self._prefix_at(i)
            if value < previous:
                raise PhoneDatError("索引未按号段升序排列，无法二分查找")
            previous = value

    # -- 查询 ---------------------------------------------------------------
    def _entry(self, i: int) -> tuple[int, int, int]:
        return struct.unpack_from("<IIB", self._data, self.index_offset + i * ENTRY_SIZE)

    def find_prefix(self, prefix: int) -> tuple[int, int] | None:
        """二分查找 7 位号段，返回 (offset, length)。"""
        lo, hi = 0, self.count - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            value = self._prefix_at(mid)
            if value == prefix:
                _, offset, length = self._entry(mid)
                return offset, length
            if value < prefix:
                lo = mid + 1
            else:
                hi = mid - 1
        return None

    def read_record(self, offset: int, length: int) -> tuple[str, str, str, str, str | None]:
        if offset < HEADER_SIZE or offset + length > len(self._data):
            raise PhoneDatError(f"记录偏移 {offset}+{length} 越界，索引已损坏")
        raw = self._data[offset:offset + length]
        text, encoding_note = _decode_component(raw)
        parts = [part.strip() for part in text.split("|")]
        province = parts[0] if parts else ""
        city = parts[1] if len(parts) > 1 else ""
        carrier = parts[2] if len(parts) > 2 else ""
        return province, city, carrier, text, encoding_note

    def lookup(self, number: str) -> LookupResult:
        digits = normalize(number)
        caveats = [CAVEAT_PORTABILITY]
        if len(digits) != 11 or not digits.startswith("1") or digits[1] not in "3456789":
            return LookupResult(
                number=number,
                valid=False,
                prefix=None,
                province=None,
                city=None,
                carrier=None,
                match="invalid",
                data_version=self.version,
                data_file=str(self.path),
                data_mtime=self._mtime,
                confidence={"level": "none", "reasons": ["号码格式不合法，未查询"]},
                caveats=["中国大陆手机号为 11 位且以 13x–19x 开头（不含 10x/11x/12x）"],
            )
        prefix = digits[:7]
        hit = self.find_prefix(int(prefix))
        if not hit:
            return LookupResult(
                number=digits,
                valid=True,
                prefix=prefix,
                province=None,
                city=None,
                carrier=None,
                match="none",
                data_version=self.version,
                data_file=str(self.path),
                data_mtime=self._mtime,
                confidence={
                    "level": "low",
                    "reasons": [f"号段 {prefix} 不在本地数据集中，可能是新号段或数据版本过旧"],
                },
                caveats=caveats + [f"本地数据未收录号段 {prefix}；升级 phone.dat 后重试"],
            )
        offset, length = hit
        province, city, carrier, text, encoding_note = self.read_record(offset, length)
        if encoding_note:
            caveats.append(encoding_note)
        if prefix[1:3] in {"70", "71", "62", "65", "67"}:
            caveats.append(CAVEAT_VIRTUAL)
        if not city:
            caveats.append("记录未给出城市粒度，仅能定位到省")

        version_year = _year_of(self.version)
        if version_year is None:
            caveats.append("数据文件未携带可解析的版本年份，时效未知")
            level = "low"
            reasons = ["归属匹配成功，但数据版本未知"]
        elif version_year < 2019:
            caveats.append(
                f"数据版本 {self.version} 早于携号转网商用（{PORTABILITY_DATE}），失真概率更高"
            )
            level = "medium"
            reasons = [f"号段精确命中，数据版本 {self.version} 早于 2019，需按旧归属理解"]
        else:
            level = "medium"
            reasons = [f"号段精确命中（{prefix}），数据版本 {self.version}"]
        return LookupResult(
            number=digits,
            valid=True,
            prefix=prefix,
            province=province,
            city=city,
            carrier=carrier,
            match="exact",
            data_version=self.version,
            data_file=str(self.path),
            data_mtime=self._mtime,
            confidence={"level": level, "reasons": reasons},
            caveats=caveats,
            raw=text,
        )

    # -- 辅助 ---------------------------------------------------------------
    def __len__(self) -> int:
        return self.count

    def iter_prefixes(self, limit: int | None = None) -> Iterator[int]:
        for i in range(self.count if limit is None else min(limit, self.count)):
            yield self._prefix_at(i)


def _year_of(version: str) -> int | None:
    digits = "".join(ch for ch in version if ch.isdigit())
    if len(digits) >= 4:
        year = int(digits[:4])
        if 1990 <= year <= 2100:
            return year
    return None


def find_database(explicit: str | None = None) -> Path:
    """按 显式参数 → 环境变量 → 工作目录 → 脚本目录 的顺序找 phone.dat。"""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("CN_PHONE_DB")
    if env:
        candidates.append(Path(env))
    here = Path(__file__).resolve().parent
    candidates.extend(
        [
            Path.cwd() / "phone.dat",
            here / "phone.dat",
            here / "data" / "phone.dat",
            Path.cwd() / "data" / "phone.dat",
        ]
    )
    for path in candidates:
        if path.is_file():
            return path
    raise PhoneDatError(
        "找不到 phone.dat；用 --db 指定，或设置 CN_PHONE_DB，或放到脚本同目录。"
        " 数据可从 xluohome/phonedata releases 获取。"
    )


def lookup(number: str, db: str | Path | None = None) -> dict[str, Any]:
    return PhoneDat(find_database(str(db) if db else None)).lookup(number).to_dict()



# --------------------------------------------------------------------------- CLI

def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _augment(record: dict[str, Any], *, db: PhoneDat) -> dict[str, Any]:
    """补上 §4.3 要求的外部数据溯源字段。"""
    record["source"] = f"phone.dat:{Path(record['dataFile']).name}:{record['dataVersion']}"
    record["observed_at"] = _now_iso()
    return record


def _merge_caveats(*groups: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for group in groups:
        for item in group:
            if item and item not in seen:
                seen.append(item)
    return seen


def _info_record(db: PhoneDat) -> dict[str, Any]:
    return _augment(
        {
            "input": None,
            "valid": True,
            "prefix": None,
            "attribution": {"province": None, "city": None, "carrier": None},
            "kind": "info",
            "match": "none",
            "dataVersion": db.version,
            "dataFile": str(db.path),
            "dataMtime": db._mtime,
            "confidence": {"level": "high", "reasons": ["只读数据文件元信息"]},
            "caveats": [CAVEAT_PORTABILITY],
            "raw": None,
            "file": str(db.path),
            "sizeBytes": db.path.stat().st_size,
            "entries": len(db),
            "indexOffset": db.index_offset,
        },
        db=db,
    )


def _prefix_record(db: PhoneDat, prefix: str) -> dict[str, Any]:
    hit = db.find_prefix(int(prefix))
    base = {
        "input": prefix,
        "valid": True,
        "prefix": prefix,
        "attribution": {"province": None, "city": None, "carrier": None},
        "match": "none",
        "dataVersion": db.version,
        "dataFile": str(db.path),
        "dataMtime": db._mtime,
        "confidence": {"level": "low", "reasons": ["号段不在本地数据集，不回退到相邻号段"]},
        "caveats": [CAVEAT_PORTABILITY],
        "raw": None,
    }
    if hit:
        province, city, carrier, text, note = db.read_record(*hit)
        base.update(
            {
                "attribution": {"province": province, "city": city, "carrier": carrier},
                "match": "exact",
                "confidence": {"level": "medium", "reasons": [f"号段精确命中（{prefix}）"]},
                "caveats": _merge_caveats([CAVEAT_PORTABILITY], [note] if note else []),
                "raw": text,
            }
        )
    return _augment(base, db=db)


def _iter_inputs(args: argparse.Namespace) -> Iterable[str]:
    for value in args.numbers:
        yield value
    if args.input:
        try:
            text = Path(args.input).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise SystemExit(emit(Exit.USAGE, f"读取 --input 失败：{exc}")) from exc
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                yield line
    if not args.numbers and not args.input:
        for line in sys.stdin:
            line = line.strip()
            if line and not line.startswith("#"):
                yield line


def main(argv: list[str] | None = None) -> int:
    force_utf8_streams()
    parser = argparse.ArgumentParser(
        prog="phone_locate.py",
        description="离线手机号号段归属地查询（phone.dat → JSONL）",
    )
    parser.add_argument("numbers", nargs="*", help="手机号（也可从 stdin 逐行读入）")
    parser.add_argument("--input", help="输入文件路径（一行一个号码，# 开头为注释）")
    parser.add_argument("--output", help="输出文件路径（默认 stdout）")
    parser.add_argument("--db", help="phone.dat 路径（缺省按 CN_PHONE_DB / 当前目录查找）")
    parser.add_argument("--prefix", help="直接查 7 位号段，例如 1380013")
    parser.add_argument("--info", action="store_true", help="只打印数据文件元信息（单行 JSONL）")
    args = parser.parse_args(argv)

    try:
        db = PhoneDat(find_database(args.db))
    except PhoneDatError as exc:
        return emit(Exit.DEPENDENCY_MISSING, str(exc))

    if args.prefix:
        prefix = "".join(ch for ch in args.prefix if ch.isdigit())
        if len(prefix) != 7:
            return emit(Exit.USAGE, "--prefix 需要 7 位数字号段")

    out_handle = None
    try:
        if args.output:
            out_handle = open(args.output, "w", encoding="utf-8", newline="\n")
            sink = out_handle
        else:
            sink = sys.stdout

        if args.info:
            sink.write(json.dumps(_info_record(db), ensure_ascii=False) + "\n")
            sink.flush()
            return 0

        if args.prefix:
            record = _prefix_record(db, prefix)
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
            sink.flush()
            return Exit.OK if record["match"] == "exact" else Exit.PARTIAL

        total = 0
        usable = 0
        located = 0
        for value in _iter_inputs(args):
            record = _augment(db.lookup(value).to_dict(), db=db)
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
            total += 1
            # 进程级成功看「这条输入是不是一个号码」；命不命中的是数据质量。
            if record["match"] != "invalid":
                usable += 1
            if record["match"] == "exact" and record["confidence"]["level"] != "low":
                located += 1
        sink.flush()
    finally:
        if out_handle is not None:
            out_handle.close()

    if total == 0:
        return emit(Exit.USAGE, "没有输入（stdin 为空，也未给位置参数或 --input）")
    print(
        f"共 {total} 条，可定位 {located}，未命中或低置信 {total - located}"
        f"（数据版本 {db.version}，{db.path.name}，{len(db)} 条号段）",
        file=sys.stderr,
    )
    return Exit.OK if usable == total else Exit.PARTIAL


if __name__ == "__main__":
    sys.exit(main())
