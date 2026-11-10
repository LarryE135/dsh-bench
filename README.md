# DSH-Bench v1

**用一套能自动判分的题，检验 DSH（DeepSeek Harness）「换工具面 / 换预设」到底省不省钱、掉不掉正确率。**

- **v1 定版**：160 道题（104 主测 + 56 候补），2 臂配对、同一题序、同一随机污染；
  另有 8 道 C-Eval 题按上游许可**只披露题号、不随包发布**（§10 有抓回命令）。
- **净效果**：两臂之间只差工具面，其余（模型、提示词、超时、判分、并发）全部一致 → 差值就是「只换工具面」的净效果。
- **真 token**：由本机记账代理逐次抓取，按 token 重算费用，不靠面板里的美元数。
- **全客观判分**：选择题、整数题、文件核对、隐藏单元测试、多步检查项，没有大模型当裁判。

版本：`1.0.0`（见 `VERSION`）。变更见 [CHANGELOG.md](CHANGELOG.md)。

---

## 1. 测什么

| 臂 | 对齐的 preset | 相对基线多/少了什么 |
|---|---|---|
| `std` | 出厂 `standard` | 基线：原生执行器 + 官方 read/glob/grep/pwsh/write/edit |
| `ptc` | 出厂 `ptc`（原生 PTC） | 只换工具面：模型只看到 `run_code`，原生文件工具与 shell 工具都不挂了；无规则、压缩/裁剪参数不动、停用 workflow 两行 |

想加别的臂（例如某个精简预设），照 `config.example.json` 里 `arms` 的写法写清 `patches` 与 `expect`。
`bench.py` 在建会话前会做「合成配置金丝雀」——逐项核对这一臂真的是你想测的那个样子，而不是「覆盖层被静默忽略、两臂其实一模一样」。

## 2. 环境

- Python 3.9+，`pip install -r requirements.txt`
- 一个可用的 `dsh`（headless CLI）：`dsh --version` 能跑
- 判分用到的 `pytest`（Python 题）、`openpyxl`（表格题）；`matplotlib` 只有画评分卡才需要

```bash
cp config.example.json config.json      # 首次运行也会自动生成
./run.sh doctor                          # 环境自检：python 依赖、dsh 可执行、端口、磁盘
```

Windows 用 `run.bat` 代替 `./run.sh`（两者等价，都只是 `python bench/bench.py "$@"`）。

## 3. 跑起来

```bash
./run.sh setup      # 建评测专用 DSH_HOME/profile，启动记账代理
./run.sh smoke      # 2 道题的冒烟：确认整条链路能跑通
./run.sh stage1     # 正式：2 臂 × 104 题 × 5 遍（config.example.json 的 stages.stage1.rep_to）
./run.sh grade      # 判分（grading: after = 全部跑完再判，判分器崩溃也不会白跑）
./run.sh report     # 报告：正确率、token、费用、配对区间
```

分阶段跑、断点续跑都是同一条命令：`stage1` 只补缺的遍数，`retry` 清掉失败状态后重跑。

完整命令：`doctor · setup · smoke · pre · stage1 · stage2 · grade · report · interim · status · manifest · leakcheck · retry · forget · calibrate · gpqa`。

## 4. 配置速查

`config.json`（全部键都有中文注释，见 `config.example.json`）：

| 键 | 作用 |
|---|---|
| `model` / `llm_provider` / `upstream` | 被测模型、协议（`deepseek` 或 `wb`）、上游地址 |
| `dsh_version_pin` / `dsh_command` | 期望的 DSH 版本；`dsh_command` 非空时用这条绝对路径启动（桌面客户端用） |
| `permission_mode` | 建议 `danger-full-access`：headless 没有审批通道，`workspace-write` 遇到需确认的操作会一直卡住 |
| `isolation` | `env`（默认，断网变量 + 随机工作目录）或 `docker`（只挂载本题目录、只能连记账代理） |
| `work_root` / `state_root` / `dsh_home` | 工作目录、会话目录、评测专用 DSH_HOME（默认都在系统临时目录或用户目录下，与日常使用隔离） |
| `arms` | 每个臂：`patches`（覆盖层）、`proxy_port`（记账代理端口）、`expect`（金丝雀断言） |
| `stages` | 每阶段跑哪些臂、哪几遍、每臂几个并行槽 |
| `max_parallel` / `slot_shards` | 全局并发上限；同一臂的槽是否分片排队（防止占满文件句柄） |
| `pricing_cny_per_mtok` | 计价口径（元/百万 token），一律按高峰价从 token 重算 |
| `provider_retry` / `adaptive_slots` | 供应商 503/429 时的重试、熔断探测、错误率高时自动降并发 |

