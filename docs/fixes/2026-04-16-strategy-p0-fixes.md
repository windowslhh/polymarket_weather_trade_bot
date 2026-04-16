# 2026-04-16 Strategy P0/P1 修复计划

- **分支**: `claude/admiring-murdock`
- **来源**: 2026-04-16 VPS 诊断（32 小时窗口，79 仓位，4 settlements，0 LOCKED-WIN 成交，A↔B 19/19 完全重复）
- **范围**: P0（A/B 去重、锁定胜死区、decision_log REJECT 观测）+ P1（相对 EV 衰减 TRIM、薄流动性城市上限）
- **不在本次范围**: P2 集成预报 429 重试 / METAR warning 清理（留作后续）
- **验证**: 仅本地 pytest，不部署 VPS，不合并 main

诊断数据的关键引用：
- A↔B: `19/19` 在 `token_id/entry_price/size_usd/created_at` 精确一致 → A/B 是同一策略的两次写入。
- `buy_reason LIKE '%LOCKED WIN%'` 最近 32h = **0**；历史快照 `bot_backup_0413.db` 曾有 32 条，0.95 价帽收紧后归零。
- 亏损集中：Miami −$4.02、SF −$1.57；Seattle +$2.80；其余 ±$0.5。全部为 MTM，未实现。
- 已实现 P&L −$0.02（15 笔 TRIM 退出）；未实现 MTM −$3.55（64 仓）。
- 1 locked-win 触发需要 `price ≤ 0.95 + gap ≥ margin + days_ahead=0`；实际市场 true lock 价接近 1.000，被 0.95 挡住。

---

## 改动优先级一览

| # | 优先级 | 主题 | 主要文件 | 影响 |
|---|---|---|---|---|
| 1 | P0 | A/B 去重：B 的 `kelly_fraction` → 0.6 | `src/config.py` | 4 变体不再同价位同仓位，A/B 对比变有意义 |
| 2 | P0 | 锁定胜死区：`gap ≥ margin` 时跳过 `max_no_price` 价帽（甲案） | `src/strategy/evaluator.py` | 真正的 guaranteed win 即使价 0.97/0.98 也能入场 |
| 3 | P0 | `decision_log` REJECT 观测（采样写入） | `src/strategy/evaluator.py` + `src/strategy/rebalancer.py` | 下次诊断能直接在表里看 EV/距离/价帽被拒原因 |
| 4 | P1 | 相对 EV 衰减 TRIM（需新增 `entry_ev` 列 + 迁移） | `src/portfolio/store.py` + `src/portfolio/tracker.py` + `src/execution/executor.py` + `src/strategy/evaluator.py` + `src/strategy/rebalancer.py` | 高 EV 入场位不被轻微波动提前 TRIM，减少回旋磨损 |
| 5 | P1 | 薄流动性城市上限 | `src/config.py` + `src/strategy/rebalancer.py` | Miami/SF 这类高集中度亏损城市被硬上限，压制 MTM 尾险 |

---

## Fix 1 — A/B 去重（P0，config-only）

### 现象
- 诊断 32h 窗口：A 与 B 的 19 个开仓在 `token_id, entry_price, size_usd, created_at` 全等。
- 现有配置：A 与 B 的非锁定胜参数 (`max_no_price=0.70`、`min_no_ev=0.05`、`kelly_fraction=0.5`) **完全相同**；本该差异化的 `locked_win_kelly_fraction` 因锁定胜死区（见 Fix 2）从未触发 → B 实际上是 A 的影子拷贝。

### 根因
`src/config.py:90-101` B 的覆盖项只在 `locked_win_kelly_fraction` / `max_locked_win_per_slot_usd` / `max_positions_per_event` 上与 A 不同。只要锁定胜不生成信号，B 的所有成交就必然是 forecast-based NO，走 `kelly_fraction=0.5` 的同一尺寸计算，与 A 完全一致。

### 改动
**`src/config.py`** — 仅修改 B 字典：

