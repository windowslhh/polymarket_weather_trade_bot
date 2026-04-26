# Phase 2 Redeploy Runbook

> 状态:**等待 user review**。所有代码改动已完成(11 个 FIX,996/996 tests),HEAD = `4952837`。本文档是 review 通过后的执行准备 + 部署后的监控清单,不是任何动作的触发器。
>
> 三部分:
> - **Part A** — 给运维看的 backtest 报告口语化解读
> - **Part B** — Day 2 redeploy 操作 checklist(增量更新,不重置 DB)
> - **Part C** — Paper 24h 监控 SQL 清单(重点验 city-local forecast 和 UTC 跨夜窗口)

---

## Part A — Backtest 报告通俗解读

完整原始数据在 `docs/backtests/2026-04-26-new-fee.md`。这一节用大白话把"修了什么、影响多少、为什么不慌"讲清楚。

### A.1 我们之前算错了什么

Polymarket 在 2026-03-30 把 Weather 类目的吃单费率(taker fee)调到了 **5%**。代码里硬编码的是 1.25%,而且公式还多乘了一个 ×2 因子。两个 bug 叠加,最终算出的费用是真实费用的 **一半**(0.025 vs 0.05)。

这意味着:
- 所有历史 backtest 报告的 net PnL 都被高估了
- 所有 EV 计算都偏乐观
- LOCKED_WIN 在 0.95 附近的"安全裕度"被压缩到 paper→live slippage 都吃不消的程度(这其实就是 FIX-17 把 cap 调到 0.90 的原因之一,只是当时不知道 fee 还错了)

### A.2 "per-share EV" vs "per-dollar EV" 是啥?

这俩术语在表格里反复出现,看一次就明白:

| 概念 | 单位 | 含义 |
|---|---|---|
| **per-share EV** | $/share | 你买**一股** NO token,平均能赚多少。一股的成本 = price_no(美元),如果赢了拿回 $1 |
| **per-dollar EV** | $/$ invested | 你投**一美元**进去,平均能赚多少。等于 per-share EV ÷ price_no |

**举个具体例子**:在 price_no = 0.90 处买 LOCKED_WIN,你押 $5(= 5/0.90 ≈ 5.56 股):
- per-share EV ≈ +0.085(每股赚 8.5 分)
- per-dollar EV ≈ +0.095(每投入 1 块赚 9.5 分)
- 你这 $5 仓位的预期利润 ≈ 5 × 0.095 = **+$0.475**

报告里 LOCKED_WIN 表格用的是 per-dollar(便于横向比较不同 price)。

### A.3 数字到底掉了多少?

10 城市 × 365 天合成数据,FORECAST_NO 路径(LOCKED_WIN 不在引擎里):

| Variant | 旧 fee 下 Net PnL | 新 fee 下 Net PnL | 差额 | 相对降幅 |
|---|---|---|---|---|
| **B**(主力) | +$743.64 | **+$663.79** | −$80 | −10.7% |
| **C**(高 EV gate) | +$221.19 | **+$189.16** | −$32 | −14.5% |
| **D'**(白名单) | +$69.61 | **+$56.27** | −$13 | −19.2% |

**ROI% 几乎没变**(B 7.42% → 7.40%, C 8.23% → 8.08%, D 9.94% → 9.62%),原因是 EV gate 把 ROI 锚定在阈值附近,fee 变了仓位变小但单位回报率一样。

**Trade 数量下降了 10–17%** —— EV gate 现在更严,原本压线的边缘交易被过滤掉。这是好事,过滤掉的本来就是"刚够看"那批。

### A.4 LOCKED_WIN 在新 fee 下还能跑吗?

**能,而且 0.90 cap 仍然合理。** 看这个表(win_prob = 0.999,即"下方锁单"的高频场景):

