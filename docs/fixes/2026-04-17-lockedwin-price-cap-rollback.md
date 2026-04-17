# Fix 2 Partial Rollback — Reinstate Locked-Win Price Cap (≤ 0.95)

**Date:** 2026-04-17
**Branch / commit:** `claude/ecstatic-curie` — see git log of this branch
**Scope:** `src/strategy/evaluator.py` (locked-win path), `tests/test_locked_win.py`, `CLAUDE.md`
**Reverts (partial):** Fix 2 from `2026-04-16-strategy-p0-fixes.md` (commit `035d353`)
**Does NOT touch:** Fix 1 (A/B kelly differentiation), Fix 3 (REJECT observability), Fix 4 (TRIM dual-gate), Fix 5 (thin-liquidity per-city cap)

---

## 1. 现象

部署 Fix 2 后第一个完整 cycle (~2026-04-17) 的生产数据:

| 指标                      | 数值                          |
|--------------------------|------------------------------|
| Locked-win 触发次数        | 17                           |
| 价格分布 (NO price)        | **全部** 集中在 0.997 – 0.9985 |
| 平均 NO price             | 0.9982                       |
| 平均 EV / share           | 0.0008 (≈ 0.08¢)            |
| 总暴露 (cost basis)        | $61.69                       |
| 期望收益 (sum EV × shares) | ≈ $0.05                      |
| 隐含年化                   | ~0.03%                       |
| BE buffer                | 4.6 bp                       |

— 17/17 信号都压在 NO 0.997 以上的极薄边际带，没有一笔落在我们设计 locked-win 时设想的 "市场漏标的深 OOM 锁定胜" 区间。

## 2. 根因

### 2.1 Fee 计算正确，但 slippage 没被建模
- Polymarket 的 taker fee 是概率加权 (`TAKER_FEE_RATE × 2 × p × (1-p)`)，不是 flat 2%。Fix 2 在 EV 公式里这部分是对的。
- 但 **paper→live 的 1 tick = 0.001 的最小价位移动 (slippage)** 在 0.997+ 区间会直接吃掉整个 EV：
  - price=0.998 时，EV ≈ 0.999 × 0.002 − 0.001 × 0.998 − fee ≈ 0.001 − 0.0006 ≈ +0.0004
  - 如果实盘成交在 0.999 而不是 0.998（仅一 tick 的滑点），EV → −0.0006，直接负值。
- 也就是说：纸面上 +EV，但 fill 概率非零地把整批信号变成 negative EV。

### 2.2 架构上 15-min cycle + mid-price ≠ orderbook sniping
- 真要在 0.997+ 区间赚钱，需要的是 **orderbook-watcher**：实时盯着对手挂的 sell 单，在挂出的瞬间 take。
- 当前 bot 是 **15-min cycle + Gamma mid-price** 决策模型。等 15 分钟到了再去 take 时，深 OOM slot 在 0.998+ 已经没有真实可吃的 liquidity（最后一条 sell 早被别人吃了），剩下的全是更深的虚价。
- 后果：信号触发但实际成交价远高于决策价；或更糟，部分成交后剩下 0.999 的 dust。

### 2.3 单门槛 `ev > 0` 把 "技术上 +EV 但实际收不到" 的单放进来
- Fix 2 的核心假设：`ev > 0` 就够了，市场会自己定价到 EV ≈ 0 的均衡点，所以只要还能 +EV 就有套利。
- 失败原因：fee + slippage 在实盘下的真实摩擦远大于 mid-price 模型推算出的 fee，单 EV 门槛在 0.997+ 区间无法区分 "真有 edge" 和 "技术 EV 但 fill 不到"。

## 3. 方案 (本次改动)

**保留 Fix 2 的好部分；只把硬价帽加回来。**

### 3.1 改动一: 加回 `LOCKED_WIN_MAX_PRICE = 0.95` 硬门槛

`src/strategy/evaluator.py` 模块顶部新增常量:

```python
LOCKED_WIN_MAX_PRICE: float = 0.95
```

`evaluate_locked_win_signals()` 在判定 `is_locked = True` 之后、计算 EV 之前增加一道硬过滤:

```python
if slot.price_no > LOCKED_WIN_MAX_PRICE:
    logger.debug(
        "LOCKED WIN skip (price %.4f > LOCKED_WIN_MAX_PRICE %.2f): "
        "%s slot %s — margin too thin for live execution",
        slot.price_no, LOCKED_WIN_MAX_PRICE,
        event.city, slot.outcome_label,
    )
    continue
```

debug-log 显式写出 skip 原因 — 不静默丢弃，方便后续 grep 验证。

### 3.2 改动二: `ev > 0` 安全网保留

价帽过完之后，原有的 `if ev <= 0: continue` 仍在，作为第二道闸门。**两道过滤同时生效** —— 任意一道拒绝就 skip，没有放宽。

### 3.3 保留 Fix 2 的非回退部分

- ✅ Below-slot lock vs above-slot lock 的 `is_below_lock` 区分逻辑
- ✅ Below-slot lock 的 `win_prob = 0.999` (温度只能升不能降的有界确定性)
- ✅ Above-slot lock 的 `win_prob = 0.99` (午后峰值仍可能上窜)
- ✅ `is_below_lock` 的字段语义、`lock_reason` 字符串

