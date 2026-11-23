# 内置 CSL 样式

这些 `.csl` 文件来自 [citation-style-language/styles](https://github.com/citation-style-language/styles)
（2026-11-16 从 `master` 取回）。样式文件由 CSL 项目以 **CC BY-SA 3.0**
（Creative Commons Attribution-ShareAlike 3.0 Unported）发布；本项目把它们当作**数据**
随 LitBench 一起分发，未修改内容，许可证与署名随文件保留。CSL 项目的许可证见
<https://github.com/citation-style-language/styles/blob/master/LICENSE>。

| 文件 | 来源 | SHA-256（2026-11-16） |
| --- | --- | --- |
| `china-national-standard-gb-t-7714-2015-numeric.csl` | citation-style-language/styles `master` | `d9cd04f94bc21a99dc320d0a2230d5a1fb47e3634c17c5820cf975c40db3c0c7` |
| `apa.csl` | 同上 | `3917def5fc951b104d64216c3373096042368b9704ba269ca83c14ebaba889fc` |
| `ieee.csl` | 同上 | `b4c7619fc16c45a31e4cc3271eab94ffe83192d3b4c7fc729470a3b459448de3` |

样式由 `pandoc --citeproc` 渲染（pandoc 只作独立子进程调用，GPL-2.0，不链接进主程序；
见仓库根目录 `THIRD_PARTY.md`）。locale 由 pandoc 内置，不需要额外文件。

**更新方式**：从上游重新下载同名文件，更新上表 SHA-256 与日期，再跑
`server/tests/test_csl_references.py`。不要手改 `.csl` 文件内容——要改规则就 fork 一份
新样式并在此登记来源。