| price_no | 旧 EV/$ | 新 EV/$ | 备注 |
|---|---|---|---|
| 0.85 | +0.146 | **+0.143** | 利润空间大,fee 影响微乎其微 |
| **0.90** | **+0.097** | **+0.095** | 当前 cap,EV/$ 相当健康 |
| 0.95 | +0.048 | +0.047 | EV 已经变薄 |
| 0.97 | +0.028 | +0.028 | 1-tick slippage(0.001)开始能吃掉相当一部分 |
| 0.997 | +0.002 | +0.002 | **slippage 把 EV 吃干**,FIX-17 把 cap 砍到 0.90 就是为了远离这个区域 |

关键直觉:
- p 越靠近 1,p × (1−p) 越小 → fee 绝对值越小 → 看上去 fee 影响在变弱
- **但** EV 本身也在以更快速度趋近 0,所以"fee 占 EV 的比例"反而越来越大
- p = 0.95 的时候,EV 是 4.7 分/$,1 tick slippage(0.001/0.95 ≈ 0.001 per dollar)就吃掉 2% 的 EV。p = 0.997 的时候,1 tick 直接把 EV 干掉一大半

**结论**:0.90 cap 不动是对的。要不要进一步调严(比如降到 0.88),等 paper 跑过新 fee 看真实数据。

### A.5 报告**没**告诉你的事(诚实声明)

1. **真实 Polymarket 成交滑点**没在 backtest 里。1 tick = 0.001,在 LOCKED_WIN 的高价区是比 fee 更主要的损耗。
2. **LOCKED_WIN 真实仓位规模**没在引擎里(用的是 full Kelly,单笔可能比 FORECAST_NO 大 2–4 倍)。所以累计 fee 影响比这个表大。
3. 一个 `min_trim_ev_absolute` 边界 case 在 fee 修正后有微小变化(单元测试已覆盖,无需运维操心)。

### A.6 等运维拍板的事(FIX-2P-12)

代码里**没动 config.yaml 里 variant 参数**。等 paper 跑过新 fee 24h 看真实数据后,可能要做下面三件事中的一件或多件。我倾向先观察、再决策,理由各列:

| 问题 | 当前值 | 我的倾向 | 理由 |
|---|---|---|---|
| 调严 `locked_win_max_price`? | 0.90 | **不动** | 解析数据看 0.90 仍有 9.5¢/$ EV;真实数据如果 LOCKED_WIN 实际止盈低于 9¢/$ 才需要调 |
| 上调 `min_no_ev`? | B/C 用默认,D 已 0.08 | **不动** | 三个 variant ROI 全为正;真实 win_rate 通常显著低于合成的 99.5%,要看真实数据 |
| 调整 D' 白名单? | LA/Seattle/Denver | **不动** | 解析数据没法回答这个,必须看真实 24h |

→ **走流程**:Day 2 paper 部署 → 跑满 24h → 收集 SQL(Part C)→ 决定要不要 FIX-2P-12 → 真实改完再 redeploy

---

## Part B — Day 2 Redeploy Checklist(草案)

> **重要**:这是**增量更新**,不是新部署。不能用 `scripts/full_reset_and_deploy.sh`(那个会清空 DB)。手动按下面 14 步走。
>
> **前置条件**:Part C 的监控查询会基于 paper 模式,所以本次部署保持 `--paper`,不切 live。Live 切换走原 runbook Part 3(`docs/runbook/go_live_runbook.md`)。

### B.0 Pre-flight on laptop(SSH 之前)

- [ ] 确认 review 已通过、无新 Blocker 待修
- [ ] 本地 pytest 全绿(增量也算):
      ```
      .venv/bin/python -m pytest tests/ \
          --ignore=tests/dry_run_offline.py \
          --ignore=tests/run_backtest_offline.py -q
      ```
      期望:`996 passed`(基线 974 + 22 新增 = 996)
- [ ] 确认 HEAD = `4952837`(`docs(FIX-2P-4)` 这条):
      ```
      git rev-parse HEAD
      ```