## 5. 目录结构

```
dsh-bench/
├─ README.md              本文
├─ LICENSE                MIT（本仓库代码 + 自建题）；第三方题面见 NOTICE / THIRD-PARTY-DATA.md
├─ VERSION                1.0.0
├─ config.example.json    配置模板（首次运行复制成 config.json）
├─ run.sh / run.bat       入口，等价于 python bench/bench.py <命令>
├─ bench/                 Python 脚本（见 docs/技术.md）
│  ├─ bench.py            主编排：doctor/setup/stage1/grade/report…
│  ├─ run_bench.py        单次运行：建会话、发题、抓用量、隔离检查
│  ├─ usage_proxy.py      记账代理：逐次记录 token 与耗时
│  ├─ grade.py            判分器（选择/整数/文件/单元测试/检查项）
│  ├─ answers.py          答案桶打包/解包（_answers.bin）
│  └─ analyze.py          配对统计：bootstrap 区间、McNemar
├─ overlays/              DSH 覆盖层：common + 每臂一份
├─ prompts/               交给被测 AI 的提示词模板（各臂完全一致）
├─ manifest/              题单（csv / jsonl / 工作区文件清单）
├─ tasks/                 每题一个目录：task.json + workspace/（答案在 _answers.bin）
├─ docker/                严格隔离模式的镜像
└─ docs/                  设计、技术、流程与校准记录
```

## 6. 判分与统计

- **判分全部客观**：选择题比字母、整数题比数值、文件题按脚本核对单元格/字段/目录结构、编程题跑隐藏测试用例、多步任务按检查项逐条算。
- **配对设计**：同一道题、同一随机种子（`seed`）、同一提示词模板；各臂在同一随机题序上错开起点。
- **区间**：题级配对 bootstrap（默认 10000 次重采样）给正确率差与费用比的 95% 置信区间；配对正确率用 McNemar 精确检验。
- **非劣界**：默认 `margin: 5`，即「正确率差的下限 > −5 个百分点」才算不掉分。
- **费用**：代理逐次记录 `cache_hit / cache_miss / output` 三类 token，按 `pricing_cny_per_mtok` 重算；半价时段不影响结论（一律按高峰价折算）。

### 答案放在哪里

答案、隐藏测试与金标准表格打包在 `tasks/_answers.bin`（异或混淆的 zip），判分时才临时解出**当前这一题**，判完立即删除 —— 目的是防「被测 agent 顺手 grep 到答案」，**不是加密**。

要审阅答案（例如做 code review 或复现判分）：

```bash
python bench/answers.py unpack tasks     # 展开成 tasks/<题>/hidden/ 明文（会删掉 _answers.bin）
# …看完再收回去，否则答案会跟着提交出去：
python bench/answers.py pack tasks
```

**为什么答案跟着仓库发**：和 SWE-bench 把 `test_patch`／金标准一起公开是同一套逻辑——被测 agent 看到的是
**工作目录**，看不到这份包；只要用 `isolation: docker` 跑，答案与题号在容器里物理不可达。公开答案换来的是
任何人能复核判分、能离线复现「这道题为什么算错」，这比藏起来更有价值。读这份包需要一层异或，那只是防止
被误当成任务数据顺手加载，**不是安全边界**，别当初级加密用它。

两条环境变量供需要时把答案挪出代码树：

```bash
export DSHB_ANSWERS=/secure/path/_answers.bin   # 指向仓库外的答案包副本
export DSHB_KEY='换成你自己的键'                 # 换混淆键（换完必须重打包：python bench/answers.py pack tasks）
```

键不对时 `answers.py` 会直接报「答案包解不开」，而不是静默判成「没有隐藏测试」。

## 7. 隔离与公平性

每次运行：随机 ID 的工作目录（`<tmp>/w/<ID>/work`，每题独占一层父目录，跑完的目录不会和下一次互为兄弟）、
全新会话、断网环境变量；题面里不出现题号、臂名和自己所属的分组。