这部分语义本身是对的，只是触发范围被压缩到 `[min_no_price, 0.95]`。

### 3.4 不回退其他 Fix

| Fix | 状态 |
|-----|------|
| Fix 1 (A/B kelly differentiation, 2b2df21) | ✅ 保留 |
| Fix 2 (locked-win 0.95 cap removal, 035d353) | ⚠️ **本次部分回退** — cap 加回，win_prob 分流保留 |
| Fix 3 (REJECT observability, 0a9713f) | ✅ 保留 |
| Fix 4 (TRIM dual-gate, 40793f2) | ✅ 保留 |
| Fix 5 (thin-liquidity per-city cap, c18dc8e) | ✅ 保留 |

## 4. 长期方向 (不在本次 PR 范围)

如果未来要认真做 locked-win sniping：

1. **独立 orderbook-watcher 模块**: 脱离 15-min cycle，订阅 CLOB websocket 或高频轮询 orderbook，看到 sell 单挂出立刻 IOC take。
2. **Tick-aware EV**: 把 Polymarket 最小 tick (0.001) 和 expected slippage 显式建模进 EV 公式 (`ev_after_slippage = ev - slippage_per_share × P(fill_at_worse_price)`)。
3. **更细的 fill 模型**: 区分 take vs make，sniping 走 take (高 fee)，bid-side market-make 走 make (零 fee 但需要被动等成交)。

这三项都是新模块/新架构，与 15-min rebalance bot 的设计目标正交，留作后续 follow-up。

## 5. 测试

`tests/test_locked_win.py`:

### 修改
- `test_below_slot_lock_accepts_high_price` (Fix 2 引入，price=0.97 应 accept) — **删除**。这是 Fix 2 设计的回归保护测试，本次回退是 *intentional*，不是 regression，所以测试本身要去掉。
- `test_price_no_very_high_negative_ev` → `test_price_no_very_high_rejected` — docstring 更新，price=0.999 现在被价帽拒绝（之前是被负 EV 拒绝），但 expected behavior 仍是 `len(sigs) == 0`。

### 新增 (`TestLockedWinPriceCap` class, 7 tests)
| 测试 | 场景 | 期望 |
|------|------|------|
| `test_below_lock_at_0_94_accepted` | below-lock + price=0.94 | accept, EV≈0.06 |
| `test_below_lock_at_0_95_accepted_boundary` | price=0.95 (恰在 cap 上, gate 是 `>`) | accept |
| `test_below_lock_at_0_96_rejected_by_cap` | below-lock + price=0.96 | reject by cap |
| `test_below_lock_at_0_999_rejected_by_cap` | 复刻生产环境 0.999 case | reject by cap |
| `test_above_lock_at_0_94_accepted` | above-lock + price=0.94 | accept, win_prob=0.99 |
| `test_above_lock_at_0_96_rejected_by_cap` | above-lock + price=0.96 | reject by cap |
| `test_constant_value` | regression: `LOCKED_WIN_MAX_PRICE == 0.95` | passes |

### 全量结果

```
.venv/bin/python -m pytest tests/ --ignore=tests/dry_run_offline.py \
    --ignore=tests/run_backtest_offline.py -q
→ 798 passed in ~170s
```

(Fix 2 baseline 为 792；本次净 +6 = 加 7 cap-related tests，删 1 个 Fix 2 高价 accept 测试。)

## 6. 部署后验证方法

1. **Paper 部署**: 跑 1-2 天，观察 dashboard 上 locked-win 触发次数 + 价格分布。
2. **预期**: 触发次数应该比 Fix 2 (17/cycle) 大幅下降，**价格分布应集中回到 0.7 - 0.95 区间**（真正有 edge 的深 OOM）。如果观察期内 0 触发，说明当前市场状态下确实没有 cap 之下的 lock 机会，这是预期行为，不是 bug。
3. **Decision_log 抽样**: REJECT 抽样 (Fix 3) 现在可以记录 `LOCKED_WIN skip (price ...)` 的具体次数，方便事后做 fee/slippage 校准。
4. **若 paper 观察期 7 天 0 触发** → 重新讨论：要么把 cap 放宽到 0.97（接受少量薄边际单），要么直接关闭 locked-win 路径，只保留 forecast-based NO entry。

## 7. 文件变更总览

| 文件 | 类型 |
|------|------|
| `src/strategy/evaluator.py` | 加常量 `LOCKED_WIN_MAX_PRICE`；`evaluate_locked_win_signals` 价帽 gate |
| `tests/test_locked_win.py` | 删 1 测试，重命名 1 测试，新增 1 个 class (7 tests) |
| `CLAUDE.md` | Strategy Design + Known Pitfalls 两段表述同步 |
| `docs/fixes/2026-04-17-lockedwin-price-cap-rollback.md` | 本文档 |

## 8. Review 备注

- 本 PR **不 push、不 merge main**。
- 等 review 通过后再决定合入流程（参考过往 worktree workflow）。