- [ ] 把 HEAD 推到远端(假设上游 = origin):
      ```
      git push origin HEAD
      ```
      记下推送的目标分支名,B.4 要 pull 同一个

### B.1 SSH 进 VPS,先看现状

- [ ] ```
      sshpass -p "<VPS_PASSWORD>" ssh -o StrictHostKeyChecking=no root@198.23.134.31
      ```
- [ ] ```
      cd /opt/weather-bot-new
      docker compose ps
      docker compose logs --tail=50 weather-bot
      ```
      期望:看到当前 paper 容器 healthy、Up 25h+,日志里有 rebalance/position-check 节奏

- [ ] **抓一份当前状态做对比基线**(B.10 部署完之后要回头比):
      ```
      docker compose exec weather-bot sqlite3 /app/data/bot.db \
        "SELECT 'positions', COUNT(*) FROM positions UNION ALL
         SELECT 'orders',    COUNT(*) FROM orders    UNION ALL
         SELECT 'settlements', COUNT(*) FROM settlements UNION ALL
         SELECT 'decision_log', COUNT(*) FROM decision_log"
      ```
      记下四个数字。后面对比"redeploy 没丢数据"。

### B.2 备份 DB(强制必做)

- [ ] ```
      mkdir -p /opt/weather-bot-new/data/backups
      cp /opt/weather-bot-new/data/bot.db \
         /opt/weather-bot-new/data/backups/bot-pre-phase2-$(date -u +%Y%m%dT%H%M%S).db
      ls -la /opt/weather-bot-new/data/backups | tail -3
      ```
      期望:新文件 > 0 字节,文件名带 UTC 时间戳

### B.3 ⚠️ git stash 坑预案(关键步骤)

一期部署踩过的坑:VPS 上 `config.yaml` 被运维手动调过(smoke 阶段的 daily_loss_limit / max_total_exposure 收紧值),但本地 commit 也 touch 了同一文件,`git pull` 报 merge conflict 后操作员急着 `git stash` → `git pull` → 忘了 `git stash pop` → VPS-only config 永久丢失。

**安全做法**:

- [ ] 先看 VPS 上有没有未 commit 改动:
      ```
      cd /opt/weather-bot-new
      git status
      git diff -- config.yaml
      ```
- [ ] 如果 `config.yaml` 是 Modified:
      - **第 1 步**先把 VPS-only 改动**抄到 backup 文件**(不是依赖 git stash):
        ```
        cp config.yaml config.yaml.vps-override-$(date -u +%Y%m%dT%H%M%S)
        ls -la config.yaml.vps-override-*
        ```
      - **第 2 步**才走 stash:
        ```
        git stash push -m "phase2 redeploy: vps-only config snapshot $(date -u +%Y-%m-%dT%H:%M:%S)"
        git stash list
        ```
        记下 stash 编号(通常是 stash@{0})
- [ ] 如果 `git status` 显示 clean,跳到 B.4

### B.4 拉新代码

- [ ] 先 fetch + 预览要拉的 commit 列表:
      ```
      git fetch origin
      git log HEAD..origin/<your-branch> --oneline
      ```
      期望:看到 11 条 `fix(FIX-2P-1)` 到 `docs(FIX-2P-4)` 的 commit
- [ ] Pull(分支名替换成 B.0 推送目标):
      ```
      git pull origin <your-branch>
      git rev-parse HEAD
      ```
      期望:HEAD = `4952837`(或更新的,如果 review 又压了 commit)

- [ ] 如果 B.3 做过 stash,**现在恢复**:
      ```
      git stash pop
      git status        # 检查冲突
      ```
      - 如果 `git stash pop` 报冲突(本次 PR 也改了 config.yaml),手动 merge:
        - 比对 `config.yaml.vps-override-*` 和 `config.yaml` 当前内容
        - 决定保留哪些 VPS override(通常是 `daily_loss_limit_usd` / `max_total_exposure_usd` / per-city cap)
        - 编辑 `config.yaml`,`git add config.yaml`,`git stash drop`
      - **绝对不要**`git checkout -- config.yaml`(会丢 VPS override)

