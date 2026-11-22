#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""
ID Card Parser — 按出生年份区间核对历史行政区划的 18 位身份证解析器

用法：
    python id_card_parse.py 11010519491231002X
    echo 130602198003121213 | python id_card_parse.py --issue-year 2005
    cat ids.txt | python id_card_parse.py > out.jsonl

注意：
    地址码必须按**赋号年份**查历史区划，不能查今天的区划表。同一码在不同
    年份可能指向不同区县，所以本脚本返回候选集合而不是单值；规则、边界与
    区划库覆盖范围见 docs/id-card.md。

用途
    解析中国大陆 18 位居民身份证号：做 GB 11643-1999 的四项校验（长度/字符集、
    出生日期合法性、MOD 11-2 校验位、地址码存在性），并**按出生年份区间**而不是
    今天的区划表去核对前 6 位地址码，输出省/市/区县候选、生效年份区间与可信度。

输入
    默认从 stdin 逐行读取身份证号（一行一个，``#`` 开头为注释，空行跳过）。
    也可用 ``--input <file>`` 指定文件；命令行位置参数可直接给号，便于手工单查。
    ``--issue-year`` 可把地址码钉在某一年（知道签发年份时更准）；``--span``
    调整出生年至签发年的年限窗口（默认 40 年，符合 16—45 周岁主要发证区间）。

输出
    stdout 输出 **JSONL**（一行一条记录），字段见 docs/id-card.md §7.3：
    ``input`` / ``valid_format`` / ``checksum_ok`` / ``birth_date`` / ``sex``(M|F) /
    ``address{code,status,candidates[],confidence,window,resolved}`` / ``warnings[]``，
    另加 ``level``（format|date|checksum|address|ok）、``dataset``、``source``、
    ``observed_at``（ISO 8601）。诊断信息一律走 stderr。

依赖
    Python >= 3.8（已启用 ``from __future__ import annotations``；本机在 3.14
    实测通过，未在 3.8 实测），仅标准库（无第三方包）。内置区划表来自
    ``gb2260@0.6.0``（revision 200212—201607，MIT）与
    ``china-division@2.7.0``（MIT，latest 基线），已在文件内联并注明来源；
    两者许可与 MIT 兼容。``--divisions`` 可挂载外部快照集，schema 见
    :meth:`DivisionIndex.from_file`。

数据时效
    内置快照覆盖 2002-12 至 2016-07 共 19 个修订，外加一个 package 未声明年份的
    latest 基线。**2016-07 之后的区划调整（含 2025 年现状）不在任何快照内**，
    因此对 2017 年以后才签发的证件，地址码只能判到 latest 基线，结论降级。
    早于 2002-12 的年份无快照，只能靠区间重叠推断，且首年（first_year）是
    快照首见年而非真实设立年。

速率
    纯本地计算，无网络请求，单条解析毫秒级；一次可处理 stdin 中的上万条，
    不设速率限制。

Caveat
    * 地址码存在性只是**数据覆盖**判断：判为 unresolved 表示"本地快照没这个码"，
      不等于证件是假的；2002 年以前的旧码（撤县设市/设区前）需要挂载更早的快照集。
    * 同一码在不同快照里可能指向不同区县（撤并、更名、复用）。模块返回**候选集合**，
      多个候选时 status=ambiguous、confidence=low，**不替你挑一个**。
    * ``first_year``/``last_year`` 是快照边界年，不是民政部批复日期；精确到月的
      改动（如 2015-09 撤闸北）只能落到快照粒度。
    * 17 位顺序码只用来定性别，不承载地区/办理顺序信息，不要拿它做任何推断。
    * 15 位旧号不做自动升位推断（升位依赖发证地与校验位），只给出 format 级失败
      与相应 caveat。

MIT License.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _codes import Exit, emit, force_utf8_streams  # noqa: E402  退出码契约与本脚本同目录

__all__ = [
    "DivisionHit",
    "DivisionIndex",
    "parse_id",
    "parse_many",
    "resolve_candidates",
    "ID_WEIGHTS",
    "ID_CHECK_CODES",
]

# --------------------------------------------------------------------------- 常量

ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
ID_CHECK_CODES = "10X98765432"

#: 内置快照的修订号（GB/T 2260 版本，YYYYMM）+ 最新基线
REVISIONS = (
    "200212", "200306", "200312", "200403", "200409", "200412",
    "200506", "200512", "200612", "200712", "200812", "200912",
    "201010", "201110", "201210", "201308", "201410", "201509",
    "201607", "latest",
)

DATASET_NAME = "gb2260-snapshots+china-division@2.7.0"
DATASET_COVERAGE = "2002-12 .. 2016-07 快照序列 + latest 基线"

#: 市辖区类的占位父级名，展示时用省名替换
_PLACEHOLDER_CITIES = {"市辖区", "县", "省直辖县级行政区划", "自治区直辖县级行政区划"}

_SEX = {1: "男", 0: "女"}

_ID_RE = re.compile(r"^\d{17}[\dX]$")

