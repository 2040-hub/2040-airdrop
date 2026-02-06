# NFT Holder USDC Random Airdrop

向 Solana NFT 持有者随机空投 USDC，类似微信随机红包的分配方式。

## 工作流程

1. 通过 Cloudflare Worker API 获取指定 NFT Collection 的所有持有者地址
2. 使用「切割线段」算法将总 USDC 金额随机分配给每个持有者（每人至少获得 `min_usdc_amount`）
3. 逐笔发送 USDC 转账交易（自动为没有 USDC ATA 的地址创建关联代币账户）

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

## 使用

### 1. 测试模式（dry run）

确保 `config.ini` 中 `dry_run = true`，运行后只会获取持有者地址并打印随机分配方案，不会发起任何链上交易：

```bash
python airdrop_usdc.py
```

输出示例：

```
2026-02-06 12:00:00 [INFO] ============================================================
2026-02-06 12:00:00 [INFO] NFT Holder USDC Random Airdrop
2026-02-06 12:00:00 [INFO] ============================================================
2026-02-06 12:00:00 [INFO] Collection ID : 6mS3jtvn...
2026-02-06 12:00:00 [INFO] Total USDC    : 10.0
2026-02-06 12:00:00 [INFO] Min per holder: 0.1
2026-02-06 12:00:00 [INFO] Dry run       : True
2026-02-06 12:00:00 [INFO] Fetching holders for collection: 6mS3jtvn...
2026-02-06 12:00:01 [INFO] Got 23 unique holders
2026-02-06 12:00:01 [INFO] Distribution plan (23 holders, total=10.0 USDC):
2026-02-06 12:00:01 [INFO]   [  1/23] 7xKXtg...AsU -> 0.318472 USDC
2026-02-06 12:00:01 [INFO]   [  2/23] 3yFwqX...y1E -> 0.142856 USDC
...
2026-02-06 12:00:01 [INFO] [DRY RUN] No transactions will be sent.
```

### 2. 正式执行

确认分配方案无误后，将 `dry_run` 改为 `false`，重新运行：

```bash
python airdrop_usdc.py
```

## 安全机制

- **总额硬上限**：生成随机金额后会 assert 校验总和不超过 `total_usdc_amount`，每笔转账前再次检查累计金额
- **自动重试**：遇到 `Blockhash not found`（等待 2s）或 `429 Too Many Requests`（递增退避 3s/6s/9s...）自动重试，最大重试次数由 `tx_max_retries` 控制
- **ATA 自动创建**：如果接收方没有 USDC 关联代币账户，会自动创建（由发送方支付 rent）

## 注意事项

- **请勿将私钥提交到 Git 仓库**，建议将 `config.ini` 加入 `.gitignore`
- 发送方钱包需要有足够的 USDC 余额和少量 SOL 用于支付交易费及 ATA 创建的 rent
- 当 `min_usdc_amount × 持有者数量 > total_usdc_amount` 时脚本会报错退出，请调整配置