### B.5 验证关键代码确实落地

- [ ] FIX-2P-1 OrderArgs:
      ```
      grep -n "OrderArgs(" src/markets/clob_client.py
      ```
      期望:出现 `OrderArgs(token_id=...,price=...,size=...,side=...)`
- [ ] FIX-2P-2 Fee 5%:
      ```
      grep -n "TAKER_FEE_RATE" src/strategy/gates.py
      ```
      期望:`TAKER_FEE_RATE: float = 0.05`
- [ ] FIX-2P-3 city-local:
      ```
      grep -n "get_forecasts_for_city_local_window" src/strategy/rebalancer.py
      ```
      期望:三个调用点(backfill / refresh_forecasts / 主 rebalance)
- [ ] FIX-2P-7 chown 已固化:
      ```
      grep "chown 1000:1000" scripts/full_reset_and_deploy.sh scripts/backup_and_reset.sh
      ```
      期望:两个脚本各两行(`.env` + `data/`)

### B.6 ⚠️ 手动 chown(不依赖 deploy 脚本)

`full_reset_and_deploy.sh` 现在有自动 chown(FIX-2P-7),但这次是 redeploy 不调那个脚本。手动跑一次同样的 chown:

- [ ] ```
      chmod 600 /opt/weather-bot-new/.env
      chown 1000:1000 /opt/weather-bot-new/.env
      chown -R 1000:1000 /opt/weather-bot-new/data
      ls -la /opt/weather-bot-new/.env
      ls -la /opt/weather-bot-new/data | head -3
      ```
      期望:`.env` = `-rw-------` 且 owner `1000`,`data/bot.db` owner `1000`

### B.7 优雅停容器

- [ ] ```
      cd /opt/weather-bot-new
      docker compose down
      ```
      期望日志里看到 `waiting up to 30s for N in-flight trade(s) to drain` 或 `Executor has 0 in-flight`,然后 `Bot stopped.`
- [ ] **不要** `docker compose kill`(会留 pending orders 给 reconciler 吃)

### B.8 重建镜像

- [ ] ```
      docker compose build --no-cache weather-bot
      ```
      期望最后一行 `Successfully tagged weather-bot-new...`,无 `ERROR` 关键字

### B.9 起容器

- [ ] ```
      docker compose up -d
      ```
- [ ] 等 startup window 过完(preflight + reconciler + historical dist + first forecast batch):
      ```
      sleep 125
      docker compose ps
      ```
      期望 STATUS = `Up N minutes (healthy)`

### B.10 健康检查 + 数据守恒

- [ ] API 通:
      ```
      curl -s http://localhost:5002/api/status | python3 -m json.tool
      ```
      期望 HTTP 200,JSON 里 `last_run` 不是 null
- [ ] **DB 行数和 B.1 比对**(redeploy 不应该丢任何数据):
      ```
      docker compose exec weather-bot sqlite3 /app/data/bot.db \
        "SELECT 'positions', COUNT(*) FROM positions UNION ALL
         SELECT 'orders',    COUNT(*) FROM orders    UNION ALL
         SELECT 'settlements', COUNT(*) FROM settlements UNION ALL
         SELECT 'decision_log', COUNT(*) FROM decision_log"
      ```
      四个数字 ≥ B.1 的数字(允许涨,不许跌)

### B.11 Phase 2 特定的"代码确实生效"日志检查

这一步是 redeploy 区别于一期的核心:验 11 个 FIX 真的在跑。

- [ ] 启动日志里能看到 city-local windowing(FIX-2P-3):
      ```
      docker compose logs --tail=200 weather-bot | grep -iE "city-local|forecast.*D1|D2"
      ```
      期望至少一行 `Fetched forecasts for N cities (city-local today + D1/D2)` 或 `Backfilled forecasts for N cities (city-local today + D1/D2)`