# 内置区间表：base85(zlib("code,name,start_rev,end_rev\n..."))
# 由 build_divisions.py 生成，勿手改。
_DIVISION_B85 = """
c-oYG$5tF$)8)LMMFUmZs>kU;&wAGX0|*dKSV>5D5W>k^JP0S?WhnO+9Xle+Waf?peM{@=)7s}`9UCWn)M{_#e{Y(z%g=ZJH
E%}W1aIF4wcySF{onudfBxV9{I6Q=Z4L0vXy@^3bM}tGwKvZ<8_n%`2M*qJ_Mdz>eAB#~Y<-;^g5|G9EigEG(>&g79UVAu{H
C?B*<HDCKAy<XPMZs-4xGMeZ(p=lPnu_EPK)}R*41_E`keze-gK9LH1{rB=i9tRt@frh_2hnE{?&cA*1X<w;P6d%=kED-+JU
1tt(Dp4tuy;t{HD2g*c`uf;N(r~-MiNIk^`r2<jbAYYX`2sX`W0x-+eT=mdIib!;Lr1`#;UQ2?_@|Qu9xP4Xwp*8wv#d&vLl
`*)km1LEih>y4)uRIjBC!fga@MCucbWJ;<%|3mkP0hk$X^IV=ZxZn^pQodd_{Ew|8tlQ-?V@2#DE2bR6sS!ta=<ZnqGw<NGr
d^YXF4V;tK%?XEVZ*jk$Zyxw>%L$iBcQ-#du&kQw$DA`UIpH$t&f}>A%LzX^Xx)rEu$=JY&F1F41Ir1&|A7nqwfFr-p@l5m@
rbVcU3QRiaQ#hpb5%azz>PPZ&)famn>*xzcgO_@YQ100!BOq4!C~)LIUMV+mcJb81+Zh(-WVw^NT?S;2wwA8ZqR#jMMAp+#V
cYnGg#==m#5}2BS@j$ft~&Q6_MWI+q%IKV17kHy92UObMp?IzInd;(p{ZyZ+jo8zv=9L@joEjwLH>)iLju<u>7lg`_#Iac3`
38-yYrGvZpq>p^X`aWo3?bo5yPo9Q8h)+t%K~Vas=Bn@fvMZ=nt&SIv_r2TtE~f3I{$KRU2bhwHP}#e)OOs$Ffh*Csg}%86V
4*4i3z;2K^Lynh@H-ZaO@TBi#R9LjHBaV)J3&azBTPEPB?yVl0Z^WEX|%^C>pg1$}GCD$_;x&@EF+VevAzBaG-o9josLtRMR
<9c^}g5Q~X&^uQHcxdzLvh}Mopc1@ygj4@~yXe4i?^LpnD~*w3A?wR}2i4a(0B?4E!ih8BP~lk*mM3jtuQ~C@frU8BF3{%`a
H_nS``dNEtv?42+<4Pkzil3jwU=(P3mN1tB&cZX{vdy!dh^D06HI=a9W78;*^lSDBL`03be={)#yH$~)1IGhFP(BY)Ez5P_Z
q0Xz;diJMnJc<^Wi|)*Y^EF>uSE@Ob2qN`{%&k`u6codvU^P9cHcn>EZ-wlp7l!)|-=;$0paOE96P;7(JtMKV&)arf}Hy%C1
l8;~}23!vm0~^L6@xu=);!VC!@u^q_^1Pjsry@e8L#1Qw<>b=hYlN-cyB`LHHWK4BvSmZwA5r|#ow>wc%TywI8a)jGeYhbIg
IaQxl-c?S-AKUC%%0D}kpE<eP3gQ5;CrERR8jf(9JarAog&ls&zqe!-?J9^R`yP<m&Rqs&*wqw+h(g24-E7rM~2Uw=PH^=25
z{#8D#i{)9_=-qKRcCbn`F6*dn>?VWH_h8i4r6<5>Wmge0S15B`g=h)B?76MTEy+(u*rCfa}F#g?~m72q~F~U2dnpH%*Wj5&
6vU=Hk-frkuU_^n;nnah`<osKH<^eu&~Al6W!H64jjGd9xXVSaF^@n&5u7goc4C<mB&638k%kE#5;LO+><ZV59$ktyv2GkzT
@n2xbkAeAYO#w9)dgj@4I6UbZD`$>v?{BxVN0lryakyyps3#*hvm2D)5RxO<}ziI&XG_Vj*;LEAkBSaBrc?6XVY}D-JA|W_u
xje1~D-sn#Z%r-u%lU}Da=(%!5%Tvy3X*ggJbk+iRW2tUVR@Y3=cpI<m|P4C9>vIFDlT)2~#tJC~3;$X5~H{<>f$Jh*y;feb
!i*4wO3_&bc0*iS3o1ghQ6fLXxB!d&VG0#t%5T!U=d1DgXn6J2Oc{qC_0k?l|wGZFXr#gA7>I%QNDS2EtdDEFcZ=HQ}KAsAv
vonw|i*OyA)S3C{z;d=87h9*79G3f*eVnld2^b6eIERH&{Mo|C*$GJGzI9hNJgS}Gk+Pxjj|-)c-5mEmp1$!U-$d>kMB5ebh
h<HUZ#^oK;J(SM?wpUyeQRy3gZ^<??%VM+4ikrE$yPo)ScJF89k$%J&bJ*0OP~^VmpTM}TI)`FGnVyQf6p<w#&)m%NhnB!{j
+ZE)dx|7V?DO#NjtoQf1W%sIH?DE2wXVCVmH?x95})`Xn+6Myj!L%QV7a_zd`6<*mYV|5{{HTwZpx`D&1BJVfc6#G7yJhsF>
+|ILmYpqy^;R$>#VEqH6@)fElH|_m{(}anzq@YQt1+KNe?}zbq+U+q-M*GKV1+K6uwC#qB>`gAs$nLKK(Q985G<xFp*ST!#h
2<Be0f{n?VyN#pj5{bO^V_YU>EdT(_qx4(0>=aH5)414=Qe_QTa|1`e%UZ!b~JG;%3ZD@FX`96hwKipwwtGYuSdgJM*=HoTJ
8ujX{QJ1UHT$tt73BaMk_XiFvclY+RH9gZ=+;1K&dQw;&FPM#yt#}=0c>EH49fw13N6@1&#;u-dgdDP3&p5{1{TYVM&M2JUO
A>q?ui5c~oSRoP6=rh%vbk}WHT|?I>b4j9q`gJ@Cn8?2*LthNlq?N3D;#>nxlw&Q8X3#wsO1~bGCnPO%994R{hV{^4cT)To;
Vh&S$cfhfulF7)VXxv_)U9#(bHQuK(t23@`{#WSfzya+OY%6#x7pszHnF+h_g;tXb+FJTbm!7>s!@Fq0wi{ZR=hnGmSLJcKM
&qP9qJoZmpZWfvv6F4h+D>2QilOLnI7<NL>)=I4t7!{rm2(aR*k_WOtc<yrzcX`wz{{4-Q;)fCV;A@4p>(LInnCdE!n8A&k~
?jqjXSO5hN>6qq<UEY$H})e+4EHvJEOh6EP<kGNL2zg^ef^P?}FCNaL%vK4%tenf0>BH(Y_I5b0G2>8>EkXM7Hva_=Tk%7aN
8>XiEoiXln0vOtZcie6(FeuK}Zu9RQU5Oe*rK1DKoK`bS-+|v-_H+M_!}`}?>BDZ_da&&0mvL{qVd;Z63&-jzqj_EX)WVkiU
r0a=7B%YzbNlUAhXmAMNXr&JXUsCLs0KrtSR$Px)4(Mbvvzy^68k>?)V@0uqiAah>_T(uQ%~mYAA^Qo>miw<1P3{@UJj1TnM
4dQ`%lD{9+@-A-Y@W`<-tbPhV)2n-A%E2-ZAS}r2ZbAr}<2`Mqt_J%>{@H=SNNpx!I>*J9E>`&0kIn<&H&7*4lCgA@(%UWPz
`zFs_MEtw+Z80}jBuL*>e0sH5Ju#|}6Qa-)midHAonHPgQT+gzM#E!}oEf42ATI<sTsfJJJl6&~sNZjWw?T1%lVpLO8MJ%hD
0Pf~`l@J*g^9l~m=?&^+xaA0MRI&-^pnS+`#uGtTlW*mrz47{Z#rW`m{*hpO&h8A|^LM}Vqg#b_2-p}rrNe9-453UCsj^LKS
m9&vsSzA{hx!4F;v_jVhc~7umIPGm^nV*l;jB4k{8oj{$L~2LGalM$PM<c908e!>Z40kOajUh~qM*dt3VS%|$P?wQNO@Zz8Q
HrUx?aLG1ayFT$dNNVPIftpO^|0lgCz#rvZ$31~*Bn@=-q}g}%c=t>`f=x|N2SFY8W#?-&6phr7}>-S(v(-7t``iWHZI;f#!
f)(#(RrOPAszYWsTI%*4+IA^D&3(>QZ&I=)euI6I`nwsj01XKH^|_(nq`triLk7?c8IWO&W$N*fDbOpton3Qa1*J`@M(Tsh5
WR#XbXBG&czUUd4e;kLsi&wIG4Za&`qKqVDQd^Lo>Pg*eIUE9acTU~YwI-d@nffr+BEvBVjfsM<sknTbMrJM&f>gq+6n5e^4
R`MN87S)k}|-Yq#86g3y(yyCzS6lTx&BlMOeeUfWaf05*>M%cbrRystgv6UnAd~-w>F|A%iRo5O&aG5Pqb**r3nRj#fw{f-(
Cfa+CbR|?@dVSE^9CP3hj>I0lP<`pj5*Gm?)0dt+`Eb%3Iok!OOxyb-PS?7goRNJ8Zs5I!YaE4TyPof7p8tNN`vDcAsBt`cQ
Lo-Es1QZpBC-SBF6jQm0@<bt>PU5e!pnk%;;`rrmhYQiHyl{_lpohz*AuDkPx!O;{kj8-^QDUk>EGJ*cwklV?JYQ1L-%TRg2
O`hJ0Ffc>IQutlnPhQW~hbrg8Ruws_tt(-FxJ#QN5K=_o4UQ2Hnbrnj+Tl8ncl@rS~lKY2nm?MXPte>tM2vw<FDqRR@mMHh*
;BzzMvr;fKj#+^a3vZaG}nVW0ePVA;*{@6B`0%Qf^BcV(qUXqGvu`35WtKC<937|;V|D=N>z?mQF(i*uc^4NCc-rGre%Up7^
w!CUF<(SfUuhAGlc4>OOFa&J)G_4C#_C9A4PYd`&U&b%tp<Wym392Tm3`Kxug<-oBzvvMp^{U7yN0xZ(_)v-fAOv$!5?O-B=
3u{O9$O4BZuez{sA#0}&7O0C>&QWC;mc{yX?qIS~XOG<7BT~iQ^W#bP=YRYN2yN+Z%sIqE6?@IaFRkw*85|bi!T}0%2MCJ2_
Q5@#r~y3G8wcp?<bWkC!k{ffHoGDmz8Tm9iddxnNF0*~Lc++lmK`|JD6{>)4lHj(pDq+JF>rpp9h|Hp3>D7NfKw>Ia(Xb`g<
THgrXOv>E=zX;Z^YRnK2G2u9PH!9X+Ca9%<<Nu@ckm(-%1XL`JXSqP{I!HbP*2oJ6(k1H+jp?FqRDMbP-GQJ6(k9`JFDpjW_
uhU<gy!uiHEg8HR)WP8Z=Yzth8TY?{o8akC$0I{q^KjZLNb@vqQ~*45AU^ewr-akXMIwkSDu0damp?wHsbPCq(u1mF373{D9
a$C#`2_QMwrgSFV$?%uCp@WP)jFSAB6!0DCd_zeDACUCK-CS?t_mRsNcHcywFM&>eiddW>Qu?Fpc*`N|;tWhY#i~TCIN^C03
*>P?~i>p<bv8ph|D9UZEv1&0}D--fQKi_|JT10AbrJmTa#(dxg%q%*vkiyjm(diX+rD9$R@-4dWN5zq8(5_zil0=W(Zh}*xK
a@KO3m5bx1ZJ4yf>x~B&CcVa8~{0Qo!QaW%DgZEoslIdqUrY_0PEJYf4sx5zutrrtN!u%b`~aSwy$CJzGguT&6%;*?IT?grK
i$@hTFs%#2`P9`7#&^MR>bhoYK)n)uV$#QKDD9Lj#?n1c4!t(`_M_ZS&EERg3*38j<$lN7*lEh1do~Dz;yOd^^Medcv%Qb7<
5be`}dDN3p32$7i1J-^qVF<B!fa<d8p&w5QkQbUH1BXZ-rEDKDHm7q~W#aAr6*z2KLxhy-H2)$9D-TIcbB?q^)RpU}$nSDNk
!baR6i*9XG3%k#yD5(LGj&Bb}ADcqXgb*a8HR+V1!?(_5C1E;ra&Hhwt{jURy@_Bk5hsfC!_&M96y^>a|mT+TEj!Sp!s5$eA
t~GRppmp-j?5(xFo;ZyP8<OTWBr&p{$#kT*`tQlX#<?t;ykk`w%H40xOnZ}8z2W49y@*bW5EHStwK+ZKG>Qg|oEGwIZXR`)4
jecEp=uuQIIz46e?N35KVso8*J-2F_#8PXoy$!uJX`X#`VOR7*x1Z8KEPI8Jwm7FJ6F@4le>N+Rd5eAGSVfQD?U<L_0Ih7=7
)9Jo%ZJg?9ta9-GR(m!|A45ft#?hU)j!f<L0OCM)fDf18#Ln&XD|iXLa6blqmDpYvhqxC@FL89AXQVBYfX3&%2Yee3wK@X-}
`p@biMia0}gr=D4UoPMj993g7i0EbO)KywAw7{C3OzRbthCVmc28t?RFYBNY}JRS37S>Oi4eIcLIcY&uYA?zf3J7po4my?e=
}y;yai;4k!s4{m3%Q8Z{YAlSu5@z9t0q`KHB$!@XNqG1dV_C?%TZ^r0~HL6z(sz$hP^a3l3y}<>}eePVnr%M7=BRo3$YPBLP
RP)Qk!2Q1nM=C@16)Z(qUYp5ju4{-@+lYC8p3-A?fmhGj6m=%dH}$qg9SaLz^_Cy}!}>b>ZRclr3+Q=nPGS5fTN9`tBJ)3<Z
m(~bE<^x9#J1O$B?E{fLj5pU8o(|sa*RY_or+C^VpE6b2sAcaNwf3Z1?s!-Yb5V3Jb;ydTH5Kq>~smhMLplmcAmB>mH@nzeW
|KZVCy6VW`$DT0IX4GW}$id*B5;QFi3+&oaP2#laTJ^cDB9nt^IStVL$`$OTq=eq8%JHem&n$G*9lF7O;T-5${e1h6Q|AS|G
rLwg9txwA=Z->A*q;_SWPz<a?Rcdh~4lYu`s+{!jC8>G|(kiLDEg0_7UoeECbH8EaJQk*2c#3UH9WTm@JowTCEI5svees|Y7
~%2kBZJmo6FjXdQlz|cv1l&jdZ=2Na>)4-=(#imh-a^+hTALIN6^&8cCuPxhwdS-{3-=?d8=QQ`)7U77!w#7y<du@x26872_
8>OY!Hs7M5?oCf%SHHfe)T&{esI~AYNW%zG*2tkCcsPZh5(!C|Dc%B_lAc1c|Gn4iO9IPT2l4fIr0ks;SkAiE`BYwgJPiBHe
PB83yi8z0Elc$^MYU)_ma}f}kVc9Hma`5y77jl;u-G9#kI+D}z!J)VhD&JA*@@Fag+VyCl#(!Iu(;jz6+y#!i~>tJm+5bh7V
|2)SX6Yq7UxrlE9hs{N(v)@PvX2-+nfQ$*g80Nlw?uBka-L)g-$7K!YuQUR#@~i7hgmVz+p%fa>!0U(sPkHh{}`gz?H|ww5v
x{O{H+CnGnyH9XRTRJumF?7P9y4r4>mtmEW7;7P7g^<M2Fjzaf`%@*~YSsIUxT^%!qk&o#NQJ>ZPsX?Jx^7Z}N2L{LS}Ll%j
tErOw+DEWbF3vBUwDr*HbbJg6Mqb#Gsm74@rsoXE`c0x~Das8ktZU3I}wm4K*-!T`4Kq7W{7LK8?%=`Rz^ZaLZY#5s29Lp9k
ZySN3ua!hFI0qcAT%yQqILIW!W?|`8Hbg3S;0R&Ua89Q%yf|fC%s>PfCR`gin|BnLdFlA^#U+9+SEFjSUu>$rjvN2A?tanbi
mR7PofIv{8Jm%2*|_^fU7Hk!H`u)2JBIUk=fi&-#&YS%U!EkEZs)VO+r8LSIoz9D+!3}ON%eYwx6SnVY{<|?wq{RgIv#~1g;
|P0z~GFJA0+G7%d=1cb_1E6=qwO%KDp;zp;Se@1M0`1aOE1L8WOwL+*zWtQ1`>hS&xHAK}ICD#B!&UVRbnENF7cThHZ?K7--
b+!_3U2u;y|qNiEsapTPsECI9ZL3!udCvH*L9E0;`N0DrFvw@fEp&tA`5ZjE7Bo~85g?%Chw#uRS>&gtDq=kb=qc(~TTdixE
RIpGXo+}zoxEsVw9u;d~PuAF@%v*0=JGJ<5~%gCz4;YbgR!>~4xSyS`7AX_nQ0oLMv!aI;A9AIuQ1|e%}S*wRr>S-6$Y8mV9
@aC01WC{nc5IZ+ZgE0~DxUuWNARi0Qf43bNISW6-AD;zQ1)AAdSNdqtSu6Eo%ce(Xk8?J1g7LH2Yl_zO6;K`yS1wM?BJsgva
1PO8A1zFtrAyNmD!#NX|1$nheXKH=u;=PyCF6H!6LK_zy~66U=~Xk>HPsh>{^MMdnsU&V#)!f=#P-{CXzD-sggAZ<Te9u-M-
C%L=;>H<{L0}=Yv6k!(6WHR0M1%(d#TJhT)8q~w(-ubP_9<e{&A+O7AhC9=^a@(TQ%?R9FeQ0X$5=6YO~<Jf=blD!Xhih{tr
H3eV%6#y~1GiNci|0=`mT}M{QBLVqkJ1=W)IZ0ajQsnif)F&Gdx9Qy6ON`~AEOI}GcB;y6(lD>Ht{Ig>2Q`Qo&wjEU;QvYbO
eTiK_*D=CO&Ifrh5CA$nP%ekemaNx+z#~)ahbJ2)*M?O0+T<M=^3Y@@FiOU&xKVA#fC8$T%a*B;W)e6mt4K8~H8gOc>_8Q~9
b_jaQJSk15{y}?%&SBR1AG<|ax7OK1=i%VBLj@yjD^toLtgwa3eEY}`45abL>(<ye2ZkN&zvr9#jHs7>KIorU`hlSOK!WBY<
>}~0y6Zu9PalpPSWF&Mx8O=CTvHnt5~EQ#&@c+>y$u*@OuLAcu}<1F$eRAshHIi;?FLe^{s89k!?)(<PIp&y<s-ZgUZVAFu0
07%{jzg_w8<QnS8(bRv@;wQ{m1SiHMR?^UcsW9zxCkCt<F?4T=x?6R5M(26IgEC?>}&i*=DIruy}^NTYEup3{<(2JJRKm)|C
`d|0#hZ<8b-g6;!KV0?T$Qk2QUn0#(0YiKu`ar~*aHHJsA9MS-~rJw8x4a}1-oniZ}bd7QbIuFTS*X+lvY|E;ra-Xf^=ex8m
_T{5R>-fo2>b<buFxqzWF`#zp2Sn2fYnLoiag`pOK=s_90pjs^wWNMM$rP*gevsnv7#P{IgjOGd>E@OL;k0CMPJb`cyIL^aq
esrc-gBvAVoJ3P8eQh33I<P)iu)xame!fwCfz*e~hLW?6#o0*fW_ml@rt7GIpBSbGS1z+^UL?%RIboU!Z+Z@9dpP!r4ddixW
;IYptV}B8FnEtgkGar@&*?wU4~6*h!1g`uzauv77he<gi?Z)o=8_?&GJ)&aId%BQdi4TA57W9lrdj_1Yoz_8_)gHN!fpy0do
aP_!IP;2d~vE8$ta_l<IF>*mT_)u5NY10Kce#~9^30J*1D$Eg%-V!!C(CG`Tjo#7R|$#1BY}5kQmwv=MGyQAT@#zeb{z5oM=
e#<0^-7z|MZrBvJ||mOYc*eXANH`b4l%XWK-ku);BBo0kMXWCYcPV}h)3%*bcB@s$si6^_9cQ`49gj)Bmy_tk;pvi8dymJCj
iLJP^i%JxjFE7Sb&Lwqb9Sc9fv(Bi+{P-4};EjR{4FpA0y|8|n^4Q-MHfsCIzz0L2ZFKrcjYhp#3FgUQ1G}*`VJ_^~yK(ih<
^YCzQ)Frt^(HagTn)cDblFa|YfyG;4<A?vlb)_Km6}NgcxTLJuO<=WJvL!2Pu&q{0wq!ZJ4ylL(tJNa8N}Es1{E*ipbqcw;p
|C2q9v0<K=EuStRbG_9m_IQD)8htl2dWsBOEIvLEW#ppmu4JT-sP21{JHz(w18jYZ06RyedjciSm^JMP%%;-)vC7$t0mVR@9
U7$GH;D1MTrmH(jYsi^l{CKxm=vmJg$Kia}lFC=NYY-i%b}0qd43^JO@{2X2o12+~a1$f#va<*`Qv8ffYvS9A9%u%}Sd{sRT
26Fu3v{sW5f3)13H5*IeU)Mm~8P2s?M{{*r5I0LML9i^Itq^C026ShSGLzIXTT&j(s1!dq7eXz)Sb;@4EAvy%zVr)RZDZuju
maJX_^l?zz>$hA;biv*>lCN><{GQ>Oc)&xoFRO`Tr<}`JCn`55`!!WV9NwYjF96)D@2cE%@45589{}#4zarMHfnf&ytH#!K1
!d`Rf-l|QAW&ts>)NYX#oN!{uf~?@g`b6h)k53Qk!G)R5=?4df-gA>PSXOXCvO0QiR+8nNyPKd3msBqt#KO+v4-n+_M>@A8Q
}2s4o5P|6JO0@j+tF%Xv_-5DdJg}E!Ub8fH45WkjZV^JNrBZC5SZqn3bNV)A`dLxI51-WHe~kCe^0~*@?~kwQrX*j|AV&8a>
R>@h_mR`3E~4w5TEttaK_>;IWUT;%{2EP95{y3C!eZu7)C_lx8}ux18clZzDDOTEOG0P^Ld_N7;dP`m|U#06W#(PLhqLHTGn
A$J**{TME*G#Zh>S2va5G~3t7IKgS=UG7*>Z{S%1HU9`V-7wgZE-egLQ7en3OZWTE)sky@c}hJX6zz>T5(EyEx`E7P61-}FF
3zhLVFnDmZRjcs`e2&_qh+sD0p)}%$!JDe1bYd8Vf=_|u^Gpv4Cr}qZxi06k3VfkrzV^E!6KCt}qqJ{^7ViKC>m-k;}c!r3^
r}MP#z#8u$iE%jWWoQg6<-DlUJ=RiVpmulyfWXqb%bwEW27p7Y<<@!HAa?dFWBcMP*P(@`7de^bZUlh)A2$Wku#MmHy+2Dx*
21aBn>MO<te5hg`gmE&cjRYwo@#irbY|aIu#XzqH8a+n`pMQ#Jxtou!am+Q>r?GyVi~`|iEutlolKA{bY<RwMb$HPMw4799Q
z7<%lNIoJ;F9#6BTKE`YxY(T`kYzFX(KYw9rfd4)ZK<q=DsBqXt;Ws6_kho;h&R8$3B!>fIumCU0ofN+iqvGj#?F09!cKcl`
dD7bAyyLxuhB1Ee{&aL{)&k5(lwKZng*!CMyS6`&bV-*F@f<Bh`J0LymLP<>-$>$n0jtoDrw)i(w{`CF8QQ|A})%5Cn?)9Gn
0jPX;N)<EG<(|$;nVGbkme!`K^Yntg-0@qG29XL_JWA}{1Sb0gj@%YG>rLNp1_f-X~ryapx1P;=SGdGz_Uj7X#Tm;JdefTEf
?-<AaWZnW(*M~?Owf=s~PIqyC0}UA$RbijMDE3wtzrh9;;YfqlZ=Bwy>}$`jvqcP)zt~xF1e8Ge3mZAPIzgJ0XJ4LPGCVy34
^NN4!_&*dp@sVhdq(|MLkssQ-Hl<mhT;*es{;oHYyLf-Iz>HY-WnWf`LeS~r*~{M(MKFu^9oXGuP{>fA9q*ZIj|^0mv8dMnC
vl7Sc-G6-qK(Xz{udf`NN$z)XU3Ps4ULm&=sgdi}Dec75S+-jP%|IFb97<by_4nxq`z8YI~%HJca8ZN&eg#;Ar!kmR%RXfgG
6qX=npzZ((?)d{h5{AAZKdEKI2MIBPABx4nL4*gUsnJ`%rGwz7ogxg}$Mz9R;vu-bDgG|#Q}{CjxamZxN%Tga?*Gn4NP8)Rs
nTT=YDb$#sgj-hw%j@~)2Y~|n2$b80O)cT&uJCzOnb`8NS2mBP9V^{0j*Y3ua1B>ou{RiSrm}W>@s5zS7&_K^&bNoosIytbM
!@I*lVZ|Y1tHN4>uyuXGVW=;xcgggtaNO(7U>vm8@;3<(rzaw)J`u_iTu$am&)_r$rjY>c;;`1WZtko&aE!opetQEoB*10Ng
R$jWE5w0i%fFBGH4?e|7g&^<P?g0?4#Q@Awv80nbk(%f;0E4&2CEN~6t!T10AaNcQ<(WM`8Od{7Z{jY9T;ki&AgdZ{%U|~1*
&%MTnb72#d~8>#n#}gIr2AjKM%CWcs_<<bsQo#xiIssI$w9Nu=m-VnXxx!WK!7tPIH(7hN@0i`!&vtp*d%1k=#+fJ#gU}z3_
HV9RhA{e7JIRRrkO5(<3BM?sq?JdwUP%ersy2`F*_k?X+AV7A<NzH|lz!uvQ+Tnhb?4v~zR4Yz_vDg}XbX4(196z1}6uXlRa
Mh&y%`F0NiUty6t|*t+^vc6<uWF$^|#<`<=KgrRj;J~(g;YY^&;uw_xZz~beAaW2AQ%v`^r9?c4i`Ty`Ere5;NQn;a|+?dN2
;56y2+Vwt^&+A9@P-{ixo16B;HecO(uK*+sX44j$xsjS?6pnhs%x!LO1?#cp==e-~C&rh+F^|KQd!J;l(Df#VaprGMl*y&A`
V$R8J{tMou7yJ%FRg1*(<*;i)KeJ_EQ<D+6om}U>8*J%O0AH9(^|GHuOwZG>g-OasH$*)pKGtpIB?ioiJWIc-RLYz;AT@Td8
{fN_oiBsO+)j!6U~J@P2LGht9LooDvtO7y+*KKfNEbIl#>!DoVd|%3a65Oau-fzCD;DYeEKptw~f7zo69SWSP#v2uRStL3k`
+lyC*PJ@`mQS*I&3-o&&A6DvUPMC56@gd12<CSB!ER@MX}3xAxcaOF6je3fpfpf(2!{z+j!&M%4APj+eHv_uDlanz(-US`=8
bIv6|2!rm)vR)nE4|FcasMG7~1y_tB0q6qEvv-ZX+?;V;I<Mu0uQ2|d^=9Syn&`>>I@j~IsZETP#OPZ}E!=CSs2HbI2_;QFJ
9;DGP!dd>2Q_2UZ?)>wdx5fzG1ZVEt7ESK5vf;6FnicKzJ&hz&Sj9+h3y{Aon^-BU4&bzwE%<A9$Rfb=fiJ^w&|4_N@~9P|H
T|E%@>H4-6b`g1Xf6%QaBo@1(PggP&?G6PT35nAHhNh5{t`pWoCb%~Rhj}trJvsEz|9~2vUxJl8bG1Oo@bMO-5Ogtfcsy~#}
U3tR++s!dhEdU-e7ZEkj=$n*(RwKN8!pXsOdHO&(SN?RPFHRz<G3PmPhBd`S^*$8Zd^T(Q21yp{MWGVYn^9{`7yezLCOk?%Z
!4-%?mL1Cox}Ih@s+0W}tYS#S=g233hxf(1V+ESLOnqc6qeRUW3Whvn}pA6i`_4ZJ;7h5f~;Bc0a}fRz1q?!cJBM5Ab%$4{L
nej2NVcZ;GuesW}inr3&AkDvW6d+P!Y`f$nLR$<6MM-FMML5yx)Ec-CL9G8((iNa|uoA;}(t@(VnuDHW@Xb#_!QH&q2YH#n*
yuDEdkglV7dqW+_Y7B<v?TyoMd+xx(O3s`?jOHKLyo6k{WbWK}-YDHQcka&OAy=W8JGVq#U}ZR5Q>%N=6CwBVhFNB=o_AkqO
EiF0U~^+9FtiJ2nLe%zt24r4lX+Q%nbWms8JUSL%>1IoulVtr?>C5%O|1M<0*-qVE6X0{7mbjdi|_Q~LG@t@EOUKpeU+|xV5
cSTH#pqebiY)klKDH&F4J-cfNMtNudW?fNZI=>{(^*7x^;gVgplnR%%YvM9qL=GaH6Y588LGnMgY*^8ederF5^=)rIEt@p|K
s)WCW5G+PVH=^^U0y^!CZS{#h~tTB#s3*W>o*6K~Dc^;n8H?OoD4rc~D8oQRN}R3)Pl(YwO00gAt%>KY}&fJF|Snd7n`;JOy
2nObz<23|Ir+A1`s=Jv-eE)|+nvlJ(T2P%jA`3lPNz4<(2()3lW_IVD?=ed3Ot|YD?@-Ty8YrFkgjywbme)aGj%@-D$TQY|B
^*vn{4Q;x*2H#KNED_rA2%lJ8b8}&69EKy(B1&*ESo4D2e)X<FT)k@Q@GK^Zz7rJO;)i>y`K5KW#9=Ipgxxcb<Iwz-VYwN<b
l^}Uz83j8*GSg;Iqx_m)e}>P<Uj87t_i-jPmm|6tgSI`WMK|Et6Ln_)ERTd@=T`8$=rcOk;GDG07hiM$xUnR2cK9y=>2vn6N
19BYF8h;{nnIVq9UK0ci_rZ)4<f9ciyT&fhHfP{_kPt0g5`nd_y(!{?!2up;0(R*uMArYgRDH)a^VLsgj;)<UHoE<^$w%PN?
biL2~anYoSTQcD{RSUawwMRfcV!c#En@!*;%V+#Vb_VGVL#U}W(H3(ca@6^@PM-f-BgkX$8iK9-%S3b=CKsy#Yh^Bzfcu=Ko
2GWW^zGjHCSzw7FQh#L%6y$Dn)e8;NwBIa7z+WX{eiRne|-%Acm2Nu`R>j7;k!XO^=)9v{aqR<bug5>kfG<4*2Vj!Ag-ea{#
!^|=BUh*=Ga~S(AJQGW8rLa|ZpdL)-m6?s4yx*z_dA@mo(vJ16md8{P(wRS}bvHwEoD^4@_7b1Bru6!|$`$09U**WCLy^_5Q
rvhPmP(5!;>(}B-idk^j*mGoyr)i(1^UZ*MNCL&4xG5H7mU4%EUA)^M=9=8mWLyA%~_o4jPBFkmFpjwYtF|%{I*8se|EP>9o
Q9?Gr#eU%cPMBowGda8DNdq=V{FphWq3`w{b-pBrGuX3so47pNsuxC#P!^I8Cxudwm>^c1yEvuTo~9Lp|$&Le0Nd|LxcC_+5
YRvxtQ`KzQwZ*x(#wR4u$l7S8)+gKHxq%U&dfR0}>fuvz!u=HHnCt&9sFS6)a)=k%5s%bRbU(ncDiE5vYctco9R4E=U#)nGO
f39fa%=D-1JF=v@z$}lW;wy0$~9cg90tT%_vaY?k7>`<z^@$$Flo4cVyIn+Dt{q`$Qm*#F#(qP?e(W*07pc~!;8%CL;M=D}>
)+7@B8$DNH^|=bO0EJvO)Sj-<VE7mv4OLVhR#rylh@`4#=L4aRu75a+y`yq6*5TeF!XMm9^$gyd88>=eYifmq_K<dk#s1y9d
{oRZtZw+!%~|14D?Wai<S<S~Z%c-0NX0IPaya^ZUOW>~X8C2=Ocbu%baOvC)C5U)thcZb@`g)kfV(TFg>2iY_+tG*mLuK*0m
#24`<3_HAO^Yhu;P9Itfj=EF7W3@GavR+H{^`Jk>#HH`Jb2CU0YPHkJi!=`3wv2o>;#lmAT<4>dZ|Yjn;qoFrwtv5Jpk9oku
3IKYVWe8KqO#2;;@yeWttc&cs%AmSRbEY-ahfNvfAc(@Je_csrJ;HTlMyG{p7x*pv(7LOxAHbYzoOk@>S0qhKRXt4E%`HIK#
PmHf~5O#YEd&Fz`vQYwn!7IE(zWsieMJx?H1_x&LXuowOZO!LWZ<e#CTABHA!Ypc)r7QS517)}-wu5jhb)HQqQy!W<iNTkFf
9gcFCT9=Ig*VV|i0d-%-77VGSB8z!^_m12DBP}yGJn6!kK%#Cvlzc_|v(iA-U)(()QtuEka?uoK3L~$~I@f_j8gJP=zwlUD%
}k}QyzR=1v|Q!;9moA6(g@6Ks^s~vS<a>R4yCIWq9b!II6I+PToev_ALlRvS{}IBB2po`b;NvVB9oy1ess29CFt(`wu1>F*&
pG;ccc<->wbi56{2bhH?q*c#qstcy{fe=zf|Y+gu^)0l0Sy3$07?29RDF&3kOCjq&0^pF%fohj|;$&`VpY`lko8~3$g(JBD>
U(gw_`Ab-b*FG4B0@=9sQB4y^>Qj8h@mvUjgIn!c#o=^(P~-QVZDW2dsv_c^WrjnwHtU|Jd;a9}wFFZdQ}e4|xV;Bc&7P1LX
xX@sH<?E{C?T5nlQYMnoQzTeF@k9z!~PmuL_<<7yN*ZwlwJ)m-E&+);#KGoucIX--!&a2gW9!D9xJvo4*{Kuhn`}U5j6C-te
fK8oaaK5~1fAV1Udf=G3JjjguOyg@7K1_qG+bb7dO=m;;ft-s{<fa+#!=TjT@&OKmXB<+Xl;9vI%9h|T$5NKy=uNT8Uk}rCJ
`(hF%x`)%r(NrF+Wayqn=Ki&To}1d1~N#VGpZ>colEc5XCP{7_S%?39Sn9RG(oInFJ@KI71lt4=E)BSj`4Bt9BWmay9@7%mA
G)&^9D<%Mzhl)OU1CcPhdQ{e|EfALUZVRTJ<KTp6XLSJCExW){LqWl<thYnhURi3-4TB;R9?pQ%gA9ndBn}8;o?(Tp+Ba7Fq
7i+qmRihsml{bIG;3T1j@j7e`Z>j?Fu=OHHjq;>J*@vf2N*I&3{UU4_@V|LK1j8mig+mxo}bY}uq=;zMhi2}Jm@^#{I0_5Li
%d}yqC@Akoh`2G;RcdUAE!1UzA)q3yPvVMBOaIxvVCC3OAVq(>MBP4}#v9ZNni|+LFk;70Vt<G>76kF`GsKsv=$)d)lHD7*s
emo&N6r0u@i5|8cSY9>BElho}V%3_<$GK@g)|gN+n8FE?!<LZr;fTX<BRN>$ej>3tdS&x2JO0F3Wb;8jie%6`sNU}&+iwZ;I
?D%@;xB6<m*(9j&77jJSzN{3!+XohY%UbDVC7)(6gxj8BsR8?)Z4E#2QA>5CWy=OI}sS_n_2HX!Pl$(v^0S8zpdKd`3b1!*t
&+*>l$XU0`uSLZ)+~cKUdy8Qf2Mj61S1Z>QDt~|LlpwkofK$D|f6KO;LzrL)pZl>WOKkL2=}qT6W-2O%#RWFo2^WxO9g#-tr
uBEQj&pt~k^`?v)R0U5z-frOBbb0e~&#*3zj1i%fXB=l^z^jhsvx*Z*_^7npZdCo?Xf3ycH>;^*nXbt`y5xx(1|p+v%RUW2%
Lvz2K)-}BC)RtJzIW)}|*ETX{Wx_8JC1S&)0F6*(?NtAGOD&fbPzfmx6KjMrcuk1T;0#_~XET959!dI!{Bi7hYVUVE3q>HNM
QfIike5`?=5L=xcQ_b<;3vb7g>K#*_^~18aV_Dislw0F)q_A1NOZP9ayeT-W6joEt$L_{^x@wv=^6v%W%s4DG?_|N-ZxkrVe
w*Df6j0Cdtn%Ke+8;N+I<P*_T-N{?nMvl|p=EvwIX{uX&{B)Vr4?2buG~=dL6hX!tWKc9nq8Xvug9kF-r;4d6b>|v2bED`^<
WdlQa*TZ7*;<K?+PM^VQcBhTU1R!IC_QTCv;ir8adzGyXFelSe@9w1b;qp;HbAUWe50JmHDu@iq{hBU9aAix@F4~j*;$)hHQ
c;uhCtxf(%csvv+i5RAFzu%MQ`8D(sP1Y4gbG9qGcc(r*gKT13s+a#fKF^>Ges!{P#X&_P`70vD?bT<6QQN1U`|<;IprAk@c
gVb6P@5$4!DTi^=8*uor7p1fyN!yG^U@L^#@w^urIyBszS-XrOVS`@hZmvLN4KgZDi6A|c=UXCH$Vb;4+pi8pA9><*}QGw2l
y-BQ$!{#TU>YdXwr$r2}l4Aao{?FIcQBCf!`x7-wJb8ad?n<H|Hj+lPb9&vn|4E*m#EK@^`jq!3u_zn+e)%>fR-FELntSOcD
)>qE4w{2nV2$Y$Z??zlL51C7i^}cEYNc@=DN*68z2M~JN!076b#*LxclhYEirgZ<%!x@ji*wwSKB<;)5|ePifGyIYg$NtJUE
#KYM7=`9zY&&wHgXkVgvNbac3?OK9PpPXF$w2(lQUpRwS<$H&&Hi&oi@1jG|CelB`N?(fB^RlOsv%U_GN45BR$Jn3d}}c=5i
8~aD=uW^ZkZ|^Zazo<HQq{aO7Q)77lDY@$s|+i-aSon2#2Tnm17)N4!<NTc|>gPym(#6)=RHha=8uBr4=cpp8cYHDqM`40NW
ab<AZ-N;GOD+tX5#Q-M`V`g+>0&Q$n*Tzz0PRnE<3KiycdH|W#U1oBt0xg}cL;1tRs_S;GIZ>y)s#*{}~!S5sbk-7vX#<4%F
ddE#eGenI{gA5YOKytOqC6%O_u}ze*ZU6q(x!j>6?<HTqyCyLwi6&piZ&OV{(#tPGXQjzSo_4r<ZDLHXq!@TubzqH!X@9tN;
27C$yyqmfvQFIaoM_Mon9>{%Gd%J-%+{R60gTEgKU_Fe-YuW1VHg(Db2sn9frf~$doXG>u6fr#4Z(fgBOZmrv05A`Q<|u#Al
DyuAbQa1LGP%PLxd8Q6S74utJ4$n>RFuTyjxN&2qY>9pf|NwBq|7iEY0)h9fCmi+ic!y&XCe;4n2n;(7f2;D(%Ds0bzV3D4W
Cf8aUTL^SX$z{b|;LQ-l!EOz8^4Q+d*#SOeDpEHj1Wq(XV~)fpPt8mYQAF~vp0Bok9MXjuq_EB92zl<y<no@yx0{<Jr`hO#b
`orebpR_RTAMjRMwtqpHiAf{-|$7u&n)W7ZTXAajbU*I;ELm;NKer(gK!U}7O3Fo3%?v)Q~&TCNZ9+hP2+b{De$A)`rz#3Rp
LhjZ73HO`R4|Rd+-p@OS)q;p5i7Q`o^@0LPzaMenSeb0TB?5!uZ@{;U!>Va?I9Y+~@{Yat;YM#+$a^8O-CGzE9ip5?D1DGBI
CRIV1xriS^0edJ67%1>ec~yN07nWx@eQr1g<6NkCRQ^$Z+^%6GM&aZNn{pR1}5qp*8M#0!$KH;{AymDI&gw03vT{SG`<PE1E
;z*m;xoMLebmO@+BGEeSbE9!$R+dlGWQw{&DR0&pU3!Ow5x^7<?K+oLW59*_oUtqJ~H<zlfN%sUv(^t%gXgNd5doK9})uZ`6
tz<ryDS%P(@ch*~QghUw-f4>3%w3jO>9S39MuVSqO7X54`z@Tpm@%EC!av!vPcnY>3}$!#)!mIeF}Sg!x4@7d7GFh~!Q8?gO
O&FpNyUA`r$6?ObF!9y!j4fV1O{Raoe)U4IE9%XHJY84fQ<2hfa$0VpeCYt|PLR99K>0ShOOn7RLRISNb#j$eI>eS4|2vl9?
y>Uz~N4c&gwFJkT^8-_L5sqN+<#In@8QR=M^ZO6Fs9INW><@oDQ_FvBz3H0QH5qI3?!f6C>Bn<7cW7B86%39jD_SV9CKkDVF
HsZ@T)Cmp5}`MJ*Hg8FBTeevvIC>`-kP^1p%wzV#P>OzK&RW<{K#P#7<WE%H>XrpP)LKe`N@IhIGm4iTANzf(AF-ZTG*;Z)v
FfSHSoxx)|3Cb?h!PtSa|W?hhq%}fB%$^xfyPe=u2?t{5)&~_g}oj8Y^3|fzo0euAF*SkUn3SY`8a;{emZmOReVM)&Z&vm6d
a;sbUN+>#$Ob_nG?Hy4<IGs_{OOA27$148dtF8~Ll;RMDX~)qWz<iq@I{WNe#fMog>qL}_Lh-fTat&~-yfUr1xlMMFy8_VhM
4LZ+J1S15?zTj<Z{|ImU?OmqWUsC}KjV#wKR^Zuu2Dv{UCy1jBYGAk}l7F}Nyt(iaLj`XSKA;AQlJAqWc*%B}UuBm=!*89c1
O1+a_IRy@<ni|j<n!1uc%%Pw)hbs?`mcWtT4lz+R-N`49RA>nt`Qf8Q2bSx)KhNC}(oENO+1lVVRt?N6v)r#n;55kQZOKtzc
@ESrEkxhhNhq5nT@9C^Q!`47nURKLq-K;BYg3MS8iuvD1S;WjIKhOwqaP_;&p33NSShX6WTmFbD!w^AEn2EUX3zKc-Z?-X;p
X_)zBHIq*QOQ+_IS)iuGF+iXMeq?7}_Kx+R2Hz!*B!XsM`CSY^yfu><<qGNj1kX(lnl4Ixzg*M%<Aj`Kqq@KT#(&v#*~cfQ4
>Vy}z1ejb^H~0K22N-WhCUiAIm79k}uwXdyu9?GW>Z7X3YI7IIlzVljbhwLF~WduzhY5iUHZnR0A{`!1xWpprGd;7=eFR7m&
1m&KCc{21ZsT+(U<RjLXqN%fKy3njf#D{Hd7de6?k=IFKGZjbzKkN$_l@~V>}J%e7)SJ5x&5M~`fI$xRLJhfanHUV<vG#8F|
KOj*aT|6X|l3k<v*wi8gm-pM_hYno1^gR~_>L{3+rs;fbIPpPa7=|iID&5XAg-oidq|W>&j^T3auwS`|nt8;eee%p#N86<uZ
3hm4CRt4_+ODrFD8Zq!-Iu(|N?ILGmRh{nxOeL{k1m{@xWG_@mP)vCZtPDr+6?2OhN)De&AMYJTzi|=dXg3=&>C=t43EW#s_
fu~8VP#Z5BV-!%n<HSm$%NBEw$w6M+Z*z<hZk{BmT$H-PA%=<;iv(aX77IyZJBgibcLXUOUW7ty=7~$T#WD-}oBDPOM`OE-W
Q{7fO-!*)C`j3CWDgB`b@HyPM$os#1-Llg02h0)7qMheb!vRf}vJY1LkZ#Wi$q!5>;(T`ru%8Kt$}g75uA-a49DT-uo{4$rC
p7#?EYi>{%6oskbdT)C~8NDVW<9lxL&{Wm+y^BAWZ-X~AC_+ZgBh?Rt{I9UJ7M7Y!NK21dU<n$K#VB(vDCCV!=EpoVS7l;Pc
q#7A0Vu5#2s-<a-3`B3|qG&qJ^T#ZUaOt8X=Gl&7!))Ym%8=EDX@tWe0!&-GyAB-new$rHxKTr+Hh$^!PL#8Bwo7GXgUhD$R
D%{fbJy_V$<@2j>{c1PurPq*+;3M4q8ig^6{6{ys~~#3)?NL>G(Q^h*jndwUMsE6eQ_{Z?Wsjj01nIRa{Z$_Iq$$inZ`bQ!~
@<S-S_W(IPFb|Xsc?e+SoVpl`DsOBQ=t)_A8ke$jjE@HHnlWtMCe<JGWdm7)4g$wLA8aUa_cFX)sba&}>)Cb0V?=u+Mk1D0$
9dG1?sOU}e|0B+H=FC|27m^V^E73@kFw?kqd72&JQ|MK?hWz|UY@@~ioB06t*#5;%fVt+aW7k)J@<?lqgI9fGXG>-h!k7G~W
#Q@`3`I_${{?fbu|u6exG{WZ&bY7V}iaCD-(tMbA=)k!I=rG=@YLt%AY!N@o)?m{A{oQ_afBO+zaE?jx=0!ze3HChUX8c`wV
-alEI`ND~!RXA36g+g&v&F7QB!VI+MX9nd_hf?}zx)&!KP!E$+-H+Fd2SM!$3mM(tZ~u2tw##d>7@?D!Tl7%E+gLK0b&hWwS
SawRcq=V9aMatClFeS>Sj(Be8>79|3U#^eou{yRPa)lO-i^@rT48m9mx6x|j7XAow&j`PBWu)tH&<+w7QQ4J%Rh(sE&fA^x`
R#qbY_w28ssXG4v#>di;=PB%@n<`sQA)6+6JFXVFX_ff4neqyph(s#26@S_nA4tC@lB++aq<rQCRl=>ZgN=&hu=OYK#<4)xE
}r>zEaFm{wSXu)iPNaiz8F(ESrpMOlDsBT8BTfmKEmv2y1RRhMXs%F`TK@DU=?*eQzCbM`kC#ubj>Yf0<b0>(MGU8lKe0jmO
Ulln?2j2N9i4kncIoNH1Q2Cu|vhe}z!CHtqnh|(ap*g^tCjr@G`?)l-f@W}1GyUy$w?+KIq)D|=@bdR8mJlpiRo7i&2dGso-
-eL797o*6<DcuCELoi!-KZaqUGRN06ceKKZ8oF3mJYW^or@b2mfv$D7N7Wh%%XPi~=wNbJC&sBO9AHc5_OG`Mf*#TfI2&Q^^
?S|T5eKf^h(zn6>`}Ft!iYIL^WGILlzKf0Ns+=@%XEz{282NA$}up9I;B@KhiMkxzyf^~u3Q5ZyzaiYmN_4sz75ONDD!B`V4
+5XH3$p6wPd+ufOdLE$iy~CAy(*3)6!;Lp|YGlglI;)2DRRrur-EY*O|YjRc92I7i)goyCmx7fB%Q-loXbWa5@4jAcyN}5Z~
IRZ!H8n8~H!o6E%lwx7VJUG#s3oOo8@l>Z$c9=6-h`xp<&f)JLqX1Bbm8DCrplhM}|l{^0pxgZ8dhpJz?PdAQqJdZg#MZfTb
DCvq5$ki&*L6#<6*>+FoWfdPgwLL#)7IzeG*BG%edXAZ34U)|Mh2adIz0ma}&VN3SvJyav59F)6!;u&y;21DsT;yrONQVPfD
(PcGKJ{^0#8`XQL#Wd#TJtjsgqTY;qTuLKz_-2j+3X9L??3jb4>O3_)=fL&etSF4Ds~VH6kzZn5TL;<4UUy^F!YR@j4U<#2Y
E9L`**#it*ao$5Y95kl{@YOW+HVu0rB3wS_upv(c!k9bv(GhQfT7FMkH2;$G$8xdIeco(6#CNnxK3d;SN=W0PI0($9ih2uAw
l3IT}NoHx?_(6o*^0*z|Ap!;lDwh&jK76SIQ4q&D2Vl$l1x^{%_~J&qZWV7{^oR{mNwyER_)Qq+C$mqE@YJv8txGUmXs~OrE
P>23{pYN?pwXi~21ek>-xgR`nIDU&_i;#o$@`-BDI(eo(up{pnRZ=%J6Ac&3ryjIIPsJR?^q<jdgDqWm3tSgTfWWMxp-9Y(a
KZnkrW4!N{4sG^Tr_>V4z7U=tW>s?L_#ukmn#f}5Ry>F8GbSMlH&$z>fsSUPuJ?=>8YJ+X>J%Tsou;@INxMfh`hN>ASor%G5
XlJpt&CiHtGu0jY7e36gnf5mN)sJy4RMX36kD{qk;mZVdEK|7h_*hb}k?bKNy(OMz^Si^;sNq+PS*=a_WUB2~dOKLC{y@&Np
p^u-SG<!4l>rLFxo`r@E;Sni#-nluJ&7s1+tWWKb(rm6XJjqQbz0g8XcUYz*m}Q67kU}GnDJI%b(enH@h*O~ra*1GqG)Or-L
cFHp>XSJdH_d-k89un4V(cSnAZo_*r?qXj*c#Df+5Eu99M7W)b6`}*F2nYU=+0Y?h%Dp3+Eo=QQ@%gXd@>agWH8}#y{O@C>f
AFhWQRFT)Br(GRRy!hC!{#*T*!%2rzEn^q<x_6$&g3tx)H=c?S+PM(Fg?fniOaawJu?CU?hfylqUBcpuaFB!yGW@O^jf^sb{
Ot#^H)1+g_ZKewvw0qxwm^=_b6y!d&Khx=~XX;Ha>TJ+-QllS^+G6LBG=LJkH*GXq-$AR^Zb|ixYhmr1S*JB8wsA%sTdv{ce
P+rXXy;D_k&MiA|U4y{giRts|POQ&k^%wsoTH4Uuw;Lnv^-Ju<{8Rhxu(LCZEMv2G&8biLk^1nDG$8ZjWY}RiFcUU%XD}0XU
@?zJQlpb;F@kCncAzF~1m~aMkYf?330q)%oWtT8bGwv}bm>ucgWEf612`)5MiK34{$vMg+s4FJW*s=qvNmy8ZAHI)*y388v{
KpvQ=g_`ehQ27ZE2^y<Sbmx3WV=1IB<e6zwYX&1E=al%xW7899WePWTRkP9#n6+8jrO%^;Xpu#x3t`dCM1Qj?|g%>VyNMI?j
mO+q{SP!y9Ozp=38{PW++MGbv=G`R&w!D`&4}*4gp)@ev(b7VJl>h$}3D!r0*;mEn-r6ktvLeOUHtQViY?hqO?P!ot7)zU4$
PQ0t_?G{|0IIc`(Vm(|Pc7N?QE{Xu33+Tc2wpxO`=s3AyZHP8kWdn4=09Li^%FFP${ZsM^uvF^a&py%_>4n2i{!R^_}0X(dX
2&}r+-nBbAO*Oc|jlzNDU==?!XIH?*wGWS)51$-ZmE<@r8{4t~6{SSAbpuY|FsQwzwdQBvfF_LY8KN^DK68#e(EP5gEuMW$V
DO5f>T3P?P%cg}fcqaGt}CW+<$2LCzkMG1sj!g!A0K=;?frJC^2GoiXk!XPQ<cgxvPZH-1+NGXQpzg^mcaFClQV;Xh2Om!$w
EKry6Up;db^Tn<kbe}0RyXg_I{L$*Fm+3EU+Lsk)MZ)Hx@q-S_;`)2afdTosH3o+jA2%$GgInqpN3?CyzIsj|&GmKiQmm$6<
}1_>UF<QyAj#YU^@_?i*|f-A~(y&7vbmiDmnE+0YYM9-pJl)_Z=H8rAbwBf-(8$7N_Nl6;(HM^RWyH?~H%9T*OXA6wrKI1Cb
Ux8xl+L^8qFy*lB*k@82^Ojj6~o_}t_Ijq%s{=mw4pReXCfe!Z$dmpE;3jO~%L|x4Q?e&kP2<zT&Ls~i8=K7w%^!)nUuTnq<
rso%D7FJ?O;Sh`}tpu!astJtmXB@cRn+rLdY9)X#!^9NrT`e4_l>oh+W!9vo>)*S(lv-MW@4N5bE@ag#XsTR=5i)b;yu6kG=
jt2R2?Uw|rz5qyS!#8KQ%nps0w|nXvh4TUDCc{;(_WmQJFCUN@9v!oWM$8e4%$C2DXcD_pKfR<tiqLhqghBk-Ea??K+O)~7=
VCPHp9$K(s}4=>Lz(P$wlr!4HM|??2RT0xpLfD0<|J^*A_g=P|d%q6`{5Mn706$%xrL&ha35igF#>G>B<Wj$f0DSI;GncXyo
$9&(_z60ed#HJYdqUw^*NAuMhvE=5laIXrPw%dq=|vST^bUlGa#P81(~p<>wtZ!TI6lXTYh}E}YzR;JUi^T+rTm6-cK1nEUe
lFjw>l4Jx!H{SN7nNk8~k^$I#fT54oxjIFZ3F)C_0tgyg|mNw*UO#@lo+WTA0iH+9QpL_+GUZb$D!J$D2p#>pGa;I~W*;V0+
3lXXYr}JU8^Ld*7wnm=r{cJvsIdFvXrQN&F4jjWF8fiButi^49AVV^Tk@$6B@yjvep;be&d6y4SXi2fHcd68z!dfbY=9L4ij
v))2)ehD7KkHq7H$%&r{p}YNFsye_y(JnXJ9pjOS)t3K>J`ZhO`p|Jbt_QqP{+zpedn>E<6{mCx02aR=T@E=H1ReuwrU{!t)
&~fAE5f3**}~w3e`EjJv#5gcq=C^X>g&y>N|hCLNg32T)E&{n11~S7i>avju)xS*$)l<wclnVR}X8wz1E0WU8r+Rs8!spPiy
4}g(N-GDVl~^Q%S8YLReMPx(=6nN&=R(*mnH3L-p+L?Cf~=BC1|!<Ykgv@2yq7&`8SET_5p|u2ip9PWbO<2SzmVjm(Y1HA|s
Oy}m>B`pzcCb`cg{fw`F=RL5_?l%)wZYq`MutqnCTxfCIGVclGuAR8d~`Hdeh7!uGe$#r}E!BfBRcc0u%2JyRbIL}bkfl~Fv
>m64g5w-R%%+gea3Tx_W2-_3}brZMooh1$jT975b&_lm%brhrp-3M5Evx#w7*xI!TiH&h!VH7U6XnJ^s<#6Bc^DvH3b%7dv;
=v81=4&18=a@>`6!%}8GpS4$s^%|SBKESW8CV{CXLFP4%~dn7_U3V8p=EQw`$%hu1IGR?u5-&ksP5+3=-AZN5^NeO4o9Gj-0
weB^&LD3ST1V7X>G7F&9f=-C3a=MP*ZWW&VIo4fUaX*o!mgn7_9Y{<OZ@DH40bmf~xZ1V|zsx5qdn-aiNO3P`%2vn#c}a2Cc
UwdYzLe2NqiPc<5l33(JM&0_}{^bc6~g`Z}<JW`MChy(NR)POjqJQN<3yy&K-`H=}foHK)3ybv*tf^{<^4mAjzzm4*r^6?@l
e*(TBQBWMA>V|*&FhZ=_E`#T?7dvg?q-m$&@!Mn(f>cMON@T>RS_8MvirOwrF@0vC;XFNJC9rl=a%Nkb8hE}x$3?J~`tmK^#
HA9sSCNu|RldZJd>mNy|KU?SHT<{B3f!Q6q<Z6nrT7em=0#n{Gha)uw=FEbKr5x6@pQ8>LH2voBhGe*PV5{ske(AuGrXo9db
YM$ZO5OiM%@q!J3#vH^HCMQd+~I^YS2$?T{RoFubM&5Oe->C>9b{t8mQ)MMH4~IKOTqBS{E)%j^*;lm8W%n;=XT;+v_n~qr}
EFMDqw_r5`~%DvtwxtRUIm9Q+s;WfrU32`RrioYk-yEFtWRk(0U#Ui^c5oro;JKGH|IPN7b~UX8Xns5{czwrNuHd&u!hkS1H
cXAslSCaAj}}-uUU$0@cT6J3v?;Rk*g^y#wiC$ERATSA{E7Ui?*6g{y?*zz3O4UaMf8k2}(_DqK5{r`)_Bs=~E(H$kh;DIBA
)3F-)QIDwiP*>PUrEv2bBo~({NKfc&hq_q#%viYxQ+R%y+lEFa-@Nx(7Y8J{b2ma-zI90t(g&pOrf@@RNv^AnfKwV3Iyl+b3
4h~nI5Or|>@g4=J*a=Y&_JvulGYvBj_EYC!H0`v}A8+t5H~iVoheGw?ux1(-_@BH|e~ms``-PjX!caAI7dOtS4V9#c0(tV(f
kmbk&EeLr1IIA7({iW^C#baeECK8sPC+{)3kHWntE@?hUSwqkP`iPY84$Q;Fn@=lYK3jot9DpU$^!j%WYrGmmbeE}qzc>4pA
RUl&0#H8j(U6?R&6$qoR8F}uJhp{A2>KPG=!_JJ|G1e?Oii-;r5aPi^1>9ulB*61M6|%p<R(W)IkMzzDlx<L{`IKadY5elw}
mr%pH+B)ICde6_)KvVPQ+YZ_%uAfDtBWa4GMk!cp(z3|4PFQOJ#7(9;rBpOzqFBy+_Vd)`lx>XOy_bDORjbjhu&DIboJ;&lK
I`%XosEH=0tiWR9FAB<C!&x}-G3`UO<G{6lhZ9l@LL4hTwtz_#I_yxuSO)UtRnzEu^g-Pz+7G-q{mb>fkDQu+$&YBw+4qUl=
Fh`1tfQtc<C1g2R0CUA*%fUmXu*g!eh{Fsu0Y{o^MXLudJ1{&47X9CDSVb~Y^#ib6kmhF3y_Uc=t;)Q@dqW8gH8M+ct8nF_A
`+;3u*$t-Bdc!tbs2gD4o8}M$f2)VX7TJxbMKN4Ev_CKs7~kcv6aifYkwK6TjCvu_^sxU&WAV+_eP2D$$W<UA-JZg&cGycSa
U87dWSgz3Ta1%Js<@|SQho+c%UyR!V%zs8&eS$I`jBGtJz+LE!Mg9ctJ0))~bIvE>_9zPYez;_HhP7;raZq&g~&lwZb#fYV+
-banAKeT5TR9?@z1<CrHEvofU^ugr@T3Zh-5^9y!R8m4{O+$;>U&QMC>+QYE2e`}bZ3IQv1SbS8$R_#nA|*EkGjb>Y-ob4@@
ma^LNR14q4tCLE68RN?hjKMhF^M7iXsTB#RV;sx<t+2m`kwGubBJc?Y`QUW_59u%&rzYz2`6oz?XWR$bhk)}>)J}!2b`6~!7
#^(LBJ8zf;)~`LDRWl(hUUFBINR^G9+0o|WoC8-bswx|2dBr4!k$-hGAB`~JpBlJ=OIN~pQsxW_#SAXH-AAhBLkd=B-s<Ey`
_+d-Eg*NyvUo(6OM7%u(gHhh)v~BYq5b=WyVpe)SpIk3c`Rz0zwh@-=U{{+$&1W|>z3D&S2%*$i~v#&$H@MIOUhv@!+huTwk
nJmT8<$$X1s=4Pf`L4#DlGxa$IBIoGqyq!Xxt!lir+Qn}68YH|HHwAsop~9+v6EG=<#QH?G}`ER6hdm0K_(72&hrCJ&W{N5d
o0nRhi)l!Zr+JM>(`qs`rsT_OvQ&Ur)jZ;<81mT(*q>)5O!t%q>RCGE)aVvCb8jqQwjiLbeGGSbA?oiEc(mVq2FXr724)Q}z
3dc54}qXSp2s2Z}JV-#yztZPT)(6D0ho8~e7k>-6C7`@rJ63bc36**CLq;6z+pQWoaa#&c~m%qUYvY}~WXJPwk*m9(yxSgjh
9!(i(C@!k;x^S%7fVs$LF|I2Y&I?u@;2LRwE4n)K*64+mLHhp!xlFV4;WRoc(g4@2cR4Yrz;*3SYxxSSEXmC{EuExr<;2v^c
;m64HLxTCXGz`d+783eOAGruF-0d<w~0B5uv%Tiqm<v-Jr*haC0V=%w^DQvbKJ{K)84>yYZ};^f}eo{I5m+24)li{YT3vNyv
<V>LA9E>nX{O&>PkhN;UABy6@0PA)QT?`%~$|fMXA2np5L$-W~2-z%MwH9it29sip6T<SUBQVVZfCm*HkwLU!0>BtFmd~XmE
$}BUg2i!EEGe0$Dh6u2`%bi6o=m+~+VJ&(&jx)W=#`MBGlUjvcsi-e4M_)pX_^norAM$8wKn99o(_M-LWT;O7Xpmjmv9oV>m
<m<F&T$BbEudzxx2s$S6SzLjSYo7P$eL02%Y)>_9_9M_&&{&7pBQ7HUkszQS$SbLzg9#%BQ=7o%|%T8|#rl#^kY=Podik<gH
NZ=FvUHT57#h({b*7OP3Sq_UTZ1l@u>IJx_0r=gUUGlQT=H+a?%SzPPyqv9f`DFnds!tlV8prAcjkE-me8*aRSJF1XFP+0sA
U<+81Hh>knJ!ziV{@+-%qV0*!55qVwD>Sn0vcEAykpaOOV%M)T2$dsE3r{8)7W(0+ZWzxQJuHIvmTrP?(@;PbY8Kk#J4YsDs
fBM6{|`dAE(|CvAS1Fx{&6{4+j<#$kv><*$~PE7Io1Jc3d<~RI~J=aW*yu_s%HJ2-~W#daFP=ajTuZ&~SROMLqm<V43jQ4p*
ndS?%00ZcvR?$qa8}uXoK{@ODPYYZY6pzVK`tOMF>^e(&;Q7F)Q!WJ;raa{SgRI%(+^4#!4JryW=c8lo;ZES{z#v);8uLNQ_
CJ7XKo#rH4lz1U#H--a{f0*9Ifoadkfj7Z3(&Zj~wK`mP%gZoYkSi0epgQA5aM{-qO;1px^c+bAXh>Z!(haRzss!wpFxl|{)
K{Zx$L1*(%(OTO(2XKf-3`^e@F|1FB%^_ahR_GZ-B6As9DLt@i1Y&b(pP9ql*s`cg`|_THg{bW1Jq#*1pR1QHTQW@uF}uv|G
jX*MB(}`pSG+`*!a@W;|KY0TSPc=DJA|hviLFBN;uQCWi7lJ<$~nBL<pXT|N%i<uf|jnVezU@1_I(bkBhZJ|J5CuA&Bn38UE
E@e@&9z-o#nK8<eKD3dOJ^_ItB~uJbjRXLDQTwqmIE>+MB@||9^Sqt$DqA<n?ULouQeKd*V$DdiT!3*nq#w-$Ka15SZqik=N
DRk{c9abF`hG_8!Pa^~4%llXIFYd1KXm!vcJNMG1t}BE6-<ZfNSz3GWHA1j1{1z4$PU7wQuJu@tV?i~>+$L7ijUtB;-Oa|f<
k6LXGTxZt|VSe;|VEX|Y2#p)cJ!Gtqa=U9Qcjy6{3SOfuVZ8*InXtt5%gZGZ%$(EO4``o%Z$96||VFKd4C7ImhEG;3Za2<{c
&Xx$=z{DJ89>r52=J+ropC<snz!j($ZZ=6v?~)T}G%<0ug5)(;C*98FokPK@opj@xiJ8A84u&3$S}z&oDC5r61s9=qR5=o>3
$7&ka0r~a;GWFNg?fo2F;`Mi(MncdHpy!F##~7!p>FW5K%kZ4HZrPFTI0a{?@?!tj!rf9Kbsdn9N7HwoMi~C-XSWjayV9ngv
e+2zn>redtspv)G>boQ}W3=Eafk>P7%^B679=704`U2#g3<6(X^ok49Q*lVpGeiR4QtnZJf$VqM$l{q}yICD`oX-FVUOrx%!
7b-8-_Qnx-HtagoAIQ{uCKyz9W~^&(vEi))~srczgp>PBPB;(IySygqQ?$~A~gOP}}M>9f;4c#ys+_Z*s;YWfet?AXYA6<k;
59XK@G#+Cy|noQ^GsRPHjRPCc_4rA3Ok9ZQH#8LnV$4gTHB$mWcV4627sn*RUsvnb`g|@tWuoBge4P3bhEaK$iUoJ)`svK)S
&Vw8<I7_eoe78nN9#oG!Fj1kk`;BN;6V>#<|CpxROH|VXZ`MC24lFx%`E_uZ@~{=7p6;hzQ2BHSwv4C(w;%VK=j*IRkh!G#E
woNYzeRiPJ^s5l(p>n^99I!&d%N6Cuh{#UJ{8Kna>;8_W(-%sNd7ftMxq%>#c11_o#a#iEdm@GE+GOAb2G@P$r8)C@pY2BWG
0$(Lqu|}sRo>?8BszlD6Bm5+CaII7GT-6vy(wets<=6WSyre2evZLzgHYM&j0psvJ-=aA(!*ey`L{q;o_H){4k4O5*5+PEd0
M-{!*HsQSnQuah)5l_)<MT`Qn%AD6TE~$ya=-0q1^6+L$#hD>Mro!ngj)6^4PPi+|<bz53fVe=UpOE)j(+J&kF-(4s^XN-aW
v!HYllI{H!ig~;EC;xEK6ZA7s{^3v)SJEWy0E4HYYmaN#K@w#;w>ZZ0aQKn4JQTHr?)gV@Un}*?<QXwX}D~xcPUo;3?VQ4Hi
sSQJ6V>WJfsG3Pzn9+-AL=;XmL!S6xZ*Dp*aIrV>@{2smThuM1#*G6vwC>sWH4fL_s^WBgy_|_NgWG=wkqN`ee-zeesJxMUx
VPp2?)<rMdP7~b=`K!9Q)5wek-~C6?w6?WrLgLB5dqKP#8}wg$ddw`D*JrB>A-ahYV_cSZixp6vZ$X9`@GsPO9MC-$^G*N(t
L6lj?{nq%Q7t3D~!ma-G0LD5^TkBS)m(+W6dvmOSCo$Yc_A#G8wGN@Jf6&0H|~97?rX>DIE5uR8q(&9Q78YWL#G`?wvCJ@ua
sP{NrhFGx*2ry#*<0niSTIA2gi^;P9=w)r~zku-uQW-!#fz;h?wL^ta_*X+C{(y4p6E%$thE@KvCeCxwM)*`Mxw7<FJs_rIy
$LE$=J>i!HkdW-ZBt?jcH(<5u^n%X7L&UmyV?;~oN@#$Q;=9l}(N=kmGo=x^OV+-xw84f|W_evc^jk0h!=us99hds)|;iyMh
%5cpbNraS(@~b4E!r=)5>eM1lH?6QQ=EYX(zPrSu)Z(njy?BQ_esJJKbr;j?4xHkwKR=#0a2+G3(l6keIy_4CPE^O>EzEaCx
KD5VwX78Ug9B^L!=ccc0-Weecr`-kYE9ph?%40_;|0Q5v-bq00`@r^Sk%uqb|q`-eUkll`M%fG`y?BAj<rMiQiYKX|2H8qz*
Ol*E297gBmL=|(=Wh5Z&vi<&`^)*ey}P4+5-8{kEj+}VPt|@Z(fc$aAZ0zueXSC$L|LD&+|Km`z_bMbGbu%ht+pQ3ls?PK$)
j6^gL-P_{$w1R@Rb+1;$ws_46m1#R4$Y9&@{NS5)C~GD}^(V=EfoT6!mW<q_7+rXE#KJ<1}Z*|?&tCI!W0N{BT5G|OQajLyV
6_cI?hIE?kh&f_hG)rtJ##ybmf^^{^$Xwp18u~o@#-(ApbSqfV~fz)z+IN~j!5xuzKBM<bw@a~w_w^~@GX+so_5n$h0;+w5`
#2&AyqiI}SvpTk-)UD-NZ&@_z?x7d-Tq0^ordD$%hCE+cZ$><lEYB}1n4_>9!uxmK(H#et9lO6msSXCielM`MENS(!Xr1amz
o=_LY?Y>4H#anEkixO1R-z1g?XA^tnnqk6TeVcPqA#M328W|JB_Gx7nb5>u-#>#7=Uo%c>6bH94_=W0+<u%ba!z64OR1XwSZ
<=&3Oa*-I_G;+&(b!Gk2!GV-e|Jso6)>LpCz8|!N}#jRZct|XEm4QhjX%ePgXD(hD(;8Kw+f9-Goh#ZM-HDTfP%lA-XI`B!)
^!%uyh=ip|YwVd_U5I8rAh%9dM=WYpJUy4tw9Msl3hNG6M#9Sw~PoVp#DCc?tU?M$AHa~MyVgt&Sm2YEbwk0McwOsM~a$c8g
#yf>(b^)6Y#V=HqkB|~W}OI%&%IJPp!rDYlFUF-W0Q5aYC2T%AEXLXFP-s9sOuB)Q}E0PSj9`;~1Z^#Mo7d)d3K&@I%FzdfR
Z>{Mibv#(;zQXY0UqZ?_7J8vALgj`Rc4-Eb*s2e=Kg>Pf&{t2>a0%bl{<h@6NUw71JWQ4>>wM9<h^Y1YMr`(AL>@M)L`XCZ;
rIQjrZ}h<KiV79-lk+_eVgOs4qUlsntNF|!n+B&pkP*EFxdR%z?zJXYf<}Ef4gHp=-kvMzPQgd3R%_P&%6#uY*l}s??r<<>A
<maoa5sh)|jseoK?1Jn&QG=^J-4G*5ib!;?|1YLb~az0f#kpR_qr1FiYVXTd~`$cUi4v^=^f%b=Sw8-jS9Q+dXq&ljZlfD4d
lVqaGA-btT!@O2xKCZ*{gnmql|6iy@)MP3M%xfR*8;PBpPrlFf$32w$98d+&Y`U13&9R_?uX)-<>97aoosTQS+ZS#1~-c6MR
a!9)dl^bLV<9InITL`<y?EHb*RrE`6fj9)sYjB>4-#kY#F)|>4WB!K4hTz6@v?CB9(#n`4fSka+b)C_#wISldso>qs6t=6jO
gF5rKc~w}X>}((Y+*89vx!z#$CC7^+5Z0<$e5=DMwNtJ)>9S~A4t#u*E{m3FeS9p3B*a#u6$L-e_zBb+t$*KnZ&PaCFOX|E3
>_BVj8t8kCuzL0z+k&UOq^p>&30?_tEBdKL?VrTMOKPFB@A*`UOj&eE`=y4j6x0jl8=&3tgd%@nubRxtc4m{yVDLFTcL&@4x
FG0gm?K69gE~n<NOF~)iD%*;jd3M+g($(`}&fbHUWpourhGF$sa=S)nKxmlhnuvr3;%2Co5^Dj+JkFiHXU?f@;PY|3*lVqo@
tcHDtQU;_?+n&auVLqyr-qb4cx%mx4ph^S1INB3@^C0kP7<(rhWaqIRfh@ZRYXDhd)!VyMw(XMY@qF;v^w;)H`~QWXb_w5Hg
@iUY?m97~iME!eFsR3q<?OU~};0?jd?ut*s9A06r(YPQh+vJibk2-{e5cMtCV^aF5p){Ssrx%4yp^8NPuSEog&mCNS&?7=U$
msXlb8@ACETdcgZ%%T^)n`hKQWp528oLCrIe$6~Q+#71JTU=xgM?L#Lhhvas=bDCyK|{jfBk#9`@fe-%Zp=AwJzF9N)~eg?&
_dNMw!Zy!U`<1bs0t1TN|yO+C9qtK>GS+6It)j8r<}7DiUkx4?!w}^CV|GxhgV@)wIBCBEbQ9jvZH<v)h%#RMT%jekDWV+7j
U{^4t5F$NEnQH5+Oc}wDQuA?&NIlfYULV*3fw(!^pxZ9AY0vmbf@i!}`=bL~<B`X()%ncp`aVy~1^54De1-1m;(QOJ_I@h>#
(6(lK=dR)t;XaN2?84g9)+<P{u7GVw2tG$63%c%_*V6pnGZT9@k%oT$U@;YSBfkqE4H@twd{ZC=8-kfpn92exYS0`up&y4rk
f)#kw(u#9%GRhvg}%=u3ai~2__OWatJm2|(Im8!=M9J<MCW2@&bZ&qF})MKu#o_ptT6j6!2d|WxMNLzxx<_9LQIRBB8HudKS
)YAI=bj&5Sz{2fs#=Bo8>9VW$%=l4zeT7ypjIAbo7W%?wua4H^M`F5bPv1H%glCYCZ<CCkfWfI4T)xRx@m)s8F|bwZD>RirY
~|j&BUjJ2({y5C^@-E?^t<mcNU}Vru~mF;olOn|vakTlygwarqe@os-9}gDRIKQGYiCi6BXnCeiu``xJ2QB}TSvQGr(i|j<%
w=@Y(L-cWRW=4{wjfnmN!306v^hl^hq^dh@}uxSm^BSds;qCVe#F3Sf#n*0K?DemKSo3t$4lUWAu)7sK&YxB!FkIzL-+NSk#
jot~@)D6}G35;R>rD(>Lklc)LKY&KX?h?E<wrYd)JbI&Y0t`C=~cl8b-yHT$HGtz?;&WNH7<Cq_Cjp^BORQopC)M=jzfU*P6
dD*vu<epMA7tZJ;PO-<@B&YU{~i|<AtPNs?|!ALW{)PEg|-w#yB->)iJ>Mi`;{bXM@DZw!e^4;}02TovG!L}$1CRHiuDwn#Y
D5uje*iwX{O12p>3aA0?pEsb<W$T?(uQ&8??eB-&4>z!ST{n{h1xf){33R*frC5S3y7`RMbSO0qwY<~Hjsqhh<z}CmE5S&#d
y1j>zImlSnx(QY;gsN19bwKdsMB7lbzNoVow1jGHwXs%wQ)+ko6(|{+tDH%1i3{o!J)h8rKVAC(MxcgTl5l~yu9e8K51^zOK
`nv(Mzox?xGi)s=qAGah`)2v`|LEZkf!NQpmC>g>IT-(&0aXET}Wv%HrJ()Md(G!utdowYhriyzqfC0IQdIH{M}bn9|EFnuS
u~1O;?Er`rxJJ2fHE=sz4-OEqyBUC!0u5pWJUUf1Dgq6k}B(R{(2VK~fD`4SxEsC)^Ib5y<rCpju#g40(~`BJxfj>;F{#Lge
2S*hq*E`-AlpVIcp6OqK};nQrLQgP#!@{a*XuGAiHA5J?kEP2!IpUV!6Fo;R+MgTZL3Yq5hPX|slHgo&Tf$Q)HhV_ZV4fv$c
1ge0cAX+%(x+}nVPv_lb2{p^1-#bJ(4<C*+k@lH0F%ixs$G6`*QJvihzjUdck$)mkFgU4qFy(;*7@Q=-h&VWNSWO^HjyfQK)
d=bU&ZY)13$;JH^h--R&iv=CRp!pcm1%(M@Z)Q<dejOxU{dAYr~+GwA6y*{Ycgg>?hsg=<=|v26_+a5VXN*AtpW?}j;*?TYh
?mDkMjCgS9g!Cx_g#Bxh#WNb$5}SL8K`Rue0M7M453|1stvv2vuVOYDG<zp=nG$PQlH&%sGbU^}afjWlbzRj8^<T>s=<6S@C
!4U4A#S;_oN3t<#TmOKKX1yMdgJ9M(#s$c0kvJrimU#OBpcno>YvIid&en^IQr;T7^$Xm0T*_c+t~sJuLYx|yt~e_d8Lef=C
&vN&9My3}{)?o;<}o9?ixM$R9luqM%cSXp&3)MSURw8N?zF_>*(Xz7c*JpyOcl;U2;%MPp_!kom1mS9iL2USalss$0na_i#3
f$P0_mz6qJfE{@@e()`cvV2i2w_03XVO?QWSkVI53Y(ir-#;L~B~d1>6<lq5BUhz^luDtV(N+W<hTW@cx-6<+K=tJj2bK%J@
|kA8kF7>JGSU>R7rd!fA;jhy|24Y%QV00CXwBKrQ_a)A{fqT-PbgMxMat~El|n}@DC)&kT?#$6Qs~0Fb1!!*W)Aj@zHe5{yf
gmToH!>6q^y{^yl<?cWNgLEVRmsg3o3`~<7LC7RS6gK1{_t`yp!sAYXZHMBX8c&DRj?Aym^DEZC*?K${z<di__)416%aV_=N
+<8YK7iy#psu$a3y2RF#3S#Hba<VL65mn;uD1ruN4O>WNS|e5)$-lUaH>wcxnH?4+ie&E$bQ99LHqkFBD(1bc8V8!H(uBu$Q
+NKLRSR^BO16nJ{WowKZhILOH6F9(jaT<xcqA7{<0|8BE0+jCqEhr<6eSPX!dy~~>xdKZ=?%q>aXd<_ToqT<Jc-ftUR_;^t3
{dS%rm7*JlKB?X*BQNwR{*G+i_aDQQ&yAl5)z!bXdIF}74`#S2R1YVe(`%!lujruSq7=E!NN;aEt(oce{>Dq4lV0(L7nW<dk
FaJ(+wgtV5%z!Q`C;#+p7OpwZnVx1UpKafngQ0gU;MCD9k`Sa4;{#1xbnm`R5O0rep(`ysI2I^m|^;clp<_7>^Wwk!F_Xd5s
uW@aOc6X28l{!O@4`CKHpg8&usA%;_UYB32r^w^yQUQ<?Nv_a4$7gy<Tg74Me%xEh1E1Ue=TlN!8^EH=osIDUw*z<;gm1Go3
}Ky1e%EHW!sc)#X91pPP4JS>(etE=h-~%fqvF=zUzv-O{@13M+rqIh}FfhUJcPJ`R5yTNd&#1>li{V}-7U-d(m4vF29!)mlE
J8=<9l{=TCHQxw+Z#`67)d3if5c?Rm|{dd1P^C^Re4fO(pud?2`aI}U};XsQKo-Igd#Y@6mpb?b!cU*@6IEI+XbzFdx-Z0DV
yns_Ue{m-ez`CW}bPTxB+Y}0$cMB>n=_D;F;7i=Hbd!^C|Aaa>97d7z3$a^NCmED>%3yw->=%}KLCY1YCN%3%Hi8Kp^kJe-P
(^9`+ZtCyg{mk8iT`rxz>(_ozAe+tuG%72pGD4fs;>0;W`(9sRyb8x`8f}+YhF^$%B#*W<3tLYcnhNoRYRELlM1kzqF>-Uih
aULzN6SW^7)Qp)A%L6qu3$&7rvv|Iei7+QS4KHHQ!O}*{I|@3au@nFXub*O*Qwn5Q>G__Vkpn=(&r`ZWDN4S>-Hxu?*v-n^|
-e0f9!0&MbDuKTueG(<PK(aRNCrD#xK5kv65{08i8UE9G>zUXld@Ed#l~Pjf3P45{Lgdn^NvL5ra%<#3{YYkv-3F%BfTnd_@
LGTTvA2TKUWHCX~TH02I8!wC$b@$$@3bO~I89mSot5NuRhrM@}x>|2a~^Ei7M<ivEpPgd?|2!(a~qu3-_q8Px)*R<8oEuDXS
74Cfa#Gds~Rl?s#oF^^R$jx;p+W}zY#G7<hLVdbh?>?dT=u~s;qI#0bM_%0Yy^``;ICbmJV4{UnziqL|<W=9py(6UefF*kd+
EXmNjI8Pqg;r4HRAg$qazf6L@uIR1^pu^aU$XfxxUq-oPK=7%-~Ng@MQ9h(8HLb2<{A;LmAV!qZN#SoJ|=aDyTtJ*Yy)c@55
z70@`bS$W6&0aZiX7uH(5CU!*KW}Pf&)jC@)eK;W#f+72)LHMXKT_(w9W4Vu!jfQVlna%n5k^spHoZlSXRTnwjn_lKRsMS7@
9fa|M=Xcl^TnxI8i6CP4PA&TLuR9cfxA`0*cY5CcS{CMMW;J~D-LWVw$XK67PWq#n882sXUl5ezS!_m9j1H9Y<@A&Artm5sk
FSVd-g`u+nUdDr{RxgGvPsZ3H_+)tNeq_Sa}=wTl<1gwgDc~#ON+cpV%YhBH^KD=vfoFL=d!bhh=m>mW%*t1_-%j1%Sq&c<T
dcWZRNPlN42cvoPm-m}r-!-RtX_$voUqqH6dgo(Xibx%8kmMNA+bkKrOut|Ipwz<rse0>Nf{|>04ddXj%w_RM=jp(KW2~qDa
U|*;?Mvn*SZ?v@cTu$W?Ixubb<KRc;mxg~vEDah9EPXF-8HBLhc#Dn`~I^7!@c&rA7NC2K_3V486`N<?A<-@r4lUOOH%H9X1
agJN-Ywse6weKEx~d>#H-sGoj&O+v;CBHx#ucU+oxPAM1qywcO$i4%9-hh36<agk90r&qXf%4c6Kk}2;GN8rv;pEMsQs@3<K
tNKM21BCn%HAJbrRuFrf#14x<FumCNcqP9<2xl*{$Oz7=4|C6aie=h$3;)iG>qx$|L@E_;-Po*r+|Wmm6hp>{W86^pMKyUs2
K*B$VHZbl}XcHW;ky@hg%yUWRh1KY~4jXN-$>XtcHul2@N)*nVzRZHZ8xn;sNMrFTgz=K(--}LR+p!jN`x0XhjZ}x1~C0Nhv
;3^g25L9+R0*f$$;{FcEL=lelE!=!`U|F*HUx-QKaEjxG!E;!|?GN8t+uqO`S|Iaik;71QEv=#`&j21;RCobh{leqweE`1M_
5rr1>eMIv*ZR2w2M9J4K7u}fn&m*UpWN%z2`ul{$x`!R)PX|~&@5b#z?L0pZ_R-bMRt)bJAuU#dimYKB#d$4s5Ld^z;&(SVx
*104KP;i`E?GfZ{IA!tOy*`22{Q1cd8<Ww9q0zv-!-q!*Gxz>?JtN5%v-s6$yK(MVuq-B{<0u_7a@t2zv>x=LmZVZsZ7i5r%
$th_IL7Fh|%6a4;ZW#g#XHHUKA<kto9Mnp^u2i7LTijzpE<C`Y17aGWDiB{<2Es1ls!NK^@~=SWluZsbT*5w_A>eF{^8gB*z
}!C{d^6<UNjCc3ycnqe+7(^sC;F1E<>HVpC6r50h1k1oMcj*l+EagL8J!AXvfF2QMzk1oOWA|G99(a7=9MHuk|Lws}zuI2dX
68!%gyV8XwnkD)|{`i=slf0cMAjm44tRjjidRZhWYvRk81m5D-shT9SoErR_(><B4?&+?ot~wPJ^yms47xd@~oD}rv3Y-@7=
n5RldUT~lqo7Au;ATOOF2Tw8h%2uxJ4GY){8$TO$6BQS=#(RHoIB+Roa9b90;joCj=&*z$`QCRJmm=1%$;%sZskt7DvV=nbj
lHUB6rGVca&o|H7*eZFYjG22T7D#fC;VArZ7_YPdUp$VwarTzrnG=;Y2r!y#ogZ$JdFhNe7lMdHojq42N~E?ad!iIE?KF=YS
99*Fg(*PFeg|*@UPxiVG%01x^YkL<LR@CPW1e1rwqIH(pe>Az;C1*q^pHaPLzICJ#o#4(BN2QVR7n><TNZHjVO9SY-0)9c02
``4E>bNBQq%cmma9KTzt$U_9&nz3;uFGY5{fmNyG~4aTtkw6%oDIu57m=IuN<a4_<<IR|cld~L)*n78VJF|0pLeFT$vMTZYY
q5inGFO@l~%Q+ZFx_BTdr;x(3l4h@~dxT<P_+D(OWNC&)LjUoTqx!WFX<gd+QEYdmH-hvoKo-nlSqm>;QUWQ!N<}91$9L(Vm
2mQG|LNwCzIj~#=CPIvKJDLsqyMdhli>KgaA4d6&HlY#tCk8@IK_=X7K2rpY$fcmH!t|jQLsB?1nJhBO{m7SU#`$71ceh+H+
=fM<iJ50B{|l3L3u0{LzN#MDWb3#(02!L=~5Wy*qb8<!|m;NT4FdDAAE21hH{03dNTN6WblKp$F!(IFh2PH{x>P7Tcu+j!((
32R;f3bv`@XAFv+O083B~S^6+-olc+Le-gX^K%#@=CWMgqS=&cfC?o?Q;!_NQc;)1$@O7x5yTVZkPz5gGr#{d}j$k%NUP;+=
fBgEc10%>ZFEUloeaHOxz7(PPAurHIb_@4Go>(`0#30(CVXK*lveZ<@`{*GY$`NfdD%&rQ?pN}JDnoo0@5kxPpJ21T8v)&?6
pILW`Qb8#kP#?pG8*0q3+b4xBV(8ZSZ}_whPVjIla7@?Y($m%zg_Rcc{<o)tg!<`HT2SFNbPjr@1(hcs^665C+5K-E=4@c9`
rUKBykK<$#E35T&b?aZFgyG}mxEFe{NY8HgH;{qtw^q5QL&Q+7;^CAD5NGW^=>Gqld~@jL%=wu?s|97u`%wtFmh$5Xncvnnq
f?{OhQ&m!6eGQYz<BOIY(J4Lwz*_h2g22^G@=HvHd>1=6YFUNFDu-PPVe8$_KD}M+eqW66ARYOQ3{<=Z(X%PUhzvEGT8!gZH
np3O2rYdqzXI_&1dBPhs(JY;05BL%{HGbarXpMX>4!{o|SJ@`Av%bEWAJ$2`?ZVG-xI?_-vc;%AM_2Vsn^_%DVJ$6Vd*AtsQ
%c{F-B`)-lRfO7WTTc@{{Z1rKy2%=1G3O8U!K2E)IV5ykeX**a8J3{XxUq(%w$T!o7IpeT?c_^e!;pRx9?89ssG17>4wA8y*
KiMt&Fg*>trB|nOI6v5{A*c`8x6W3l{IF+}Vgx&I0>UEi6VX7v%bmf_yaNZ#IG;UnV9oFHJ`oMo6CG-+O_yma^DtgIuZ9FJ>
rJM_Dn@L~U?(S2Qd==1WECSeW*mcT0>rm?GK(-)I8s&uuIEpnCNduG_D*my;7p0un@}pNIR_S%_uvCpWsHwd4#vk<uZcs%_z
2+xV`SW+o{um@wLwTB<0HIzjc~S0uZ123I7K*|4wJVj43~-I`by#*g_Tq9X0UxvB?HF;);aS424fVI9hsK-0Nj|!1P6~$^#I
LC1-IsU?eCBelWtTmU4`=xsQiUe@(7CyqbUvb+<w8h{W2C}CcfCjN4ZCXal^^i`n2|&{&!OU-)fy&Z}#6qZaC-F)L|}15~Ef
L#tkRw=``R!7&jb(x7&Q;HOxSmR~Thpi0aKLqh}MdyJ#6_If^-Ww9hn3506pH3B<v?xA<o6jt5`QdGAPBF1@V)_89(~ZX`zh
hMj_H2}b+|6P7w(jQFkh@DW+ZGHJCz+rcQ=@GwRJ5Tbe;$SB!B<V!c}jB`zxEtrEm80VU78MqVVFwD}E!I%Ad@?L2Q^<2i>U
)rRjXw;8FiDlNeyisVWf>*D>b@X{dHppjQMRSJVp>z<3V>R*_VS~bnmd{weaNraN6t~=>tS(p^Gs48stQUh8{<@m!wO6SaG!
eMX6Au&?Ax{4C-dr;+uHMj;afOwUp;)sm_i7mT-ITXdpxQp|Z+qKJ)5>7r;cV~L!e7&?lvhaMTB$*W#OiPsPb({1dbmzsO-l
@%&6UZ)w3clK58xRe8uFpkJJ$bRcrR&y_>yW@rY6S<oBZ@V90O~!xJaFzf2L2<^5x%u<FXM4OMl~dFinp=(**BkxZgCY*90T
g1R<C<vzjB-1QCx+9ZQjFf`g@V2UAxXt;wRWI=r0STh%}%vz>Hcvy3@rYB_bsMp|<W7ThSOB=6FXsx`*|kL9I0Aat}Hav>ma
R_tP=MwMZ(*t{99#4;S~UBxIprwk{$tzDftu!!uT?O>o^yQY}l49RW4$e{{#BoCJa0(dMCjkrF|Io?aXv7yZl$5Y0l7h?+RG
6uVDW2}yq;pD|QZmEn}C9YMkYk*JP!9YjZaWFB?Z;n71PT(jRFROBPiC*_a8uyU3sNM!;9EJI%Q-;&x(ka8CxOB>Jqqua+a5
J}b%2+G6bc$FQFP%zCP1Yis7%q=u>c|i-O>-Gea;v9|r7x-pGea!AD3YV+u|{!ul;LJ>d6cnMaS4=QEkwZwnsGh!j*2OMmGm
iXIx%cYHuR_!jbO69X%eJ}#Zir-jN1VWU>r`g+AVcTM3(rty3k+$<G@H<-V;}6?SZFRkD}FUw);;ICY{Y8C282Rzq*R>q`F7
MnV}DTu{Iq|o1&`I?BC8FlV|*2mh{y*<$t7cZfl6i<uGcF9u$L_@~~L^e@<ydLWSjiZ)w->EnC&61s?`!<}#lwV;YorBT368
zL0nv9Kwu}dZxh;4}BrD{&p~?L4{8pH~~y6bSs>yqh;Z}1IyQadyqZ6q-thdfl^Ce*r<#&Z||UPEoJP1_PnZI9Gh$8_JAhM3
am*<zpf#P3V(#cS|w_9K2~5e{fEIOg=mu4vXu-j1b%5yM{KD|V*ANn<_xv`cbcQP+IxHQ4^3r`7B&$_Sg~&_O=Z5i-;wRbKH
Xw#i^(>Dl|!~79hSyq=SN+EpBr_hLp+=V4hL5#m8K0>D3zwoI-!(WXR8QSM*V&kYuvQh3RsWzj)z!ds0UvrX~9Q@5iES&-(G
fLSpl;<5}QV0W5}L*ehW4xo!-g<rM(6mjbQQtTBz~GW-%%=-y882o!;G=11Cx|viJ}7o6*x?9{yoa8VoS~v#3bq;Ru7`S3yP
`1FBpXPP7*1FAq+&H}$b+d?_FH#E1XvD1wr;?e;Gx`IE4MmQRa;sd-qvXx=EGze!AGF~&O&qjcP~Hy4^zB7c0eL*YioocZzH
vx9_h%9^r0I?4#^0JCpXy-oYC-1mSCzFzn+*5ADk%ig*B_VMD_fi)9B_+%cJe$p0ATd~Nw9j))@<e`Kf!!%%a{734i>R$i7K
LXVWZ-J75KYCZ2^qX#KcE<w0Z!pdf?8h#wZpmJouZ(8eP&W7j37|NBJXD4B>w53qmiWQTuVVS>P;*MUcU85pEOmJ4X_RDrmU
^%Za|AZA;?@eAVS2Rez*Z*l%@K#~f`WTJEI<A6>W&LEQDR{69Ks9D28QDY>bP9OGV)SwQ9E>~IhYZ7zSN;$?+;C9{1+Aa1#O
XnWV<)BS7o#d$xs&pk1rcxq`sBkMZ+ieaGVnMa0Y9Tsa&s1xo9@}xPJ7S9Xxvim6Ht}L8iF5JHNyAeKo?+@`s^+F8<(er#G&
rWq#{1Y$T`D)n>`VIEnte<}l*B_E!-c+&-kESKkIgUk)6&Rydl&D_yDc)^d3Dm^+|j!|fJ68kRi}EPEH)7(IZ7*}Gk?yJy+E
ol|}nZ7I0ftW*dZLp23APtX`F1s4@3gaqv9imvE1oAuGOmf@%<nLM28wkEf+gUuP?K<TY?WjA+swp4dE<RM;8qE=&v6kpaxH
8Yu-(I>kyV}Mr}1mN3JhyXE1)ALTFM6(KRUO6xrcdrJYwVEt%0d7OtM9%p&J6bf9PC|}s8nKHOP35XDZ$x_W2)KnXow0G-DE
79aZ7j;_+DWq_d-EV{S=xn2n7pHUd27_Y6-1C^J~j}m#FzUQ-YC%usGolJ?@yc-sUFr+<o38rTj&<BvH~LJjqlY|eQgopRt>
JaswJD+CHB<$h)THvBinOJ#`5&?Ep4F})uM}>&s6i_6~-<2TOPTaTI8<eUr7M?{?X`OreLL(JJ|D9x(rq$b}SE;dZdje%0er
*z@e3sN(GL~QYp2F#!_Kvw%TH%Ah$BC`A>C%sdR`7f+@inj|TSZdPiq!Nv776d>qQJ`n0vjqs{<ZPFCSYar6zVRoHv?(A(|y
J~NFeRl`ue8X>^zZ=O3X<aqe<hF`a)RuX9id6W<uOODe<O;pG2eOUE{J8rFa?29{i*x@>OcT}aXk)htYFYZ9=VM1!C4F4cgR
yL94&&2#m)xH^AcDWxhRr{uYbHXF=Qnf#@fTV8N>(l<156`}`-jIv2E@K>Wu9jQi%<r6ea#!QvVs7wu(t#6H?3RM+4vg$CF?
@bFuo#_FbB`})p7E}rX#?JHT#9qJ33zb0;lM4-+$g8B1h=yK@UmRymuXt3Hk!yaVxq*&xd{8Qb4;wX)F=~gPKpEqc{m>St~z
E?y)BRfzsOFI@15#Vzq;VSNa5b&cgv|>63ChK_x_~=H!<|V+Hrq*#c3gt?g!^QJX0(2;OImK`TVMg4^3rB>M#E4)!PJ!e7B1
?BO=!u1Dg~vNBCbuX_Xc$v)EmD3nT?C^e5>TU{s5nFYyL^WyE^)SPS-!P+<m}%TL=={Q1}sHkXTJ!+BUt|M{QT`!2Vg7SOvp
NIB-PSi(z7#dVd3!FTuDSs5{$u!sxW-E^itR^RJo8#J~2^NPWJ@%N#FnlTiPbyE}eZwD6X9<D>WwvpXlBKSlOvbx<p*27wM1
WEpMrcDoPYr&>XO<;rR^Nrr0mbzD1PRyM%9@7RmQQC@M#||uaRi~TYnuyeizU04JLnF3sD4b|jtcSPQ(o3f#`395!GF^{eT>
+L|>&uc@2o5Y4?R$%Geu;~8^PFk3iQWp@U(ol3q{S~TS>r<Eak2iie)#v{+@JbHKY966zN8b4hPggLJ_d(%C*@`=;GpH@=a(
H=u3M$drPv8v(UBHS&<|j4r{`%4UAnTw<^B26O?2s63nq2D2U~E?sU$Mnu`h{`<eW<M&g0X5e}j)uojcZ>oKuOe?71yk+)ZK
GjXrPC@?Z)Fe01RYkX89>7j|}D$6<Zq?DU>rMcl0;u{b+QtPzunUW~~9%2H(oY_tUy;=&{2B3D?+WBD}#;6!Z$+3C3QmuS^+
xkPs1kg34J<ilFD^rC)ma<F?PR&M61UEKKFS*)n++H~Z>unzLb!QIz#w}y3V{9Ct$yWZ?-mh4-2u?bBqk&&HJ<+=6WSb3H$p
0|*lX15CqU{*gJ-YC(cd{Vyl=A+XhMI3atVU-uZz~UHQn?!uzFtomCjY^A#@`GHqod?M0y?l=4E0-}w$V(cvP<arC2Ze<#@$
vPcWJ$aKGBKf>)YgFmYYaTHuq$xGs4P&V^R}CdR>(f#X2x_Obip_0*vV#pjOMo7)N(-j-L{CUw(0)?ALa~x
"""


