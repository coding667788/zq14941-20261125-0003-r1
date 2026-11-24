# counterweir

复核隔离设备导出的**封存回执**：给定每台设备每个世代（generation）的合成日志、
可信锚点、批准/撤销区间，判断每条待核回执在所有满足约束的实际序号分配下是否获批。
所有数据均为合成，仓库从零构建（counterweir），仅依赖 Python 3.12 标准库。

## 1. 形式化模型

每个世代由输入声明模数 `M`。事件的**实际序号** `x` 是非负整数，日志只保留余数
`r = x mod M`，写作

```
x_i = r_i + M * k_i,        k_i >= 0
```

`k_i` 称为圈数（lap）。所有求解都在 `k` 空间进行，**绝不逐序号或逐圈展开**，
因此 `x` 取到 `10^12` 时代价不变。

### 约束来源

| 约束 | 形式 | 证据 ID |
|---|---|---|
| 观察余数 / 序号上界 | `0 <= k_i <= floor((max_seq-r_i)/M)` | 观察 ID |
| 可信锚点 | `k_i` 被钉为单点 | 锚点 ID（不可重新核实） |
| 同源采集次序 | `gap_min <= x_cur - x_prev <= gap_max`，化为 `lo <= k_cur-k_prev <= hi` | 当前观察 ID |
| 同代区分性 | 同余数的**不同事件** `x_i != x_j`（即 `k_i != k_j`） | 两事件全部证据 |

规则要点（与任务说明一一对应）：

- 同一稳定事件 ID 在不同观察源出现时是**同一个变量**；余数冲突直接判输入错误。
- 余数相同的不同事件**不能合并**，由区分性约束强制实际序号互异。
- 一个观察源的采集次序不可重排；相邻已见事件之间允许漏记录或跨多圈，
  体现在 gap 的上下界可以很宽，圈数差由 ceil/floor 除法精确换算。
- 首尾可信锚点把变量钉为单点；同名设备按该世代 `device_aliases` 生效映射认定，
  跨世代互不影响（每个世代独立建模）。

### 批准语义

每台设备的批准与撤销都是**实际序号闭区间**并绑定证物摘要 `digest`。回执引用
`(世代, 事件, 摘要)`：当事件序号落入某个同摘要批准区间、且不落入任何撤销区间时
才算获批。区间在 `k` 空间被换算为圈数区间并合并，撤销点从批准区间中扣除。

## 2. 三个 CASE

### audit

对每条回执输出四态之一：

- `PROVABLE_APPROVED`：每个合法分配都获批；
- `PROVABLE_NOT_APPROVED`：每个合法分配都未获批；
- `INSUFFICIENT_EVIDENCE`：两种分配都存在，附**一条允许、一条拒绝的完整可复演分配**；
- `CONTRADICTORY_EVIDENCE`：组件无合法分配，附**最小可重放证据集合**
  （先按条数、再按升序 ID 决胜）。

最早/最晚可能序号各自在某个满足全部源约束的完整分配中取得（二分可行性 + 见证分配）。

### repair

从至多 16 条 `recheckable_observations` 中选暂停采信集合，要求：其余约束一致；
所有 `mandatory` 回执仍可证实获批；所有 `sealed` 回执在**任何**合法分配下都未获批。
目标依次最小化：重新核实成本（`recheck_costs`，默认 1）→ 暂停条数 → 升序 ID 列表。
每个候选掩码都**从头重新求解**，不沿用基线结论。

### verify

对声称的 audit/repair 结果独立重建模型并重新求解，逐条比对。明确拒绝三类错误：

1. **只符合一边日志的见证**：`INSUFFICIENT_EVIDENCE` 缺允许或拒绝分配，或分配不满足约束；
2. **同成本但排序更后的 repair 方案**：枚举所有更优目标元组，存在合法者即拒绝；
3. **把同一余数误当同一事件**：校验见证分配的余数、锚点、gap 与区分性，
   发现两个不同事件被赋予同一实际序号即拒绝。

`verify` 通过退出码 0、拒绝退出码 1，便于脚本化。

## 3. 求解器与规模化

`counterweir/solver.py` 在 `k` 空间维护整数区间域 + 排除点集合：

- **传播**：差约束的 FIFO 松弛队列（Bellman–Ford 风格），线性收敛；
  单点域触发区分性剪枝。
- **搜索**：只对带排除点的变量，按排除点把区间切成自由子区间递归，
  分支数取决于区分性伙伴数而非数值域大小。
