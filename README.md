# NFT Holder USDC Random Airdrop

Randomly airdrop USDC to Solana NFT holders, using a configurable Dirichlet distribution algorithm.

## How It Works

1. Fetches all holder addresses for a given NFT collection via a Cloudflare Worker API
2. Optionally applies an address mapping to redirect airdrops to alternate wallets
3. Splits the total USDC amount randomly across holders using a configurable Dirichlet distribution (each holder is guaranteed at least `min_usdc_amount`)
4. Sends USDC transfers one by one, automatically creating Associated Token Accounts (ATAs) for recipients who don't have one yet

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
address_mapping_file = address_mapping.json
distribution_alpha = 0.1
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
| `address_mapping_file` | Path to a JSON file that maps holder addresses to alternate recipient addresses (optional, leave empty to disable) | `address_mapping.json` |
| `distribution_alpha` | Dirichlet distribution alpha parameter controlling amount variance (default: `1.0`). `1.0` = classic cut-the-line algorithm (moderate randomness). `< 1.0` = higher variance (some holders get much more, most get less). `> 1.0` = lower variance (amounts cluster toward the mean). Recommended: `0.1`–`0.5` for exciting distributions with occasional big winners. | `0.3` |

## Distribution Algorithm

The script uses a **Dirichlet distribution** to randomly split the USDC pool among holders. The `distribution_alpha` parameter controls how "wild" the randomness is:

| `distribution_alpha` | Behavior | Max amount probability (102 holders, 8099.7 USDC, min 20) |
|---|---|---|
| `1.0` (default) | Classic "cut the line" — moderate randomness, similar to WeChat red envelopes | Max > 1000: ~0%, Max > 500: ~2.5% |
| `0.5` | Moderate high variance — some lucky holders get noticeably more | Max > 1000: ~0.3%, Max > 500: ~36% |
| `0.3` | High variance — occasional big winners emerge | Max > 1000: ~5%, Max > 2000: ~0.01% |
| `0.2` | Very high variance — exciting distribution with clear winners | Max > 1000: ~20%, Max > 2000: ~0.1% |
| `0.1` | Extreme variance — a few holders win big, most get near minimum | Max > 1000: ~69%, Max > 2000: ~6% |

**How it works:** Each holder is first guaranteed their minimum (`min_usdc_amount`). The remaining pool is then split using Dirichlet(alpha) — a probability distribution over the simplex. When alpha < 1, the distribution concentrates mass on a few holders, creating "big winners". When alpha = 1, it's equivalent to the uniform cut-the-line algorithm. When alpha > 1, amounts converge toward equal splits.

The total distributed never exceeds `total_usdc_amount` regardless of the alpha value.

## Address Mapping

The address mapping feature allows you to redirect airdrops from specific NFT holder addresses to different recipient wallets. This is useful when holders want to receive USDC at a different address than the one holding the NFT.

Create a JSON file (e.g. `address_mapping.json`):

```json
{
    "OriginalHolderAddress1": "ActualRecipientAddress1",
    "OriginalHolderAddress2": "ActualRecipientAddress2"
}
```

Set `address_mapping_file = address_mapping.json` in `config.ini` to enable it. When active, the script will:

- Validate that all addresses (both source and destination) are valid Solana public keys
- Skip mappings where source and destination are identical
- Warn if multiple holders map to the same recipient address
- Log all applied mappings for transparency

Holders without a mapping entry will receive the airdrop at their original address as usual.

## Usage

### 1. Test Run (Dry Run)

Make sure `dry_run = true` in `config.ini`. The script will fetch holder addresses and print the randomized distribution plan without submitting any on-chain transactions:

```bash
python nft_airdrop.py
```

Sample output:

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

### 2. Test Address Mapping

Before running the full airdrop, you can verify that address mappings are working correctly by sending a small fixed amount of USDC to each mapped address:

```bash
python nft_airdrop.py --test mapping 0.01
```

This will:

- Load the address mapping file
- Fetch current NFT holders
- Find holders that have a mapping entry
- Send the specified amount (e.g. 0.01 USDC) to each mapped destination address
- Ignore the `dry_run` setting (always executes real transactions)
- Warn about any mapped addresses that are not current NFT holders

This is useful for confirming that mapped wallets can receive USDC before committing to the full airdrop.

### 3. Go Live

Once you've reviewed the distribution plan and everything looks good, set `dry_run` to `false` and run again:

```bash
python nft_airdrop.py
```

## Safety Measures

- **Hard cap on total amount**: After generating random amounts, the script asserts the sum never exceeds `total_usdc_amount`. A second check runs before each individual transfer to verify the cumulative total stays within bounds.
- **Automatic retries**: Transactions that fail with `Blockhash not found` are retried after a 2-second wait. `429 Too Many Requests` errors trigger incremental backoff (3s, 6s, 9s, ...). Both are capped at `tx_max_retries`.
- **Automatic ATA creation**: If a recipient doesn't have a USDC Associated Token Account, the script creates one on the fly (rent is paid by the sender).
- **Address mapping validation**: All addresses in the mapping file are validated as legitimate Solana public keys. Duplicate destination warnings and non-holder mapping warnings help catch configuration errors early.
- **Test amount guard**: The `--test` mode rejects amounts exceeding 1,000,000 USDC as a sanity check.

## Important Notes

- **Never commit your private key to a Git repository.** Consider adding `config.ini` to your `.gitignore`.
- The sender wallet must hold enough USDC for the airdrop plus a small amount of SOL to cover transaction fees and ATA rent.
- When using address mapping, it is recommended to run `--test mapping <small_amount>` first to verify the mapped addresses can receive USDC.