# --------------------------------------------------------------------------- 数据模型

@dataclass(frozen=True)
class DivisionHit:
    """一次按年份解析到的行政区划。"""

    code: str
    name: str
    level: str
    valid_from: str
    valid_to: str
    snapshot: str
    basis: str

    @property
    def in_force(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "level": self.level,
            "validFrom": _rev_label(self.valid_from),
            "validTo": _rev_label(self.valid_to),
            "snapshot": _rev_label(self.snapshot),
            "basis": self.basis,
        }


def _rev_label(rev: str) -> str:
    if rev == "latest":
        return "latest"
    if len(rev) == 6 and rev.isdigit():
        return f"{rev[:4]}-{rev[4:]}"
    return rev


def _level_of(code: str) -> str:
    if code.endswith("0000"):
        return "province"
    if code.endswith("00"):
        return "prefecture"
    return "county"


# --------------------------------------------------------------------------- 区间表

@lru_cache(maxsize=1)
def _builtin_table() -> tuple[dict[str, tuple[tuple[int, int, str], ...]], tuple[str, ...]]:
    blob = _DIVISION_B85.replace("\n", "").replace(" ", "")
    text = zlib.decompress(base64.b85decode(blob.encode("ascii"))).decode("utf-8")
    rev_index = {rev: i for i, rev in enumerate(REVISIONS)}
    rows: dict[str, list[tuple[int, int, str]]] = {}
    for line in text.splitlines():
        if not line:
            continue
        code, name, start, end = line.split(",")
        rows.setdefault(code, []).append((rev_index[start], rev_index[end], name))
    frozen = {code: tuple(sorted(items)) for code, items in rows.items()}
    return frozen, REVISIONS