- [ ] 没有 FIX-22 invariant 异常(原 H-9 的症状):
      ```
      docker compose logs --since 5m weather-bot | grep -iE "AssertionError|forecast_date.*market_date"
      ```
      期望 **0 行**。任何一行都说明 FIX-2P-3 没起作用 → 立即停 → 复盘
- [ ] 没有 OrderArgs 相关错误(FIX-2P-1,paper 模式不会真触发,但别有 import 报错):
      ```
      docker compose logs --tail=400 weather-bot | grep -iE "OrderArgs|create_and_post_order"
      ```
      期望:**0 行**(paper 早 return,根本不到这一步)
- [ ] Preflight 跑过 + 在 paper 模式跳过 fee_rate 校验(FIX-2P-6):
      ```
      docker compose logs --tail=400 weather-bot | grep -iE "Preflight|fee_rate"
      ```
      期望:`Preflight: db_writable`、`Preflight: clob_skipped_non_live`、(可能有)`Preflight: fee_rate_skipped_non_live`
- [ ] Dashboard 验前端没有 strategy A 卡(FIX-2P-5):
      ```
      curl -s http://localhost:5002/ | grep -oE "Strategy [A-D/]+"
      ```
      期望:`Strategy B/C/D`(不是 `Strategy A/B/C/D`)

### B.12 容器内 pytest

- [ ] ```
      docker compose exec weather-bot python -m pytest /app/tests/ \
          --ignore=/app/tests/dry_run_offline.py \
          --ignore=/app/tests/run_backtest_offline.py -q
      ```
      期望:`996 passed`(本地一致)

### B.13 优雅停 + 重启冒烟测试(FIX-09 还活着的验证)

- [ ] ```
      docker compose stop
      docker compose logs --tail=40 weather-bot | grep -iE "waiting|drained|abandoning|Bot stopped"
      ```
      期望 `waiting up to 30s for...` 或 `Executor has 0 in-flight`,然后 `Bot stopped.`
      **不**期望 `abandoning N in-flight trades`
- [ ] ```
      docker compose up -d
      sleep 125
      docker compose ps   # → healthy
      ```

### B.14 Deploy exit gate(全部 ✓ 才能离开 VPS)

- [ ] `docker compose ps` shows `healthy`
- [ ] `/api/status` returns 200
- [ ] B.1 vs B.10 行数 OK(无数据丢失)
- [ ] B.11 全部通过(city-local 日志可见 + 0 AssertionError + dashboard 没 A)
- [ ] B.12 容器内 pytest 996/996
- [ ] B.13 graceful stop emit FIX-09 日志,无 abandoning
- [ ] `.env` 是 `-rw-------` 且 UID 1000

---

## Part C — Paper 24h 监控 SQL 清单

> 目的:**这次 paper 跑 24h 的核心问题不是"赚不赚",是"FIX-2P-3 city-local 是否消除了 H-9"**。次要问题是新 fee 下 EV gate 行为变化。
>
> 所有命令在 VPS 上跑,假设当前目录 `/opt/weather-bot-new`。

### C.1 入门:健康节奏(每 2-4h 跑一次)

- [ ] 容器 still healthy:
      ```
      docker compose ps
      ```
- [ ] 最近 1h 有 cycle:
      ```
      docker compose logs --since 1h weather-bot | grep -E "rebalance cycle|Position check done" | tail -5
      ```
      期望:至少 1 条 rebalance(每 60min)+ 4 条 position check(每 15min)
- [ ] 最近 1h 错误数:
      ```
      docker compose logs --since 1h weather-bot | grep -iE "ERROR|CRITICAL" | wc -l
      ```
      期望:**0**(known noise 像 Gamma 422 retry 是 WARNING 不计)

### C.2 ⭐ FIX-2P-3 city-local 是否消除 H-9

