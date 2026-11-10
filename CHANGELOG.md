# 变更记录

## 1.1.0 — 受限题目改成「只披露题号，不发布题面」

- **C-Eval 的 8 道题面移出仓库**：C-Eval 是 CC BY-NC-SA 4.0（非商用 + 相同方式共享），继续随包发布等于把
  「不许商用」这条义务转嫁给每个下游使用者。现在仓库里只留 `manifest/upstream.json`（数据集、修订号、
  逐题 `source_id` 与题面内容哈希），要用就 `python bench/fetch_data.py --provider ceval` 在本地抓回。
  本包直接可跑的规模因此是 **160 道（104 主测 + 56 候补）**，完整题单仍是 168 道。
- 新增 `bench/fetch_data.py`：走 HuggingFace datasets-server 的行接口（只用标准库，不需要 pandas），
  抓回后**逐题校验 task.json 哈希**——对不上就报「上游数据可能变了」，不会静默换成另一道题。
- `answers.py pack` 现在会剔除「磁盘上已经没有的题」的答案：以前删了题、答案还留在 `_answers.bin` 里。
- `check_tasks.py` 增加 upstream 核对：受限题若出现在 `tasks/` 只提示「本地抓回，别提交」；
  `.gitignore` 按题号前缀排除，抓回来的题不会被误提交。

## 1.0.2 — 判分器与编排层的两轮审计修复

- polyglot_python / polyglot_javascript 不再把「退出码 0」当成「测试全过」（os._exit(0)、影子 pytest.py、addopts --co、process.exit(0) 四种白过已封）。
- ssb_cells 增加「整表被清空」守卫：交一个空工作簿不再能靠「空 == 空」拿分。
- 测试文件与 lcb harness 写入前先拆软链，堵住「软链注入写到工作目录之外」。
- 判分临时目录权限收到 0700（同用户被测代码仍可读，见 docs/审计-v1.0.0.md §8 known issue）。
- frac 夹到 [0,1]；read() 拒绝设备文件并限 4 MB；lcb 失败结果补 got/expected。
- Ctrl+C / SIGTERM 不再把正在跑的题写成「答错」并被续跑跳过；退出时清理还活着的 wk-* 容器。
- calibrate 在没有 results 时直接报错，不再静默空转退出 0；--only 拼错题号报错；forget 改原子写。

## 1.0.0（2026-11-10）· 首个公开定版

从内部 `v1.9.1` 起，经过四个子审计（判分统计 / 任务数据 / 运行隔离 / 安全许可文档）与本机实测，
公开定版为 **DSH-Bench v1**。逐条见 `docs/审计-v1.0.0.md`。

### 会让结论直接失效的修复

- **工作目录互为兄弟**：原来所有 run 都是 `<tmp>/w/<rid>`，env 模式（danger-full-access）下一句 `ls ..`
  就能看到别的臂/别的遍写好的 `ANSWER.txt` / `output.xlsx`。改成 `<tmp>/w/<rid>/work`，每题独占一层父目录。
- **预设保真门禁是空的**：`preset_fidelity` 把「预设要求、但合成配置里没有这一行」一律归入
  `absent-in-headless`（非失败），空 dump 也返回 0 —— 工具面等价性没有任何证据。现在只有 `WEB_ONLY`
  里的行允许缺席，其余判 mismatch；dump 少于 5 行直接拒绝判定（返回 2）。
- **判分器异常被当成答错**：`grade()` 把异常转成 `pass=False`，而 `selftest` 的 empty 模式期望值正是 False，
  于是缺 openpyxl 时 20 道表格题静默清零也发现不了。`selftest` 现在见到 `grader_error` 直接判失败并打印。
- **记账漏记被当成 0 元**：`analyze` 与 `scorecard` 现在标记 `usage_missing`，费用比只在「两臂都记到账」的
  题上计算，不再把漏记按 0 元入账（那会把费用比拉向 1）。
- **钩子/熔断**：熔断器并发许可在供应商重试时泄漏（503 风暴后所有槽永久卡死）。

### 装得上、跑得起来

- `config.example.json` 不再把 `dsh_command` 钉死成作者本机的 Windows 桌面路径（`null` = 自动探测）。
- `bench/integrity.py` 重写：原来写死 `D:/EVAL/p191` 且会**删掉**解析不了的行；现在只做只读核对，默认 dry-run。
- docker 模式容器里补上 `WB_PROXY_API_KEY`（原来「严格隔离」等于跑不成）；走 wb 时显式剔除 `DEEPSEEK_API_KEY`。
- 官方串下 `config.model` 不再被静默忽略；两个分片槽不再写同一个结果文件；冒烟会真的判服务端模型名。
- `doctor` 不再因为「包目录名含 test/bench」硬失败（发布目录就叫 `dsh-bench`）。
- README 重写（原先代码围栏坏掉，「快速开始」整段渲染成一行）；新增 `LICENSE` / `NOTICE` /
  `THIRD-PARTY-DATA.md`（C-Eval 8 题为 CC BY-NC-SA 非商用、LiveCodeBench 33 题仅限研究）。
- 新增 `bench/check_tasks.py`（manifest.csv / manifest.jsonl / tasks/ / `_answers.bin` / 判分器名单五方一致）、
  `bench/strip_xlsx_meta.py`（去掉 xlsx 里的真人姓名与企业名）、`.github/workflows/ci.yml`、`VERSION`。

### 已知未修

见 `docs/审计-v1.0.0.md` 第 2、5 节：答卷措辞敏感（35% 的题）、答案键与密文同包、费用比两套口径、
超时按答错计、缺题被交集静默剔除、`leak_allow` 通配放行、测试可被 `conftest.py` 绕过、
`grade_runs` 不去重、设计功效 0.38（`margin=5` 配 `r=3`）、长任务簇不独立、办公维度题干与交付错位。

> 一句话：**这份包能跑、判得分，但在上面这些未修条目解决前，正确率差与费用比都还不该当结论引用。**