class DivisionIndex:
    """按签发年份解析 6 位地址码。

    行格式 ``(code, name, start_rev_index, end_rev_index)``，区间为闭区间，
    对应"该名称在哪些快照里存在"。一个码可以有多行（撤并后复用、更名）。
    """

    def __init__(
        self,
        rows: dict[str, tuple[tuple[int, int, str], ...]],
        revisions: Iterable[str],
        *,
        name: str = DATASET_NAME,
        coverage: str = DATASET_COVERAGE,
        builtin: bool = True,
    ) -> None:
        self._rows = rows
        self.revisions = tuple(revisions)
        self._rev_index = {rev: i for i, rev in enumerate(self.revisions)}
        self._dated = tuple(r for r in self.revisions if r.isdigit())
        self.name = name
        self.coverage = coverage
        self.builtin = builtin

    # -- 构造 ---------------------------------------------------------------
    @classmethod
    def builtin_index(cls) -> "DivisionIndex":
        rows, revisions = _builtin_table()
        return cls(rows, revisions)

    @classmethod
    def from_file(cls, path: str | Path) -> "DivisionIndex":
        """加载外部快照集。

        schema::

            {
              "schema": 1,
              "revisions": ["198101", "199001", "latest"],
              "divisions": [
                {"code": "110105", "name": "朝阳区", "from": "198101", "to": "latest"},
                ["110223", "通县", "198101", "199001"]
              ]
            }

        ``from``/``to`` 必须是 ``revisions`` 中的修订号；一个码可有多条区间。
        加载后**替换**内置表（而不是合并），避免两套快照编号混淆。
        """
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        revisions = [str(r) for r in payload["revisions"]]
        rev_index = {rev: i for i, rev in enumerate(revisions)}
        rows: dict[str, list[tuple[int, int, str]]] = {}
        for item in payload["divisions"]:
            if isinstance(item, dict):
                code = str(item["code"])
                rec_name = str(item["name"])
                start = str(item["from"])
                end = str(item.get("to", "latest"))
            else:
                code, rec_name, start, end = (str(x) for x in item)
            if start not in rev_index or end not in rev_index:
                raise ValueError(f"未知修订号：{code} {start}..{end}")
            if rev_index[start] > rev_index[end]:
                raise ValueError(f"区间倒置：{code} {start}..{end}")
            rows.setdefault(code, []).append((rev_index[start], rev_index[end], rec_name))
        frozen = {code: tuple(sorted(items)) for code, items in rows.items()}
        return cls(
            frozen,
            revisions,
            name=f"external:{Path(path).name}",
            coverage=f"{_rev_label(revisions[0])} .. {_rev_label(revisions[-1])}",
            builtin=False,
        )

    # -- 查询 ---------------------------------------------------------------
    def snapshot_for(self, year: int | None) -> tuple[str, bool]:
        """返回 (适用快照, 是否早于最早快照)。"""
        if year is None:
            return "latest", False
        if not self._dated:
            return self.revisions[-1], True
        earliest = int(self._dated[0][:4])
        if year < earliest:
            return self._dated[0], True
        chosen = self._dated[0]
        for rev in self._dated:
            if int(rev[:4]) <= year:
                chosen = rev
            else:
                break
        return chosen, False

    def resolve(self, code: str, year: int | None = None) -> tuple[DivisionHit | None, str, bool]:
        """解析 code；返回 (命中, 适用快照, 是否早于数据起点)。"""
        snapshot, approx = self.snapshot_for(year)
        idx = self._rev_index.get(snapshot)
        if idx is None:
            return None, snapshot, approx
        for start, end, name in self._rows.get(code, ()):
            if start <= idx <= end:
                if approx:
                    basis = "snapshot-earliest"
                elif snapshot != "latest":
                    basis = "snapshot"
                else:
                    basis = "latest-baseline"
                return (
                    DivisionHit(
                        code=code,
                        name=name,
                        level=_level_of(code),
                        valid_from=self.revisions[start],
                        valid_to=self.revisions[end],
                        snapshot=snapshot,
                        basis=basis,
                    ),
                    snapshot,
                    approx,
                )
        return None, snapshot, approx

    def lifetimes(self, code: str) -> list[dict[str, str]]:
        return [
            {
                "name": name,
                "from": _rev_label(self.revisions[start]),
                "to": _rev_label(self.revisions[end]),
            }
            for start, end, name in self._rows.get(code, ())
        ]

    def ancestry(self, code: str, year: int | None = None) -> dict[str, str | None]:
        province, _, _ = self.resolve(code[:2] + "0000", year)
        prefecture, _, _ = self.resolve(code[:4] + "00", year)
        county, _, _ = self.resolve(code, year) if _level_of(code) == "county" else (None, "", False)
        city = None
        if prefecture:
            city = province.name if prefecture.name in _PLACEHOLDER_CITIES and province else prefecture.name
        return {
            "province": province.name if province else None,
            "city": city,
            "county": county.name if county else None,
        }

    # -- 元信息 -------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "coverage": self.coverage,
            "revisions": list(self.revisions),
            "builtin": self.builtin,
            "codes": len(self._rows),
        }


