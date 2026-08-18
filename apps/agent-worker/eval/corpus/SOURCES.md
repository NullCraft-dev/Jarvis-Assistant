# Public corpus sources

当前本地 cache 包含 7 份真实 PDF、1024 页、81,544,761 bytes。正文不进入普通 Git；每份 case
保存下载 URL、许可证、attribution、大小、页数和 SHA-256。

| Case | 页数 | 来源与许可 | 主要覆盖 |
|---|---:|---|---|
| NIST AI RMF 1.0 | 48 | NIST official publication；US Government Work | 层级、callout、定义、跨章节 |
| NASA Systems Engineering Handbook Rev 2 | 356 | NASA NTRS；Public Use Permitted | 长文档、多栏、图表、附录 |
| IRS Publication 15-T (2026) | 71 | IRS；US Government Work | 密集数字表、跨页表头、公式 |
| JOSS PEAT | 4 | Journal of Open Source Software；CC BY 4.0 | 双栏论文、引用、图注 |
| World Bank Data-Driven Development | 177 | World Bank；CC BY 3.0 IGO | 图表、地图、脚注、跨页统计表 |
| UNESCO Generative AI Guidance (Spanish) | 48 | UNESCO；CC BY-SA 3.0 IGO | 西语、重音字符、复杂视觉排版 |
| LOC Daddy-Long-Legs (1913) | 320 | Library of Congress；Public Domain | 扫描 OCR、历史字体、插图、页码噪声 |

World Bank 和 UNESCO 文档含单独标记的第三方图片例外，因此 PDF 只保留在本地评测 cache，不作为
可拆分复用的素材。详细许可说明和官方链接以各 `cases/*/case.json` 为准。
