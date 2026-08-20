# 交接：金融知识层与价格审计

- 交接日期：2026-08-20
- 基线提交：`10208ce`（版本 2026.08.19.1）
- 对应优先级：3（金融知识层）与 4（价格审计）
- 本次交付：两个模块的**可运行骨架 + 护栏测试**，内容待组员补齐
- 回归状态：742 通过（此前 729，新增 13），零回归

---

## 为什么这样设计（先读这段）

`FR-SHORT-004` 禁止系统输出或衍生 `LONG`/`SHORT`、价格方向、目标价、预期收益/跌幅、
时机、期限、仓位、杠杆、止盈止损。

**知识层最自然的写法会直接违反这条**：「这类事件通常意味着什么、历史平均跌幅多少」。
一旦这样写，任何读过 `PRODUCT_CHARTER` 的人都会发现系统在违反自己的章程——
损失的不是模块分，是整份治理叙事的可信度。

因此本模块的定位是**证据阅读层**，不是投资含义层：

| 不做 | 做 |
| --- | --- |
| 这事对价格意味着什么 | 这事**由谁说了才算数** |
| 历史平均涨跌幅 | 原文里**必须出现什么措辞** |
| 风险等级评分 | 什么东西**长得像但不是** |
| 该不该关注 | 什么情况**必须判证据不足** |

这个定位正好命中北极星 `FR-PROD-002`：缩短「发现 → 打开原文 → 识别冲突 → 决定」。

---

## 模块 3 · 金融知识层

### 已交付文件

| 文件 | 内容 | 状态 |
| --- | --- | --- |
| `config/event_playbook_v1.json` | 12 张卡（6 族 × 2 类） | **骨架已填，内容待复核** |
| `app/models/event_playbook.py` | 加载器 + 结构校验 | 完成 |
| `tests/test_event_playbook.py` | 6 项护栏测试 | 完成 |

### 卡片的六个必填字段

| 字段 | 内容 | 为什么必须有 |
| --- | --- | --- |
| `authoritative_sources` | 这类事件的 P0/P1 权威来源 | 防止用新闻稿当事实 |
| `required_language` | 原文出现什么措辞才算成立 | 区分「已发生」与「可能发生」 |
| `common_impostors` / `impostors` | 长得像但不是的情形 | 非目标控制 |
| `time_anchor` | 以哪个时间为准 | **直接决定价格审计的窗口对不对** |
| `corroboration_min` | 几个独立来源才算互证 | 可被测试断言 |
| `insufficient_when` | 什么情况必须判证据不足 | 把「诚实的不确定性」写成规则 |

### 关键设计：卡片绑定到真实闸门

卡片**不是独立的散文**。每张卡通过 `gate_refs` 声明它描述的是哪条实际判定逻辑，
命名空间有三个，加载时校验、测试时断言：

```
risk_scope_gate:<cue>                              # 16 个真实线索，见 app/models/risk_scope_gate.py
light_verification:auto_formal_event_types:<type>  # 4 个自动确认白名单类型
risk_label_contract_v3:<section>                   # 标签合同的真实小节
```

闸门被改名或删除 → 测试直接红。**文档与行为不一致这个失分点，从设计上就被堵住了。**

### 护栏测试（已通过故障注入验证）

我实际注入了三种违规，确认护栏会拦：

| 注入 | 结果 |
| --- | --- |
| 往卡里加「平均跌幅显著，建议卖出」 | 拦下，报 `forbidden investment language: 平均跌幅` |
| 引用不存在的闸门 `risk_scope_gate:nonexistent_cue` | 拦下，报 `unknown risk-scope cue` |
| 编造来源 `bloomberg_terminal` | 拦下，报 `unknown authoritative source` |

禁用词表位于 `tests/test_event_playbook.py::FORBIDDEN_INVESTMENT_TERMS`。
**注意它刻意不含「下行风险」和「做空研究」**——这两个词在所有者细则里是合法用语。

### 组员接手要做什么

1. **逐卡复核内容准确性。** 我按仓库现有事件族和来源写了初稿，
   但金融事实的准确性需要有金融背景的人过一遍，特别是 `required_language` 里的措辞。
2. **把 `status` 从 `DRAFT_PENDING_REVIEW` 改成正式状态**（复核完成后）。
3. **接入 UI。** `cards_for_family(event_family)` 直接返回可渲染的字典列表，
   建议接入点：`app/web/pages/1_Event_Intelligence.py`（事件详情页按 family 显示）
   与 `app/web/pages/5_Method_and_Boundaries.py`（全量索引）。
   *UI 文件属于优先级 2 的负责范围，接入前先与其协调，避免冲突。*

### 审 PR 时看三样

有没有 `gate_refs`、`authoritative_sources` 是不是账本里真实的 P0/P1、
`impostors` 能不能对应到细则或真实事件。**缺一样直接打回**，比逐句读内容快得多。

> 这个模块最容易被写成一堆通用金融科普——尤其是如果拿 AI 生成卡片内容。
> 通用科普的特征很好认：措辞泛泛、没有具体 `source_id`、`gate_refs` 对不上。

---

## 模块 4 · 价格审计

### 已交付文件

| 文件 | 内容 | 状态 |
| --- | --- | --- |
| `scripts/audit_price_windows.py` | 五维审计 + JSON/Markdown 报告 | 完成，可运行 |
| `tests/test_price_window_audit.py` | 7 项契约测试 | 完成 |

### 运行方式