# --------------------------------------------------------------------------- 校验

def _checksum(body17: str) -> str:
    return ID_CHECK_CODES[sum(int(ch) * w for ch, w in zip(body17, ID_WEIGHTS)) % 11]


def _check(name: str, ok: bool, detail: str, **extra: Any) -> dict[str, Any]:
    item = {"name": name, "ok": bool(ok), "detail": detail}
    item.update(extra)
    return item


def _parse_birth(raw: str, today: date) -> tuple[date | None, str]:
    if not raw.isdigit() or len(raw) != 8:
        return None, "出生日期段不是 8 位数字"
    year, month, day = int(raw[:4]), int(raw[4:6]), int(raw[6:8])
    if year < 1900:
        return None, f"出生年份 {year} 早于 1900，不符合 18 位证件编码"
    if not 1 <= month <= 12:
        return None, f"月份 {month:02d} 非法"
    if not 1 <= day <= 31:
        return None, f"日期 {day:02d} 非法"
    try:
        born = date(year, month, day)
    except ValueError:
        return None, f"{year}-{month:02d}-{day:02d} 不是真实存在的日期"
    if born > today:
        return None, "出生日期晚于今天"
    return born, ""



# --------------------------------------------------------------------------- 区间候选解析

#: 默认年限窗口：出生年 → 出生年+40（16 周岁首办至 45 周岁，覆盖绝大多数签发情形）
DEFAULT_SPAN = 40