**能保证什么、不能保证什么**：`isolation: env` 是软隔离——断网变量 + 随机 ID + 启动前泄漏自检（工作目录里出现答案文件、
题号、判分脚本关键词就跳过这次运行），它挡的是"顺手看到"，**挡不住**主动读盘：同一台机器上 `bench/`、`tasks/`、`~/.dsh`
对 agent 都可读（`permission_mode: danger-full-access` 时更是如此）。要真隔离就把 `isolation` 改成 `docker`
（容器里只挂载工作目录，答案与题号物理不可达）。报告里"正确率"的可信度与这个前缀一致：env 模式适合自查，docker 模式才适合对外结论。

工作目录默认在判分后移到 `runs/_logs/<ID>/work` 以便复查。

## 8. 常见问题

| 现象 | 处理 |
|---|---|
| 起动即 `ValidationError` | 覆盖层里的行 id 与 DSH 版本不匹配；用 `doctor` 打印当前 base 组合包的行 id |
| 一直卡住不结束 | `permission_mode` 不是 `danger-full-access`，headless 下没有审批通道 |
| 某臂所有题都失败 | 先看 `runs/_logs` 里的启动日志；多数是 profile 没建好或代理端口冲突 |
| 供应商 503/429 一片 | 保留默认 `provider_retry` / `adaptive_slots`；或把 `max_parallel` 降到 2~3 后 `retry` 续跑 |
| 路径里带 bench/ops/presets 等词时行为异常 | 某些 DSH 构建会对路径关键词做泄漏拦截，把整包放到中性路径再跑（例如 `D:\E1\pkg`） |
| 想省时间 | `stages.*.slots_per_arm` 提到 3、`pre_repeats` 降到 1；正式结论建议仍跑满 5 遍以上 |

## 9. 规模与耗时

| 项 | 参考值（单机、32 核、本机记账代理） |
|---|---|
| 题量 | 160 道：104 主测（编程 38 / 办公 25 / 知识 8 / 长任务注意力 33）+ 56 候补；另 8 道 C-Eval 只披露不发布（§10） |
| 一遍全跑 | 2 臂 × 104 题，2 槽/臂下约 1–2.5 小时 |
| 5 遍 | 约 7–12 小时（这是默认；r=3 时的检验把握只有 0.38，见 §9） |
| 单题 token | 1 万–40 万；长任务题可达 60 万 |

## 10. 数据来源与许可

题面来自 LiveCodeBench、Aider Polyglot、SpreadsheetBench Verified、MMLU-Pro、LongBench v2，以及自建的办公 / 长任务 / 仓库检索题。
逐题的 `source` / `source_id` / `license` 写在 `manifest/manifest.csv`；署名与再分发条件见 [NOTICE](NOTICE) 与 [THIRD-PARTY-DATA.md](THIRD-PARTY-DATA.md)。

### 受限题目：只披露题号，不发布题面

C-Eval Hard 子集的 8 道题是 **CC BY-NC-SA 4.0**（非商用 + 相同方式共享），题面不随仓库发布。
仓库里只留 `manifest/upstream.json`——数据集、修订号、逐题 `source_id` 与内容哈希；要跑就抓回本地：

```bash
python bench/fetch_data.py --provider ceval   # 抓取 + 重建，并校验哈希（确认还是同一道题）
python bench/answers.py pack tasks            # 答案在本地生成，不来自仓库
python bench/check_tasks.py                   # 抓回后会提示「已本地抓回，别提交」
```

抓回来的题按题号前缀写进了 `.gitignore`，不会被误提交；抓取与使用都发生在你自己的机器上、没有再分发，
署名 / 非商用 / 相同方式共享的义务就落在你自己的使用范围里。所以「160 道」是本包直接可跑的规模，
「168 道」是加上这 8 道之后的完整题单。GPQA Diamond 同理（门控数据集，`./run.sh gpqa` 现场生成）。
逐题的 `source` / `source_id` / `license` 写在 `manifest/manifest.csv`；署名与再分发条件见 [NOTICE](NOTICE) 与 [THIRD-PARTY-DATA.md](THIRD-PARTY-DATA.md)。

**注意**：LiveCodeBench 的 33 道题面版权归 LeetCode / AtCoder，**仅限评测研究**——这 33 道仍在包里，引用分数时请按研究用途对待。商业用途请先自行核对。

## 11. 贡献

欢迎提 issue / PR。改动题目或判分器后，请先跑：

```bash
python bench/selftest.py tasks    # 题目与判分器自检
./run.sh leakcheck                # 泄漏自检
```

再出结论。新增题目请同时更新 `manifest/manifest.csv`、`manifest/manifest.jsonl` 与 `manifest/workspace_files.json`。