```python
"B": {
    "max_no_price": 0.70,
    "kelly_fraction": 0.6,       # ← 新增：较 A 的 0.5 放大 20% 仓位
    "max_positions_per_event": 6,
    "calibration_confidence": 0.90,
    "min_no_ev": 0.05,
    "max_position_per_slot_usd": 5.0,
    "max_exposure_per_city_usd": 30.0,
    "locked_win_kelly_fraction": 1.0,
    "max_locked_win_per_slot_usd": 10.0,
},
```

**为什么选 `kelly_fraction=0.6` 而不是放宽 `max_no_price`**：

- 放宽 `max_no_price=0.80` 会改变 **选中的 slot 集合**，破坏 A/B 的历史可比性（B 开始吃 A 不吃的深价位置）。
- 调 `kelly_fraction=0.6` 保持选同一批 slot，只让每笔仓位大 20%。天然符合 “B = locked aggressor / 稍激进” 的定位。
- 此外，由于 `test_strategy_variants.py::test_same_entry_as_a` 只断言 `max_no_price / min_no_ev / calibration_confidence` 三项，本方案 **不需要改现有测试**；即 `test_a_b_same_no_signals`（断言 slot 标签集合相等）也保持通过。
- 新增一条 B≠A 的 sizing 断言在同文件内兜底。

### 验证
```
.venv/bin/python -m pytest tests/test_strategy_variants.py -q
```
预期：原有所有测试仍通过 + 新增 B 的 `kelly_fraction` 断言通过。

### 取舍标注
- **不删 B**：删除会污染 docker 镜像里已入库的 `positions.strategy='B'` 行，使前端 “Locked Aggressor” 列永久空数据，破坏纵向对比。
- **不改 `max_no_price`**：历史诊断以 A/B 同入场集为基线；此次仅拉开 sizing 差距，便于 2-4 周后对比 A vs B 的累计收益率。

---

## Fix 2 — 锁定胜死区（P0，甲案）

### 现象
- 过去 32h：0 次 LOCKED-WIN 入场。
- 市场对 “几乎确定” slot 会定价到 ~0.99-1.00，被 `src/strategy/evaluator.py:523` 的 `price > 0.95` 价帽挡住。
- 结果：设计意图中 “full-Kelly 保底胜” 的信号从未成交，B 的唯一差异化通道失效。

### 根因
`src/strategy/evaluator.py:520-528`（锁定胜逻辑）与通用 `max_no_price` 语义冲突：
- 通用价帽本意是避免 “高价 NO 的 asymmetric risk”，即在不确定的 0.80+ 价位买 NO 意味着输钱风险收益比差。
- 但对已观测到 `daily_max > upper + margin` 的 slot，**NO 是 bounded 确定性事件**（温度只升不降）。0.97 入场 → 3% gross / ~2.4% net（1.25% 概率加权 fee 在 0.97 时约 ≈ 0.073%），仍是正 EV。
- 硬编码的 0.95 把这种 guaranteed win 全部过滤掉。

### 改动（甲案：当 `gap ≥ margin` 时跳过 `max_no_price` 价帽）
**`src/strategy/evaluator.py`** —

1. `evaluate_locked_win_signals`（line 456-557）内在命中锁定胜后：
   - 移除硬编码 `if slot.price_no > 0.95: continue`。
   - 以更严的 `if slot.price_no >= 1.0: continue` 替代（避免 price=1.0 做无意义买单）。
   - 当命中条件为 "below-slot lock"（`rounded_max > upper_int` 且 `gap ≥ margin`）时，把 `win_prob` 从 `0.99` 提升到 `0.999`——因 daily_max 单调不降且 wu_round 离上界 ≥ margin 度，温度回落到区间内概率小于千分之一。
   - 保留既有 `if ev <= 0: continue` 作为最终门（任何亏损的锁定胜都不交易）。

2. `evaluate_no_signals` 不改动。通用 NO 入场路径仍受 `max_no_price` 约束（正确）。

### 验证
- 新测试 `tests/test_locked_win.py` 增加：
  - 价 0.97、`gap = margin` 的 below-slot lock → 生成 1 个 signal，EV > 0。
  - 价 0.97、`gap < margin` → 不生成（不是 locked，回落 NO 路径，被 `max_no_price` 挡）。
  - 价 0.999 → 基于 fee 计算 EV ≤ 0 → 不生成（`ev <= 0` 门兜住）。
  - above-slot lock（`daily_max_final=True` + `lower - rounded_max ≥ margin`）走 `win_prob=0.99` 不升（因下午温度再升可能性仍存在）。