- **组件化**：先按采集次序边分件并传播；传播后即单点的大型对齐链标记为
  `deterministic`，不实例化 O(n²) 区分性约束；只有仍歧义的小组件
  （未确定事件 ≤ 16）才补全同余数 neq 并精确求解。
- 常规已对齐日志可达 20 万条，实测普通 CPU 数秒；歧义组件 ≤ 16 个未确定事件。
- `brute_force_solutions` 是仅供 unittest 使用的小圈数域**逐分配穷举 oracle**，
  生产路径不调用。

## 4. CLI（UTF-8 JSON）

建议用隔离模式运行，不可信 case 文件只作为路径参数，不从其目录导入任何代码：

```bash
python -I cw.py audit   path/to/case.json [-o result.json]
python -I cw.py repair  path/to/case.json [-o result.json]
python -I cw.py verify  path/to/case.json claimed_result.json [-o verdict.json]
python -I run_tests.py
```

## 5. 手算夹具

### 夹具 1 `fixtures/fixture1_two_lap_two_source.json`（M=4）

- E1 余数 2，单看可取 `{2,6,10}`；S2 以可信锚点 E3=10 收尾，gap [4,8] 排除 x(E1)=10，
  **两源合并后恰好只剩 `{2,6}`**——精确排除一种单源分配。
- 批准区间 `[6,7]` 对余数 2 事件跨越两圈（圈数 0 的点 2 在圈 0，点 6 在圈 1）。
- E2 余数 3，合法 `{3,7,11}`。R1（E1, dgx-alpha）材料不足，R3/R4 摘要不匹配可证实未获批。

### 夹具 2 `fixtures/fixture2_repair_ambiguity.json`（M=4）

- 基线下 gap o2 [4,7] 只允许 `(E1,E2)=(2,9)`，E1=2 被强制，R-MAND 可证实获批。
- **暂停单条 o2** 后 E1 释放为 `{2,6}`，R-MAND 立刻变成材料不足——
  移除一条可疑观察使强制回执由确定变为不确定。
- R-SEALED（E2）始终在批准点 2 之外，保持可证实未获批。

另有：`fixture3_contradiction.json`（最小矛盾证据）、
`fixture4_repair_nontrivial.json`（唯一最小 repair `{o4}`，含高成本诱饵 o6）、
`fixture5_tie_break.json`（两个独立矛盾组件，成本/条数/字典序决胜）。

### 合成夹具生成器

```bash
python -I tools/generate_synthetic.py aligned   -n 200000        -o big.json
python -I tools/generate_synthetic.py aligned   -n 200000 --big  -o big1e12.json
python -I tools/generate_synthetic.py ambiguous --undetermined 12 -o amb.json
python -I tools/generate_synthetic.py repair                      -o rep.json
```

## 6. 实测性能（Windows 普通 CPU，Python 3.12，`-I`）

| 规模 | load | build | audit | 合计 |
|---|---|---|---|---|
| 10 万对齐事件 | 0.64s | 1.8s | 1.3s | ~3.1s |
| 20 万对齐事件 | 1.3s | 3.9s | 2.6s | ~6.5s |
| 20 万、序号 ~10^12 | 1.3s | 3.9s | 2.6s | ~7.9s |

歧义组件（≤16 未确定事件）走精确 DFS，穷举 oracle 在测试中对拍全部判决、
极值、可行解集与区分性。

## 7. 输入 JSON 概览

```json
{
  "case_id": "...",
  "generations": [{
    "generation_id": "G1", "device": "name", "modulus": 4,
    "max_sequence": 11,
    "device_aliases": {"alias": "canonical"},
    "anchors": [{"anchor_id": "a1", "event_id": "E3", "sequence": 10}],
    "observations": [
      {"observation_id": "o1", "source_id": "S1", "event_id": "E1", "residue": 2},
      {"observation_id": "o2", "source_id": "S1", "event_id": "E2",
       "residue": 3, "gap_min": 1, "gap_max": 5}
    ],
    "approvals": [{"lo": 6, "hi": 7, "digest": "dgx-alpha", "evidence_id": "app1"}],
    "revocations": []
  }],
  "receipts": [
    {"receipt_id": "R1", "generation_id": "G1", "event_id": "E1",
     "digest": "dgx-alpha", "mandatory": false, "sealed": false}
  ],
  "recheckable_observations": ["o2"],
  "recheck_costs": {"o2": 1}
}
```

同源第一条观察没有 gap；其后每条必须给 `gap_min/gap_max`（允许为负，
区分性由独立 neq 约束保证，不依赖 gap 非负）。