#: 15 位旧号的相关说明（docs/id-card.md §6.3）
WARN_15_DIGIT = (
    "15 位旧号只有 2 位出生年，无法定位世纪（19xx/20xx 二义）",
    "15 位旧号没有校验位，任何一位改错都无法自证",
    "15 位旧号的地址码是升位前版本，升位时可能被改写为当时的市县代码",
    "本脚本不做升位推断：补 19 与校验位都依赖发证地，静默补位等于猜",
)


def _label_year(label: str) -> int | None:
    """``"2002-12"`` → 2002；``"latest"`` → None（开口区间）。"""
    head = (label or "")[:4]
    return int(head) if head.isdigit() else None


def resolve_candidates(
    code: str,
    *,
    birth_year: int,
    index: DivisionIndex,
    span: int = DEFAULT_SPAN,
    issue_year: int | None = None,
) -> dict[str, Any]:
    """按 [birth_year, birth_year+span] 扫该码的生效区间，返回候选集合（不选一个）。

    返回::

        {"code", "status": resolved|ambiguous|unresolved, "confidence": high|medium|low,
         "window": {"birthYear","span","from","to"}, "candidates": [...], "note"}
    """
    window_lo = birth_year
    window_hi = birth_year + span
    lifetimes = index.lifetimes(code)

    if issue_year is not None:
        hit, snapshot, approx = index.resolve(code, issue_year)
        candidates = []
        if hit:
            candidates.append(
                {
                    "name": hit.name,
                    "level": hit.level,
                    "first_year": _label_year(_rev_label(hit.valid_from)),
                    "last_year": _label_year(_rev_label(hit.valid_to)),
                    "from": _rev_label(hit.valid_from),
                    "to": _rev_label(hit.valid_to),
                    "basis": "pinned-issue-year",
                }
            )
        confidence = "low" if approx or not hit else ("high" if hit.basis == "snapshot" else "medium")
        return {
            "code": code,
            "status": "resolved" if hit else "unresolved",
            "confidence": confidence,
            "window": {"birthYear": birth_year, "span": span, "from": window_lo, "to": window_hi},
            "issueYear": issue_year,
            "pinnedSnapshot": _rev_label(snapshot),
            "candidates": candidates,
            "lifetimes": lifetimes,
            "note": "地址码已按 --issue-year 钉在单一年份" if hit else "该码在该签发年份的快照中不存在",
        }

    merged: dict[str, dict[str, Any]] = {}
    for item in lifetimes:
        sy = _label_year(item["from"])
        ey = _label_year(item["to"])
        if sy is None:
            continue
        eff_hi = ey if ey is not None else 9999
        lo, hi = max(sy, window_lo), min(eff_hi, window_hi)
        if lo > hi:
            continue
        key = item["name"]
        cur = merged.get(key)
        if cur is None:
            merged[key] = {
                "name": item["name"],
                "level": _level_of(code),
                "first_year": sy,
                "last_year": ey,
                "from": item["from"],
                "to": item["to"],
                "basis": "snapshot-window",
            }
        else:  # 同名区间合并（更名后再改回、区间被快照切断）
            cur["first_year"] = min(cur["first_year"], sy)
            cur["from"] = item["from"]
            if ey is None:
                cur["last_year"] = None
                cur["to"] = item["to"]
            elif cur["last_year"] is not None:
                cur["last_year"] = max(cur["last_year"], ey)
                cur["to"] = item["to"]

    candidates = sorted(merged.values(), key=lambda c: (c["first_year"], c["name"]))
    earliest = _label_year(_rev_label(index.revisions[0])) or 0
    covered = window_lo >= earliest and window_hi <= int(_rev_label(index.revisions[-1])[:4] or 0 or earliest) + 1

    if not candidates:
        status, confidence = "unresolved", "low"
        if not lifetimes:
            note = (
                f"本地快照集未收录 {code}（{index.coverage}）；若为 2002 年前的旧码，"
                "需用 --divisions 挂载 1981 起的完整快照集"
            )
        else:
            latest_hit, _, _ = index.resolve(code, None)
            hint = (
                f"；该码在 latest 基线上存在（{latest_hit.name}，"
                f"{_rev_label(latest_hit.valid_from)}—{_rev_label(latest_hit.valid_to)}），"
                "说明它落在窗口之外，若证件签发于该区间请用 --issue-year"
                if latest_hit
                else ""
            )
            note = (
                f"{code} 的生效区间与窗口 {window_lo}—{window_hi} 无交集："
                "该码在出生至签发窗口内未被任一快照记录，可能是更早年代的旧码" + hint
            )
    elif len(candidates) == 1:
        status = "resolved"
        open_end = candidates[0]["last_year"] is None
        confidence = "high" if covered and not open_end else "medium"
        note = (
            f"{code} 在窗口内只对应一个区县：{candidates[0]['name']}"
            + ("（区间开口于 latest 基线，2016-07 后的调整未知）" if open_end else "")
        )
    else:
        status, confidence = "ambiguous", "low"
        note = (
            f"{code} 在窗口内对应 {len(candidates)} 个不同的区县，"
            "无法仅凭号码判定，必须补签发年份或参考其他证据"
        )

    return {
        "code": code,
        "status": status,
        "confidence": confidence,
        "window": {"birthYear": birth_year, "span": span, "from": window_lo, "to": window_hi},
        "candidates": candidates,
        "lifetimes": lifetimes,
        "note": note,
    }