H-9 在 audit 中是"position_check 失败 7 次/25h",全部在 UTC 跨夜(美东傍晚 = 03:00–08:00 UTC)。判定标准:

- [ ] 24h 内 **0 个** AssertionError / forecast_date mismatch:
      ```
      docker compose logs --since 24h weather-bot | grep -iE "AssertionError|forecast_date.*market_date|forecast.forecast_date"
      ```
      期望:**0 行**。任何一行 → FIX-2P-3 失效 → 立即报警
- [ ] 24h 内 **0 次** position_check 异常退出:
      ```
      docker compose logs --since 24h weather-bot | grep -iE "Position check failed|Position check.*raised"
      ```
      期望:**0 行**(实际日志关键短语来自 `rebalancer.py:887` 的 `logger.exception("Position check failed")`,grep 大小写敏感所以用 `-i`)
- [ ] 关键:UTC 03:00–08:00 窗口里 position_check **正常完成**(就是过去会炸的窗口):
      ```
      docker compose logs --timestamps --since 24h weather-bot | \
          grep "Position check done" | \
          awk '{print $2}' | \
          awk '$1 >= "03:00:00" && $1 < "08:00:00"' | wc -l
      ```
      日志行形如 `2026-04-26 03:15:00 [INFO] src.strategy.rebalancer: --- Position check done in 12.4s ---`,$2 是 HH:MM:SS,字符串比较即可。
      期望:**约 16-20 次**(5h × 4 cycles/h)。如果 < 10 → 跨夜窗口仍有问题。
      若 docker logs 时间戳不可用,fallback:
      ```
      docker compose exec weather-bot sh -c "tail -10000 /app/data/monitor_log.txt 2>/dev/null || echo 'no monitor log'"
      ```
- [ ] edge_history 里 `forecast_date != market_date` 的行数(应永远 0):
      ```
      docker compose exec weather-bot sqlite3 /app/data/bot.db \
        "SELECT COUNT(*) FROM edge_history WHERE forecast_date != market_date"
      ```
      期望:**0**

### C.3 新 fee(FIX-2P-2)对 EV gate 行为的影响

- [ ] 新增 BUY 数量 vs 上次 paper 周期:
      ```
      docker compose exec weather-bot sqlite3 /app/data/bot.db \
        "SELECT date(created_at) as d, strategy, COUNT(*) as buys
         FROM positions
         WHERE created_at > datetime('now', '-24 hours')
         GROUP BY d, strategy
         ORDER BY d, strategy"
      ```
      预期:B/C/D 各有,**没有新的 strategy='A' 行**。BUY 数量比之前同期低 5-15%(新 fee 让 marginal 交易过 gate 更难)
- [ ] EV_BELOW_GATE REJECT 占比变化:
      ```
      docker compose exec weather-bot sqlite3 /app/data/bot.db \
        "SELECT reason, COUNT(*) c FROM decision_log
         WHERE cycle_at > datetime('now','-24 hours') AND action='SKIP'
         GROUP BY reason ORDER BY c DESC"
      ```
      预期 EV_BELOW_GATE 比一期占比上升(因为 fee 更高了 → 更多 trade 被刷)
- [ ] LOCKED_WIN 实际触发价格分布:
      ```
      docker compose exec weather-bot sqlite3 /app/data/bot.db \
        "SELECT ROUND(entry_price, 2) p, COUNT(*) c
         FROM positions
         WHERE buy_reason LIKE '%LOCKED%'
         AND created_at > datetime('now','-24 hours')
         GROUP BY p ORDER BY p"
      ```
      预期:price 集中在 0.85–0.90 区间,**没有 ≥ 0.90 的行**(`locked_win_max_price` cap)

### C.4 FIX-2P-1 OrderArgs(paper 验不到,但确认没意外触发)

Paper 模式根本不调 SDK,所以这条是反向验证(没炸 = 通过):

