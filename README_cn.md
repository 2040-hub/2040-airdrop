# NFT Holder USDC 随机空投

向 Solana NFT 持有者随机空投 USDC，使用可配置的 Dirichlet 分布算法。

## 工作流程

1. 通过 Cloudflare Worker API 获取指定 NFT Collection 的所有持有者地址
2. 可选：通过地址映射将空投重定向到持有者指定的其他钱包
3. 使用可配置的 Dirichlet 分布将总 USDC 金额随机分配给每个持有者（每人至少获得 `min_usdc_amount`）
4. 逐笔发送 USDC 转账交易（自动为没有 USDC ATA 的地址创建关联代币账户）

## 环境要求

- Python 3.8+

## 安装

```bash
pip install -r requirements.txt
```

依赖包：`requests`、`solana`、`solders`、`base58`

## 配置

编辑 `config.ini`：

```ini
[settings]
nft_worker_url = https://nft-holders.your-account.workers.dev
nft_collection_id = 6mS3jtvnxPfdEny4JgEp3v3u6Atc32dLZ6jwE2cmZXWL
dry_run = true
private_key = YOUR_BASE58_PRIVATE_KEY_HERE
rpc_url = https://api.mainnet-beta.solana.com
total_usdc_amount = 10
min_usdc_amount = 0.1
tx_sleep_time = 1
tx_max_retries = 5
address_mapping_file = address_mapping.json
distribution_alpha = 0.1
```

### 配置项说明

| 配置项 | 说明 | 示例 |
|---|---|---|
| `nft_worker_url` | 获取 NFT 持有者的 Worker API 地址 | `https://nft-holders.xxx.workers.dev` |
| `nft_collection_id` | NFT Collection ID | `6mS3jtvn...` |
| `dry_run` | 设为 `true` 时只打印分配计划，不发送交易 | `true` / `false` |
| `private_key` | 发送方钱包的 Base58 私钥 | - |
| `rpc_url` | Solana RPC 节点地址 | `https://api.mainnet-beta.solana.com` |
| `total_usdc_amount` | 本次空投的 USDC 总额 | `10` |
| `min_usdc_amount` | 每个地址最少收到的 USDC 数量 | `0.1` |
| `tx_sleep_time` | 每笔转账之间的等待秒数 | `1` |
| `tx_max_retries` | 单笔转账失败后的最大重试次数 | `5` |
| `address_mapping_file` | 地址映射 JSON 文件路径，将持有者地址映射到其他接收地址（可选，留空则不启用） | `address_mapping.json` |
| `distribution_alpha` | Dirichlet 分布的 alpha 参数，控制随机分配的方差大小（默认：`1.0`）。`1.0` = 经典切割线段算法（适度随机）。`< 1.0` = 方差更大（少数人拿大额，多数人拿小额）。`> 1.0` = 方差更小（金额趋于均分）。推荐值：`0.1`–`0.5` 可产生刺激的分配效果，偶尔出现大额赢家。 | `0.3` |

## 分配算法

脚本使用 **Dirichlet 分布** 将 USDC 资金池随机分配给持有者。`distribution_alpha` 参数控制随机性的「激烈程度」：

| `distribution_alpha` | 行为 | 最大金额概率（102 持有者, 8099.7 USDC, 保底 20） |
|---|---|---|
| `1.0`（默认） | 经典「切割线段」— 适度随机，类似微信随机红包 | 最大值 > 1000: ~0%, 最大值 > 500: ~2.5% |
| `0.5` | 中等高方差 — 部分幸运持有者明显获得更多 | 最大值 > 1000: ~0.3%, 最大值 > 500: ~36% |
| `0.3` | 高方差 — 偶尔出现大额赢家 | 最大值 > 1000: ~5%, 最大值 > 2000: ~0.01% |
| `0.2` | 很高方差 — 刺激的分配效果，赢家明显 | 最大值 > 1000: ~20%, 最大值 > 2000: ~0.1% |
| `0.1` | 极端方差 — 少数人赢大额，多数人接近保底 | 最大值 > 1000: ~69%, 最大值 > 2000: ~6% |

**工作原理：** 每个持有者首先获得保底金额（`min_usdc_amount`）。剩余资金池使用 Dirichlet(alpha) 分布进行分割 — 这是一种定义在单纯形上的概率分布。当 alpha < 1 时，分布将概率质量集中在少数持有者上，产生「大赢家」。当 alpha = 1 时，等价于均匀的切割线段算法。当 alpha > 1 时，金额趋向于均分。