- 全套：`.venv/bin/python -m pytest tests/ --ignore=tests/dry_run_offline.py --ignore=tests/run_backtest_offline.py -q`

### 取舍标注
- **为什么甲案（改 evaluator）而不是乙案（改每个变体把 `max_no_price` 抬到 0.99）**：
  - 乙案污染非锁定胜路径——高价 NO 的正常 EV 风险在 0.85-0.95 区间依然不佳，不该全局放。
  - 甲案只把 “已观测到保底” 的 slot 豁免价帽，边界清晰、回归面积小。
- **`win_prob` 升到 0.999 的风险**：wu_round 与真值误差 ≤ 0.5°F，margin ≥ 2 时理论下界仍 ≥ 1.5°F 真实差；历史 32 条老 LOCKED entries 均赢家（0 个翻盘）。沿用 0.99 会让 0.97+ 的 slot 的 EV 轻微为负被 `ev <= 0` 过滤掉。
- **不动通用路径上 `min_no_price`**：锁定胜条件下价 < 0.20 本就不可能（市场会定价到 0.90+），没必要破坏通用门。

---

## Fix 3 — decision_log REJECT 观测（P0）

### 现象
- `decision_log` 目前只记录 **生成了 signal** 的决策（含 SKIP：size=0 / cooldown / max_positions）。
- 被 `evaluate_no_signals` 内部 `continue` 掉的 slot（距离不足、价 > max_no_price、EV < threshold、价格偏离）**没有任何条目**。
- 结果：诊断时无法回答 “为什么这 32h 整个市场只有 15 个 NO signal 成交——是 slot 本来就少，还是 EV 门过严？”

### 根因
`src/strategy/evaluator.py:185-329` 内部多处 `continue`（line 188, 200, 212, 226, 256, 259, 263, 267, 300, 310）直接吃掉被拒 slot。rebalancer 只能看到最终 `signals` 列表，感知不到分母。

### 改动
**`src/strategy/evaluator.py`** — `evaluate_no_signals` 新增可选参数 `rejects: list | None`：
- 每次 `continue` 前，若 `rejects is not None`，append 一个轻量 dict:
  `{"slot_label": ..., "price_no": ..., "reason": "DIST_TOO_CLOSE" / "PRICE_TOO_HIGH" / "PRICE_TOO_LOW" / "EV_BELOW_GATE" / "DIVERGENCE" / "DAILY_MAX_IN_SLOT" / "DAILY_MAX_ABOVE_LOWER" / "DAILY_MAX_BELOW_UPPER"}, "distance_f": ..., "win_prob": ..., "expected_value": ...}`
- 不改返回签名；`rejects` 为 None 时零开销（原语义保持）。

**`src/strategy/rebalancer.py`** — 主循环（line 961）调用处：
- 传入新建的 `no_rejects: list = []`。
- 生成 signal 后、写 decision_log 前对 `no_rejects` 做采样（每 `strategy` × `event` 最多写 3 条 REJECT；可配置：`decision_log_reject_sample = 3`，不增加配置项，硬编码在 rebalancer 里即可）。
- 写入时：`action="REJECT"`, `signal_type="NO"`, `reason=reject_item["reason"]`，其它字段从 reject_item 复制，`size_usd=0`。

### 验证
- 新测试 `tests/test_reason_tracking.py`（已存在）增加：`evaluate_no_signals` 带 `rejects=[]` 调用后，被 price_cap 拒绝的 slot 产生 `PRICE_TOO_HIGH` 条目。
- 跑完 smoke_dry_run，肉眼检查 `data/bot.db` decision_log 表最近条目含 REJECT 行。