- [ ] ```
      docker compose logs --since 24h weather-bot | grep -iE "OrderArgs|AttributeError.*token_id"
      ```
      期望:**0 行**

### C.5 FIX-2P-5 strategy A 已彻底退出活跃逻辑

- [ ] 24h 内**没有新建 strategy='A' 的 positions**:
      ```
      docker compose exec weather-bot sqlite3 /app/data/bot.db \
        "SELECT COUNT(*) FROM positions
         WHERE strategy='A'
         AND created_at > datetime('now','-24 hours')"
      ```
      期望:**0**(老的 A 行还在,新的不应该再来)
- [ ] Dashboard 端不再渲染 A 卡:
      ```
      curl -s http://localhost:5002/ | grep -c "Strategy [BCD]"
      curl -s http://localhost:5002/ | grep -c "Strategy A "
      ```
      期望:`Strategy B`/`C`/`D` 各 1,`Strategy A` 0

### C.6 FIX-2P-11 大写 reason code

- [ ] 新 REJECT 用的是大写:
      ```
      docker compose exec weather-bot sqlite3 /app/data/bot.db \
        "SELECT DISTINCT reason FROM decision_log
         WHERE cycle_at > datetime('now','-24 hours')
         AND reason LIKE '%WHITELIST%'"
      ```
      期望:看到 `[D] REJECT: CITY_NOT_IN_WHITELIST`,**没有** `city_not_in_whitelist`

### C.7 对账:orders ↔ positions 1:1(FIX-03 + FIX-05 invariant 仍守护)

- [ ] ```
      docker compose exec weather-bot sqlite3 /app/data/bot.db \
        "SELECT 'pending', COUNT(*) FROM orders WHERE status='pending'
         UNION ALL
         SELECT 'filled_BUY', COUNT(*) FROM orders WHERE status='filled' AND side='BUY'
         UNION ALL
         SELECT 'positions_non_legacy', COUNT(*) FROM positions
            WHERE source_order_id NOT IN ('legacy','')"
      ```
      期望:`pending = 0`(无 60min+ 残留),`filled_BUY count == positions_non_legacy count`

### C.8 24h 退出 gate(全部 ✓ 才能进 FIX-2P-12 决策)

- [ ] C.2 全部通过(0 AssertionError + 跨夜窗口 cycle 数正常)
- [ ] C.3 新 fee 数据收集完成(BUY count、REJECT 分布、LOCKED_WIN 价格分布)
- [ ] C.4 0 OrderArgs 异常
- [ ] C.5 0 个新建 strategy A
- [ ] C.6 reason code 全大写
- [ ] C.7 orders/positions 1:1
- [ ] **0 sys.exit(2) / sys.exit(3)** 重启:
      ```
      docker compose logs --since 24h weather-bot | grep -E "sys.exit\\(2\\)|sys.exit\\(3\\)|Reconciler MISMATCH"
      ```
- [ ] 一段汇总数据帖到 chat,我据此写 FIX-2P-12 提案

---

## Appendix — 紧急回滚

如果 B.10/B.11/B.12 任意一项不过,**立刻回滚**,不要尝试在线修:

```
# 1. 停容器
cd /opt/weather-bot-new
docker compose down

# 2. 恢复 DB
cp /opt/weather-bot-new/data/backups/bot-pre-phase2-<TIMESTAMP>.db \
   /opt/weather-bot-new/data/bot.db
chown 1000:1000 /opt/weather-bot-new/data/bot.db

# 3. 回退代码到一期 HEAD
git reset --hard 9dd83c9   # 一期 deployed HEAD: docs(runbook): add go-live runbook

# 4. 起回老镜像
docker compose up -d --build
sleep 125
docker compose ps   # → healthy
```

回滚完成后,**抄一份 logs 给开发**(`docker compose logs --tail=500 weather-bot > /tmp/phase2-rollback-logs.txt`),不要直接重试部署。