```bash
python scripts/audit_price_windows.py \
  --db data/finance_radar.sqlite3 \
  --json-out reports/price_window_audit.json \
  --markdown-out reports/price_window_audit.md
# 退出码 0 = PASS，1 = ATTENTION（可直接用于 CI）
```

### 「先做时间合同，再接行情」是对的

前两个维度**完全不依赖行情数据**。锚点错了接再多行情源都是错的；
锚点对了，只接一个免费源也站得住。先做时间，是把风险最大的部分前置。

### 本次审阅发现的核心缺陷

`scripts/observe_live_event_markets.py` 当前的窗口锚点是：

```sql
baseline_at = MIN(s.captured_at) WHERE observation_window='initial'
```

也就是说，**T+0 是「我们第一次成功抓到报价的时刻」**——
既不是事件发生时间，也不是来源披露时间。

它比预想的更成问题：这个锚点**随采集延迟漂移**。
如果系统在事件披露 3 小时后才注意到，那么标着 `t_plus_5m` 的窗口
实际是事件后 3 小时 05 分。数据没错，**标签在说谎**。

审计现在把这件事显式报出来（`observed_anchor_is_degraded: true`），
并逐族对比知识层声明的锚点。这是模块的第一件事，也是最有价值的一件事。

### 五个审计维度

| # | 维度 | 依赖行情？ | 回答什么 |
| --- | --- | --- | --- |
| 1 | 锚点正确性 | 否 | 实际锚点是否等于知识层声明的锚点 |
| 2 | 窗口兑现率 | 否 | 应捕获多少、实际多少、错过多少 |
| 3 | 捕获延迟 | 是 | p50/p95，以及超出宽限期的数量 |
| 4 | 无回填证明 | 是 | 错过的窗口之后是否被偷偷补写 |
| 5 | 泄漏隔离 | 是 | 事后行情是否进入模型特征或发现排序 |

**维度 2 的分母是诚实的**：还没完成的作业仍留在分母里，不做美化。

**维度 5 是可证明的**：`event_market_metrics` 上有三条 SQLite `CHECK` 约束
（`metric_scope='post_event_audit_only'`、`allowed_for_discovery_rank=0`、
`allowed_as_model_feature=0`），事后行情在**数据库层面**就不可能被标成模型特征。
测试 `test_post_event_metrics_cannot_be_stored_as_a_model_feature` 实际触发了
`sqlite3.IntegrityError` 来证明这一点。答辩时这句话很值钱。

### 组员接手要做什么

1. **给 `market_jobs` 增加锚点字段。**建议 `anchor_kind` + `anchor_at` +
   `anchor_lag_seconds`（锚点相对来源披露时间的偏移）。
   加完后审计的维度 1 可以从「报告不一致」升级为「报告真实偏移量」。
   迁移写法参考 `app/storage/operations.py` 里 `ALTER TABLE ... ADD COLUMN` 的模式。
2. **处理交易时段与停牌。**这是最容易静默出错的地方：
   15:55 的事件 T+30m 已跨收盘，停牌股 T+1d 没有有效报价。
   这些必须产出 `NOT_APPLICABLE` 并说明原因，
   **绝不能填 0、绝不能沿用前值、绝不能顺延到下一交易时段还叫 T+30m**。
3. **把兑现率放到公开页。**「我们兑现了 X% 的观察窗口」比「我们有行情功能」可信得多。
4. **接入 Operator 界面**看延迟分布与失败原因（同样先与对应负责人协调）。

---

## 两个模块的接缝

`time_anchor` 是唯一接缝：**知识层定义，价格审计消费**。

```python
from app.models.event_playbook import time_anchor_for_family
time_anchor_for_family("delisting_or_suspension")   # -> "filing_effective"
```

枚举值已定死在 `app/models/event_playbook.TIME_ANCHORS`：

| 值 | 含义 |
| --- | --- |
| `event_occurred` | 事件本身生效的时刻 |
| `source_published` | 权威来源披露的时刻 |
| `filing_effective` | 备案内记载的生效日 |
| `first_capture` | 我方首次抓到报价——**标记为 DEGRADED，不得被任何卡片采用** |

`first_capture` 之所以留在枚举里，是为了让审计能如实描述观察器的当前行为，
而不是假装窗口从事件时刻开始。测试
`test_time_anchors_are_declared_and_never_silently_degraded` 禁止任何卡片采用它。

**这件事要先定死再动手**，否则两个模块各写各的，合并时都要返工。

---

## 验收标准

### 模块 3 完成的定义

- [ ] 12 张卡内容经金融背景成员复核
- [ ] `status` 由 `DRAFT_PENDING_REVIEW` 转正式
- [ ] 禁用词测试在 CI 中运行（当前已随 `pytest` 全量执行）
- [ ] 事件详情页能按 family 显示对应卡
- [ ] 方法与边界页有全量索引

### 模块 4 完成的定义

- [ ] `anchor_kind` / `anchor_at` 字段落库并回填
- [ ] 审计维度 1 报告真实偏移量而非仅报不一致
- [ ] 交易时段/停牌产出 `NOT_APPLICABLE` 而非 0
- [ ] 兑现率显示在公开页
- [ ] 审计脚本纳入 CI 或定期任务

---

## 加分点在哪

真正的加分不在功能多，在于能讲出两句别人讲不出的话：

> 「我们的知识层**不告诉你该怎么操作**，只告诉你怎么读懂证据——
> 因为章程禁止前者，而这条禁令是**用测试强制的**。」

> 「我们的行情观察**错过了就承认错过**，不用事后拿到的报价回填——
> 这一点**有测试证明，数据库约束也不允许**。」

**加分来自克制，不来自堆料。**