### 取舍标注
- **采样 3 条/event/strategy**：每城 20 slot × 4 策略 × 30 城 × 每 60 分钟 cycle ≈ 上限 14k REJECT/day；采样压到 ~1.4k/day，与现有 decision_log 写入量同数量级，SQLite 可接受。
- **为什么不记全部**：全量会把 decision_log 撑到 GB 级，前端 `get_decision_log(limit=50)` 查询变慢。
- **为什么不单独建表**：决策观测统一在一张表便于前端 filter；加新表需要模板改动，和 P0 范围无关。

---

## Fix 4 — 相对 EV 衰减 TRIM（P1）

### 现象
- 现有 `evaluate_trim_signals` 用**绝对阈值** `ev < -min_trim_ev`（默认 -0.02 / VPS yaml -0.005）。
- 高 EV 入场（比如 EV=+0.08）遇到小幅 forecast 波动，EV 回落到 -0.006 立刻触发 TRIM；但其实持有到 settlement 仍优于 round-trip spread。
- 诊断期 15/79 仓被 TRIM（19%），全部贡献 ≈ −$0.02 realized；频繁出入还造成 cooldown 锁死后续入场。

### 根因
绝对阈值把 “入场时 EV=+0.08 的强信号” 与 “入场时 EV=+0.02 的边际信号” 一视同仁。边际信号的 -0.005 翻转说明市场已反向，该 trim；强信号的 -0.005 只是噪音。

### 改动
**`src/portfolio/store.py`** — 新增列 + 迁移：
```python
# 在 _migrate_columns 的 migrations 列表里追加：
("positions", "entry_ev", "ALTER TABLE positions ADD COLUMN entry_ev REAL"),
("positions", "entry_win_prob", "ALTER TABLE positions ADD COLUMN entry_win_prob REAL"),
```
- `insert_position` 签名扩展：加 `entry_ev: float | None = None`, `entry_win_prob: float | None = None`，写入 INSERT。

**`src/portfolio/tracker.py`** — `record_buy` 透传新参数。

**`src/execution/executor.py`** — 写仓时从 `signal.expected_value / signal.estimated_win_prob` 取值传入。

**`src/strategy/evaluator.py`** — `evaluate_trim_signals` 接收新 dict 参数 `entry_ev_map: dict[token_id, float] | None`：
- 原 `if ev < -config.min_trim_ev` 条件修改为：
  - 若 `token_id` 在 `entry_ev_map` 且 `entry_ev > 0`：`if ev < entry_ev * (1 - config.trim_ev_decay_ratio)`（默认 `trim_ev_decay_ratio=0.75`，即当 EV 衰减掉 75% 及以上才 TRIM）。
  - 并且与绝对门 OR 组合：`or ev < -config.min_trim_ev_absolute`（避免正 EV 入场、`ev` 变成 -0.1 也不 trim 的情况）。
- `StrategyConfig` 新增字段：`trim_ev_decay_ratio: float = 0.75`, `min_trim_ev_absolute: float = 0.03`（替代原 `min_trim_ev` 的使用场景；保留 `min_trim_ev` 字段防止旧 yaml 报错但不再使用，加 deprecation comment）。

**`src/strategy/rebalancer.py`** — 调用 `evaluate_trim_signals` 前从 `existing_positions` 构造 `entry_ev_map`（`pos["entry_ev"]` 回填 None 时跳过）。

### 验证
- `tests/test_trim_signals.py` 新增：
  - 高 EV 入场 (entry_ev=0.08)、当前 ev=-0.005 → 不 TRIM（绝对门 ≥ -0.03，相对门未达 75% 衰减）。
  - 高 EV 入场 (entry_ev=0.08)、当前 ev=-0.04 → TRIM（绝对门过了）。
  - 高 EV 入场 (entry_ev=0.08)、当前 ev=0.018（衰减 77%）→ TRIM（相对门过了）。
  - `entry_ev_map` 为空 → 回退原语义（`ev < -min_trim_ev_absolute`）。
- 迁移测试：空库跑 `initialize()`，断言 `positions` 表有 `entry_ev / entry_win_prob` 列。