无论 alpha 取何值，分发总额永远不会超过 `total_usdc_amount`。

## 地址映射

地址映射功能允许将特定 NFT 持有者的空投重定向到其他钱包地址。当持有者希望用与持有 NFT 不同的地址接收 USDC 时，此功能非常有用。

创建一个 JSON 文件（如 `address_mapping.json`）：

```json
{
    "原始持有者地址1": "实际接收地址1",
    "原始持有者地址2": "实际接收地址2"
}
```

在 `config.ini` 中设置 `address_mapping_file = address_mapping.json` 即可启用。启用后脚本会：

- 验证所有地址（源地址和目标地址）是否为有效的 Solana 公钥
- 跳过源地址与目标地址相同的映射
- 当多个持有者映射到同一接收地址时发出警告
- 记录所有已应用的映射，确保透明可追溯

没有映射条目的持有者将照常在其原始地址接收空投。

## 使用

### 1. 测试模式（dry run）

确保 `config.ini` 中 `dry_run = true`，运行后只会获取持有者地址并打印随机分配方案，不会发起任何链上交易：

```bash
python nft_airdrop.py
```

输出示例：

```
2026-02-06 12:00:00 [INFO] ============================================================
2026-02-06 12:00:00 [INFO] NFT Holder USDC Random Airdrop
2026-02-06 12:00:00 [INFO] ============================================================
2026-02-06 12:00:00 [INFO] Collection ID : 6mS3jtvn...
2026-02-06 12:00:00 [INFO] Total USDC    : 100.0
2026-02-06 12:00:00 [INFO] Min per holder: 0.5
2026-02-06 12:00:00 [INFO] Dry run       : True
2026-02-06 12:00:00 [INFO] Addr mapping  : address_mapping.json
2026-02-06 12:00:00 [INFO] Distr. alpha  : 0.3 (Dirichlet, high variance)
2026-02-06 12:00:00 [INFO] Fetching holders for collection: 6mS3jtvn...
2026-02-06 12:00:01 [INFO] Got 10 unique holders
2026-02-06 12:00:01 [INFO] Distribution plan (10 holders, total=100.0 USDC):
2026-02-06 12:00:01 [INFO]   [  1/10] 7xKXtg...AsU -> 3.182345 USDC
2026-02-06 12:00:01 [INFO]   [  2/10] 3yFwqX...y1E -> 45.432100 USDC
...
2026-02-06 12:00:01 [INFO] [DRY RUN] No transactions will be sent.
```

### 2. 测试地址映射

在执行完整空投之前，可以通过向每个映射地址发送小额固定金额的 USDC 来验证地址映射是否正确：

```bash
python nft_airdrop.py --test mapping 0.01
```

此命令会：

- 加载地址映射文件
- 获取当前 NFT 持有者列表
- 找出有映射条目的持有者
- 向每个映射的目标地址发送指定金额（如 0.01 USDC）
- 忽略 `dry_run` 设置（始终执行真实交易）
- 对不是当前 NFT 持有者的映射地址发出警告

这对于在正式空投前确认映射钱包能否正常接收 USDC 非常有用。

### 3. 正式执行

确认分配方案无误后，将 `dry_run` 改为 `false`，重新运行：

```bash
python nft_airdrop.py
```

## 安全机制

- **总额硬上限**：生成随机金额后会 assert 校验总和不超过 `total_usdc_amount`，每笔转账前再次检查累计金额
- **自动重试**：遇到 `Blockhash not found`（等待 2s）或 `429 Too Many Requests`（递增退避 3s/6s/9s...）自动重试，最大重试次数由 `tx_max_retries` 控制
- **ATA 自动创建**：如果接收方没有 USDC 关联代币账户，会自动创建（由发送方支付 rent）
- **地址映射验证**：映射文件中的所有地址都会被验证为合法的 Solana 公钥。重复目标地址警告和非持有者映射警告有助于尽早发现配置错误
- **测试金额保护**：`--test` 模式会拒绝超过 1,000,000 USDC 的金额作为安全检查

## 注意事项

- **请勿将私钥提交到 Git 仓库**，建议将 `config.ini` 加入 `.gitignore`
- 发送方钱包需要有足够的 USDC 余额和少量 SOL 用于支付交易费及 ATA 创建的 rent
- 使用地址映射时，建议先运行 `--test mapping <小额金额>` 验证映射地址能否正常接收 USDC