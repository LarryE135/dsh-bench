# 第三方数据：来源、许可与再分发条件

DSH-Bench v1 的完整题单是 168 道：**54 道自建**（CC0）+ **114 道来自 6 个公开数据集**。
其中 C-Eval 的 8 道因为 **CC BY-NC-SA 4.0**（非商用 + 相同方式共享）**不随仓库发布**，
只披露题号（`manifest/upstream.json`）；要用 `python bench/fetch_data.py --provider ceval` 抓回本地。
**所以本包直接可跑的是 160 道**（104 主测 + 56 候补）。
逐题的对应关系见 `manifest/manifest.csv`（`source` / `source_id` / `license` 三列，160 行无空值）；
不随包发布的 8 道见 `manifest/upstream.json`（`source_id` + 内容哈希）。

| 来源 | 题数 | 维度 | 许可 | 再分发 / 使用条件 |
|---|---|---|---|---|
| LiveCodeBench `code_generation_lite` release_v6 | 33 | 编程 | 数据集卡 CC；题面版权归 LeetCode / AtCoder | **仅限评测研究**。题面经 LCB 重写，非原文照搬；商业用途请自行取得授权 |
| Aider Polyglot（源自 Exercism） | 20 | 编程 | MIT（Exercism 各语言 track） | 保留版权与许可声明即可再分发 |
| SpreadsheetBench Verified-400 | 20 | 日常办公 | CC BY-SA 4.0 | 需署名 + **相同方式共享**（衍生题单也要 CC BY-SA 4.0） |
| MMLU-Pro（test） | 16 | 知识 | MIT | 保留声明即可 |
| C-Eval Hard 子集 | 8（**不随包发布**） | 知识 | **CC BY-NC-SA 4.0** | **非商用**；署名 + 相同方式共享。只披露题号，`bench/fetch_data.py` 本地抓回 |
| LongBench v2（length=short） | 17 | 长任务注意力 | Apache-2.0 | 保留 NOTICE 与许可文本 |
| 自建（office / long-chain / long-needle） | 54 | 办公 / 长任务 | CC0 | 无限制 |

## 本仓库怎么处理这些差异

1. **`LICENSE` 的 MIT 只覆盖代码与 54 道自建题**，不覆盖第三方题面；文件末尾的「范围说明」写明了这点。
2. **`NOTICE`** 逐个数据集给出出处、版本、许可与署名要求。
3. **`manifest.csv` 的 license 列**是逐题事实来源；`selftest`/CI 检查它不为空。
4. **受限题目写成了机制而不是口头提示**：C-Eval 的 8 道题面不进仓库（只留题号 + 内容哈希，抓回脚本自带哈希校验），
   LCB 的「仅限研究」在 README 第 10 节显式提示，不让使用者误以为整包都是 MIT/CC0。

## 没有包含在本包里的数据

- **GPQA Diamond**：Hugging Face 门控数据集，需要在网页同意条款并配置 `HF_TOKEN`。本包**不含**其题目；`./run.sh gpqa` 只在本机已放好 `raw/gpqa_diamond.csv` 时才会生成对应题目，生成物同样受其条款约束。

## 如果你要再分发

- 只发**自建 54 题 + 代码**：可以，MIT/CC0。
- 发**本包全部 160 题**：需要同时满足上表每一行的条件，尤其 LCB 的研究用途限制。
- 发**含 C-Eval 的全量 168 题**：那 8 道是非商用 + 相同方式共享，抓回后由**你自己的使用范围**承担这两条义务，
  不要再放进公开仓库。
- 发**衍生题单**（改过题目/增删题）：SpreadsheetBench 与 C-Eval 的 SA 条款要求衍生作品沿用相同许可。

## 引用

```bibtex
@misc{dsh_bench_v1,
  title  = {DSH-Bench v1: 用自动判分的题测 DSH 换工具面 / 换预设的代价},
  author = {LarryE135},
  year   = {2026},
  url    = {https://github.com/LarryE135/dsh-bench}
}
```

各数据集的原始引用请见其仓库/论文；本文件只做对应关系与合规提示。