### 取舍标注
- **为什么存 entry_ev 而不是 entry_win_prob-only 现场反推**：entry_ev 已包含了进入时的价格、fee，重建会引入浮点漂移；直接存入数据开销极低（2 × REAL 列）。
- **保留 `min_trim_ev` 字段的代价**：字典 YAML 可能含该 key；删除会让 `StrategyConfig(**raw)` 抛 TypeError。只将它标为 deprecated 不再参与逻辑。
- **`trim_ev_decay_ratio=0.75` 的默认**：激进；若后续看到 TRIM 被拦太死再调到 0.60。

---

## Fix 5 — 薄流动性城市上限（P1）

### 现象
- MTM 亏损 $4.02 中 **Miami -$4.02（占比 113%）**；SF -$1.57；其它城市零和或正。
- Miami 成交 3 个开仓；spread 长期 0.10-0.15；LOBO 薄。
- 当前 `max_exposure_per_city_usd=30` 对这类薄流动性城市过松——市场一反向，MTM 放大。

### 根因
统一 per-city cap 没区分流动性层级。诊断数据里 Miami / SF / Tampa / Orlando 的 Gamma `volume` 中位数 ~$800-1500，远低于其他城市的 $3000+，但享受同一个 exposure 上限。

### 改动
**`src/config.py`** — 新增配置：
```python
# StrategyConfig 新字段：
thin_liquidity_cities: set[str] = field(default_factory=lambda: {
    "Miami", "San Francisco", "Tampa", "Orlando"
})
thin_liquidity_exposure_ratio: float = 0.5   # 这些城市 cap = 0.5 × normal cap
```

**`src/strategy/rebalancer.py`** — 在 size 前计算 effective city cap：
```python
effective_city_cap = strat_cfg.max_exposure_per_city_usd
if event.city in strat_cfg.thin_liquidity_cities:
    effective_city_cap *= strat_cfg.thin_liquidity_exposure_ratio
```
并传入 `compute_size` 或直接在 rebalancer 侧 `strat_city_exp + size > effective_city_cap` 时截断。

> 最小入侵实现：直接在 rebalancer 里构造一个临时 `StrategyConfig` 副本（`dataclasses.replace`）替换 `max_exposure_per_city_usd` 后传给 `compute_size`。

### 验证
- `tests/test_strategy_variants.py` 新增：
  - Miami 的 effective cap = A 的 `max_exposure_per_city_usd × 0.5` = 15。
  - 非薄流城市（NY）effective cap = 原值 30。
- 单元测：配置字段可被 yaml 覆盖（防止默认 set 被序列化问题）。

### 取舍标注
- **为什么不全面动 `min_market_volume`**：volume 是流量，不反映盘口厚度；有些高 volume 城市仍薄 spread。
- **4 城白名单的依据**：诊断期 spread 中位数最宽的 4 座城市。后续可用 `edge_history.ensemble_spread_f` + 市场 spread 数据做自动分层。
- **ratio=0.5 的依据**：MTM −$4.02 在 cap=30 下是 13.4%；减半到 15 ⇒ 最坏 MTM ~−$2，可接受。

---

## 测试策略

每个 commit 后执行：
```bash
.venv/bin/python -m pytest tests/ --ignore=tests/dry_run_offline.py --ignore=tests/run_backtest_offline.py -q
```

提交顺序（每条单独 commit，messages 明确引用本文档小节号）：
1. Fix 1 - config-only（最安全，先落）
2. Fix 2 - 锁定胜死区（evaluator 变化有限、已有测试覆盖）
3. Fix 3 - decision_log REJECT（evaluator + rebalancer，行为新增）
4. Fix 4 - 相对 EV TRIM（含 DB migration，改动面最广）
5. Fix 5 - 薄流动性 cap（rebalancer 边缘改动）

任何一步 pytest 红掉 → 停住，定位根因，不继续后续 commit。

## 文档同步

完成 Fix 1-5 后：
- `CLAUDE.md` 的 `## Strategy Design` 小节：把 “B = Locked-win aggressor” 的说明补上 “也对 forecast entry 使用 0.6 Kelly（A=0.5）”。
- `CLAUDE.md` 的 `## Known Pitfalls`：新增一条 “Locked-win price cap 0.95 已废除，改由 `gap ≥ margin` + `ev > 0` 双门过滤”。
- README 暂不改（没有变体级细节）。
