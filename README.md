# NFT Holder USDC Random Airdrop

Randomly airdrop USDC to Solana NFT holders, using a distribution algorithm inspired by WeChat's random red envelopes.

## How It Works

1. Fetches all holder addresses for a given NFT collection via a Cloudflare Worker API
2. Splits the total USDC amount randomly across holders using a "cut the line" algorithm (each holder is guaranteed at least `min_usdc_amount`)
3. Sends USDC transfers one by one, automatically creating Associated Token Accounts (ATAs) for recipients who don't have one yet

## Requirements

- Python 3.8+

## Installation

```bash
pip install -r requirements.txt
```

Dependencies: `requests`, `solana`, `solders`, `base58`

## Configuration

Edit `config.ini`:

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

### Configuration Reference

| Option | Description | Example |
|---|---|---|
| `nft_worker_url` | Worker API endpoint that returns NFT holder addresses | `https://nft-holders.xxx.workers.dev` |
| `nft_collection_id` | NFT Collection ID to look up | `6mS3jtvn...` |
| `dry_run` | When `true`, prints the distribution plan without sending any transactions | `true` / `false` |
| `private_key` | Base58-encoded private key of the sender wallet | - |
| `rpc_url` | Solana RPC endpoint | `https://api.mainnet-beta.solana.com` |
| `total_usdc_amount` | Total USDC to distribute in this airdrop | `10` |
| `min_usdc_amount` | Minimum USDC each holder will receive | `0.1` |
| `tx_sleep_time` | Delay in seconds between each transfer | `1` |
| `tx_max_retries` | Maximum retry attempts per failed transaction | `5` |

## Usage

### 1. Test Run (Dry Run)

Make sure `dry_run = true` in `config.ini`. The script will fetch holder addresses and print the randomized distribution plan without submitting any on-chain transactions:

```bash
python airdrop_usdc.py
```

Sample output:

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

### 2. Go Live

Once you've reviewed the distribution plan and everything looks good, set `dry_run` to `false` and run again:

```bash
python airdrop_usdc.py
```

## Safety Measures

- **Hard cap on total amount**: After generating random amounts, the script asserts the sum never exceeds `total_usdc_amount`. A second check runs before each individual transfer to verify the cumulative total stays within bounds.
- **Automatic retries**: Transactions that fail with `Blockhash not found` are retried after a 2-second wait. `429 Too Many Requests` errors trigger incremental backoff (3s, 6s, 9s, ...). Both are capped at `tx_max_retries`.
- **Automatic ATA creation**: If a recipient doesn't have a USDC Associated Token Account, the script creates one on the fly (rent is paid by the sender).

## Important Notes

- **Never commit your private key to a Git repository.** Consider adding `config.ini` to your `.gitignore`.
- The sender wallet must hold enough USDC for the airdrop plus a small amount of SOL to cover transaction fees and ATA rent.
- The script will exit with an error if `min_usdc_amount × number of holders > total_usdc_amount`. Adjust your configuration accordingly.