# --------------------------------------------------------------------------- 主入口

def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _record_shell(raw: str, index: DivisionIndex) -> dict[str, Any]:
    return {
        "input": raw,
        "valid_format": False,
        "checksum_ok": None,
        "birth_date": None,
        "sex": None,
        "address": None,
        "warnings": [],
        "level": "format",
        "dataset": index.to_dict(),
        "source": f"builtin:{index.name}" if index.builtin else f"file:{index.name}",
        "observed_at": _now_iso(),
    }


def parse_id(
    value: str,
    *,
    issue_year: int | None = None,
    span: int = DEFAULT_SPAN,
    index: DivisionIndex | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """解析一个 18 位身份证号，返回 docs/id-card.md §7.3 形状的记录（不抛异常）。"""
    index = index or DivisionIndex.builtin_index()
    today = today or date.today()
    raw = value or ""
    norm = re.sub(r"\s+", "", raw).upper()
    record = _record_shell(raw, index)
    warnings: list[str] = record["warnings"]

    # 0) 15 位旧号：只解释，不推断 ------------------------------------------
    if len(norm) == 15 and norm.isdigit():
        warnings.extend(WARN_15_DIGIT)
        return record

    # 1) 长度与字符集（format 级） ------------------------------------------
    if len(norm) != 18 or not _ID_RE.match(norm):
        warnings.append(
            f"长度 {len(norm)}、字符集{'合法' if _ID_RE.match(norm) else '非法'}："
            "18 位格式为 6 位地址码 + 8 位出生日期 + 3 位顺序码 + 1 位校验位"
        )
        return record
    record["valid_format"] = True

    body, check_digit = norm[:17], norm[17]
    address_code = body[:6]
    birth_raw = body[6:14]
    sequence = body[14:17]
    sex_digit = int(body[16])
    record["sex"] = "M" if sex_digit % 2 else "F"

    # 2) 出生日期（date 级） ------------------------------------------------
    born, birth_error = _parse_birth(birth_raw, today)
    if born is None:
        record["level"] = "date"
        warnings.append(birth_error)
        return record
    record["birth_date"] = born.isoformat()

    # 3) 校验位（checksum 级） ----------------------------------------------
    expected = _checksum(body)
    record["checksum_ok"] = expected == check_digit
    if not record["checksum_ok"]:
        record["level"] = "checksum"
        warnings.append(f"MOD 11-2 计算值 {expected}，证件值 {check_digit}")
        return record

    # 4) 地址码（address 级，按出生年份窗口扫历史区划） ----------------------
    resolved = resolve_candidates(
        address_code,
        birth_year=born.year,
        index=index,
        span=span,
        issue_year=issue_year,
    )
    province = city = county = None
    if resolved["status"] == "resolved":
        ancestry = index.ancestry(address_code, issue_year)
        province, city, county = ancestry["province"], ancestry["city"], ancestry["county"]

    address = {
        "code": address_code,
        "status": resolved["status"],
        "confidence": resolved["confidence"],
        "window": resolved["window"],
        "candidates": resolved["candidates"],
        "resolved": {"province": province, "city": city, "county": county},
        "note": resolved["note"],
    }
    if issue_year is not None:
        address["issueYear"] = issue_year
    record["address"] = address

    # 结论 -------------------------------------------------------------------
    if resolved["status"] == "resolved":
        record["level"] = "ok"
    else:
        record["level"] = "address"
        warnings.append(resolved["note"])

    if born.year < 1900:
        warnings.append("出生年份早于 1900，不属于 18 位证件编码范围")
    if sequence == "000":
        warnings.append("顺序码为 000，实际签发中极少出现，建议核对原始证件")
    if (today - born).days > 365 * 105:
        warnings.append("出生日期距今超过 105 年，属于罕见情形，建议核对")
    if resolved["candidates"] and all(c["first_year"] for c in resolved["candidates"]):
        firsts = min(c["first_year"] for c in resolved["candidates"])
        if firsts > born.year + span:
            warnings.append(
                f"候选区间最早为 {firsts}，晚于窗口上限 {born.year + span}："
                "号码可能不是按当前区划码段签发的旧证"
            )
    return record


def parse_many(
    values: Iterable[str],
    *,
    issue_year: int | None = None,
    span: int = DEFAULT_SPAN,
    index: DivisionIndex | None = None,
) -> list[dict[str, Any]]:
    index = index or DivisionIndex.builtin_index()
    return [parse_id(v, issue_year=issue_year, span=span, index=index) for v in values]


# --------------------------------------------------------------------------- CLI

def _iter_inputs(args: argparse.Namespace) -> Iterable[str]:
    for value in args.ids:
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
    if not args.ids and not args.input:
        for line in sys.stdin:
            line = line.strip()
            if line and not line.startswith("#"):
                yield line


def main(argv: list[str] | None = None) -> int:
    force_utf8_streams()
    parser = argparse.ArgumentParser(
        prog="id_card_parse.py",
        description="18 位身份证解析 + 按出生年份窗口核对历史行政区划（JSONL 输出）",
    )
    parser.add_argument("ids", nargs="*", help="身份证号（也可从 stdin 逐行读入）")
    parser.add_argument("--input", help="输入文件路径（一行一个号，# 开头为注释）")
    parser.add_argument("--output", help="输出文件路径（默认 stdout）")
    parser.add_argument(
        "--issue-year",
        type=int,
        help="证件签发年份（知道时更准；缺省按出生年 + span 的窗口扫描候选）",
    )
    parser.add_argument(
        "--span",
        type=int,
        default=DEFAULT_SPAN,
        help=f"出生年 → 签发年的年限窗口，默认 {DEFAULT_SPAN}",
    )
    parser.add_argument("--divisions", help="外部快照集 JSON（schema 见 DivisionIndex.from_file）")
    parser.add_argument("--dataset-info", action="store_true", help="只打印数据集元信息后退出")
    args = parser.parse_args(argv)

    if args.span < 0 or args.span > 120:
        return emit(Exit.USAGE, "--span 需在 0..120 之间")

    if args.divisions:
        try:
            index = DivisionIndex.from_file(args.divisions)
        except (OSError, ValueError, KeyError) as exc:
            return emit(Exit.DEPENDENCY_MISSING, f"加载 --divisions 失败：{exc}", path=args.divisions)
    else:
        index = DivisionIndex.builtin_index()

    if args.dataset_info:
        print(json.dumps(index.to_dict(), ensure_ascii=False))
        return 0

    out_handle = None
    try:
        if args.output:
            out_handle = open(args.output, "w", encoding="utf-8", newline="\n")
            sink = out_handle
        else:
            sink = sys.stdout

        total = 0
        usable = 0
        resolved = 0
        for value in _iter_inputs(args):
            record = parse_id(value, issue_year=args.issue_year, span=args.span, index=index)
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
            total += 1
            # 进程级成功只看「这条输入是不是一个能解析的身份证号」；
            # 地址是否定位到区县属于结果质量，已写在 record.address.status 里。
            if record["valid_format"] and record["checksum_ok"]:
                usable += 1
            if (record.get("address") or {}).get("status") == "resolved":
                resolved += 1
        sink.flush()
    finally:
        if out_handle is not None:
            out_handle.close()

    if total == 0:
        return emit(Exit.USAGE, "没有输入（stdin 为空，也未给位置参数或 --input）")
    print(
        f"共 {total} 条，结构与校验通过 {usable}，地址已定位到区县 {resolved}"
        f"（数据集 {index.name}，覆盖 {index.coverage}）",
        file=sys.stderr,
    )
    return Exit.OK if usable == total else Exit.PARTIAL


if __name__ == "__main__":
    sys.exit(main())
