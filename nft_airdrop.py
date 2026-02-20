#!/usr/bin/env python3
"""
Random USDC Airdrop to NFT Holders on Solana
- Fetches NFT holder addresses from a Cloudflare Worker API
- Randomly distributes USDC (like random red envelopes) to holders
- Supports dry_run mode for testing
"""

import argparse
import configparser
import json
import random
import sys
import time
import logging
from typing import List, Tuple

import requests
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.instruction import Instruction, AccountMeta
from solders.hash import Hash
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
import base58

# Solana USDC Mint (mainnet)
USDC_MINT = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

# USDC has 6 decimals
USDC_DECIMALS = 6

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_config(path: str = "config.ini") -> dict:
    """Load configuration from ini file."""
    cfg = configparser.ConfigParser()
    cfg.read(path)
    s = cfg["settings"]
    return {
        "nft_worker_url": s.get("nft_worker_url"),
        "nft_collection_id": s.get("nft_collection_id"),
        "dry_run": s.get("dry_run", "true").lower() == "true",
        "private_key": s.get("private_key"),
        "rpc_url": s.get("rpc_url"),
        "total_usdc_amount": float(s.get("total_usdc_amount")),
        "min_usdc_amount": float(s.get("min_usdc_amount")),
        "tx_sleep_time": float(s.get("tx_sleep_time", "1")),
        "tx_max_retries": int(s.get("tx_max_retries", "5")),
        "address_mapping_file": s.get("address_mapping_file", ""),
        # 随机分配的 Dirichlet 分布 alpha 参数，控制金额分散程度：
        # 1.0 = 原始切割线段算法（温和随机），<1 方差更大（少数人拿大额），>1 趋于均分
        # 推荐值：0.1~0.5 产生更大方差，让部分持有者有机会拿到大额
        "distribution_alpha": float(s.get("distribution_alpha", "1.0")),
    }


def fetch_holders(worker_url: str, collection_id: str) -> List[str]:
    """Fetch NFT holder addresses from the worker API."""
    logger.info(f"Fetching holders for collection: {collection_id}")
    resp = requests.post(worker_url, json={"collectionId": collection_id}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"API returned success=false: {data}")
    holders = data.get("holders", [])
    logger.info(f"Got {len(holders)} unique holders")
    return holders


def load_address_mapping(file_path):
    # type: (str) -> dict
    """
    Load address mapping from a JSON file.

    Expected JSON format:
    {
        "original_address_1": "mapped_address_1",
        "original_address_2": "mapped_address_2"
    }

    Returns a dict of {original_address: mapped_address}.
    Validates that all addresses are valid Solana public keys.
    """
    if not file_path or not file_path.strip():
        return {}

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            mapping = json.load(f)
    except FileNotFoundError:
        logger.error("Address mapping file not found: %s", file_path)
        raise
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in address mapping file %s: %s", file_path, e)
        raise

    if not isinstance(mapping, dict):
        raise ValueError(
            "Address mapping file must contain a JSON object (dict), got %s"
            % type(mapping).__name__
        )

    validated = {}
    for orig, dest in mapping.items():
        orig_str = str(orig).strip()
        dest_str = str(dest).strip()

        if not orig_str or not dest_str:
            raise ValueError("Address mapping contains empty address: '%s' -> '%s'" % (orig, dest))

        if orig_str == dest_str:
            logger.warning("Address mapping has identical source and destination, skipping: %s", orig_str)
            continue

        # Validate both addresses are valid Solana public keys
        try:
            Pubkey.from_string(orig_str)
        except Exception:
            raise ValueError("Invalid source address in mapping: %s" % orig_str)

        try:
            Pubkey.from_string(dest_str)
        except Exception:
            raise ValueError("Invalid destination address in mapping: %s" % dest_str)

        validated[orig_str] = dest_str

    logger.info("Loaded %d address mapping(s) from %s", len(validated), file_path)
    for orig, dest in validated.items():
        logger.info("  Mapping: %s -> %s", orig, dest)

    return validated


def apply_address_mapping(holders, address_mapping):
    # type: (List[str], dict) -> List[Tuple[str, str]]
    """
    Apply address mapping to holders list.

    Returns a list of (original_holder_address, actual_recipient_address) tuples.
    If a holder has a mapping, the actual_recipient is the mapped address;
    otherwise the actual_recipient is the holder's own address.

    Also checks that mapped destinations don't collide with existing holders
    or other mappings, logging warnings if so.
    """
    if not address_mapping:
        return [(h, h) for h in holders]

    # Track how many holders map to the same destination for dedup warnings
    dest_sources = {}  # type: dict
    result = []
    mapped_count = 0

    for holder in holders:
        if holder in address_mapping:
            actual_recipient = address_mapping[holder]
            mapped_count += 1
            logger.info(
                "  Holder %s mapped to %s",
                holder,
                actual_recipient,
            )
        else:
            actual_recipient = holder

        # Track duplicate destinations
        if actual_recipient not in dest_sources:
            dest_sources[actual_recipient] = []
        dest_sources[actual_recipient].append(holder)

        result.append((holder, actual_recipient))

    # Warn about duplicate destinations (multiple holders mapping to same address)
    for dest, sources in dest_sources.items():
        if len(sources) > 1:
            logger.warning(
                "Multiple holders map to the same recipient %s: %s",
                dest,
                sources,
            )

    logger.info(
        "Address mapping applied: %d of %d holders remapped",
        mapped_count,
        len(holders),
    )

    return result


def _random_split(pool, n, alpha=1.0):
    # type: (float, int, float) -> List[float]
    """
    Split 'pool' into n non-negative random parts that sum to pool.

    When alpha == 1.0, uses the classic 'cut the line' algorithm (equivalent
    to Dirichlet with alpha=1, i.e. uniform simplex sampling).

    When alpha != 1.0, uses Dirichlet distribution via Gamma variates:
      - alpha < 1: high variance (some get a lot, most get little)
      - alpha > 1: low variance (amounts cluster around pool/n)

    Returns a list of n floats summing to pool, each rounded to USDC_DECIMALS.
    """
    if n <= 0:
        return []
    if n == 1:
        return [round(pool, USDC_DECIMALS)]
    if pool <= 0:
        return [0.0] * n

    if alpha == 1.0:
        # Classic cut-the-line: equivalent to Dirichlet(1,1,...,1)
        cuts = sorted(random.uniform(0, pool) for _ in range(n - 1))
        cuts = [0.0] + cuts + [pool]
        parts = [round(cuts[i + 1] - cuts[i], USDC_DECIMALS) for i in range(n)]
    else:
        # Dirichlet distribution via Gamma variates
        # alpha 必须 > 0；clamp 到一个安全下限避免数值问题
        safe_alpha = max(alpha, 0.001)
        gammas = []
        for _ in range(n):
            g = random.gammavariate(safe_alpha, 1.0)
            gammas.append(g)
        total_g = sum(gammas)
        if total_g == 0:
            # 极端情况兜底：均分
            parts = [round(pool / n, USDC_DECIMALS)] * n
        else:
            parts = [round(pool * (g / total_g), USDC_DECIMALS) for g in gammas]

    # 修正浮点漂移，保证精确求和
    drift = round(pool - sum(parts), USDC_DECIMALS)
    if drift != 0:
        # 把漂移加到最大的那份上（影响最小）
        max_idx = parts.index(max(parts))
        parts[max_idx] = round(parts[max_idx] + drift, USDC_DECIMALS)

    return parts


def generate_random_amounts(
    total,          # type: float
    n,              # type: int
    min_amount,     # type: float
    alpha=1.0,      # type: float
):
    # type: (...) -> List[float]
    """
    Generate n random amounts that sum to total, each >= min_amount.
    Uses the Dirichlet distribution to split the remaining pool after
    guaranteeing each holder's minimum.

    alpha controls the variance of the distribution:
      - alpha = 1.0: classic 'cut the line' / uniform simplex (original behavior)
      - alpha < 1.0: higher variance (some holders get much more, most get less)
      - alpha > 1.0: lower variance (amounts cluster toward the mean)
    """
    if n <= 0:
        return []

    min_needed = round(min_amount * n, USDC_DECIMALS)
    if min_needed > total:
        raise ValueError(
            "Cannot distribute {total} USDC to {n} holders "
            "({n} x {min_amount} = {min_needed} USDC needed).".format(
                total=total, n=n, min_amount=min_amount, min_needed=min_needed,
            )
        )

    remaining = total - min_needed

    # Split remaining pool using Dirichlet distribution
    parts = _random_split(remaining, n, alpha=alpha)

    amounts = []
    for i in range(n):
        amt = round(min_amount + parts[i], USDC_DECIMALS)
        amounts.append(amt)

    # Fix any floating point drift so total is exact
    diff = round(total - sum(amounts), USDC_DECIMALS)
    if diff != 0:
        amounts[-1] = round(amounts[-1] + diff, USDC_DECIMALS)

    random.shuffle(amounts)
    return amounts


def get_associated_token_address(owner: Pubkey, mint: Pubkey) -> Pubkey:
    """Derive the associated token account address."""
    seeds = [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)]
    ata, _ = Pubkey.find_program_address(seeds, ASSOCIATED_TOKEN_PROGRAM_ID)
    return ata


def build_transfer_ix(
    source_ata: Pubkey,
    dest_ata: Pubkey,
    owner: Pubkey,
    amount_lamports: int,
) -> Instruction:
    """Build an SPL Token transfer instruction."""
    # SPL Token Transfer instruction index = 3
    data = b"\x03" + amount_lamports.to_bytes(8, "little")
    accounts = [
        AccountMeta(pubkey=source_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=dest_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=owner, is_signer=True, is_writable=False),
    ]
    return Instruction(TOKEN_PROGRAM_ID, data, accounts)


def build_create_ata_ix(
    payer: Pubkey,
    owner: Pubkey,
    mint: Pubkey,
) -> Instruction:
    """Build a CreateAssociatedTokenAccount instruction."""
    ata = get_associated_token_address(owner, mint)
    accounts = [
        AccountMeta(pubkey=payer, is_signer=True, is_writable=True),
        AccountMeta(pubkey=ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=owner, is_signer=False, is_writable=False),
        AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
        AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
    ]
    return Instruction(ASSOCIATED_TOKEN_PROGRAM_ID, bytes(), accounts)


def check_ata_exists(client: Client, ata: Pubkey) -> bool:
    """Check if an associated token account exists on-chain."""
    resp = client.get_account_info(ata, commitment=Confirmed)
    return resp.value is not None


def send_usdc(
    client: Client,
    payer: Keypair,
    recipient: Pubkey,
    amount: float,
    max_retries: int = 5,
) -> str:
    """Send USDC to a recipient. Creates ATA if needed. Retries on BlockhashNotFound. Returns tx signature."""
    amount_lamports = int(amount * (10 ** USDC_DECIMALS))
    source_ata = get_associated_token_address(payer.pubkey(), USDC_MINT)
    dest_ata = get_associated_token_address(recipient, USDC_MINT)

    ixs = []

    # Create destination ATA if it doesn't exist
    if not check_ata_exists(client, dest_ata):
        logger.info(f"  Creating ATA for {recipient}")
        ixs.append(build_create_ata_ix(payer.pubkey(), recipient, USDC_MINT))

    ixs.append(build_transfer_ix(source_ata, dest_ata, payer.pubkey(), amount_lamports))

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            # Fetch fresh blockhash on every attempt
            recent_blockhash_resp = client.get_latest_blockhash(commitment=Confirmed)
            recent_blockhash = recent_blockhash_resp.value.blockhash

            msg = MessageV0.try_compile(
                payer=payer.pubkey(),
                instructions=ixs,
                address_lookup_table_accounts=[],
                recent_blockhash=recent_blockhash,
            )
            tx = VersionedTransaction(msg, [payer])

            resp = client.send_transaction(tx, opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed))
            sig = str(resp.value)
            return sig
        except Exception as e:
            last_error = e
            err_str = str(e)
            if "Blockhash not found" in err_str or "BlockhashNotFound" in err_str:
                logger.warning(f"  ⚠️  Blockhash not found (attempt {attempt}/{max_retries}), retrying in 2s...")
                time.sleep(2)
                continue
            elif "429" in err_str or "Too Many Requests" in err_str or "too many requests" in err_str.lower():
                wait = 3 * attempt  # exponential backoff: 3s, 6s, 9s...
                logger.warning(f"  ⚠️  Rate limited 429 (attempt {attempt}/{max_retries}), retrying in {wait}s...")
                time.sleep(wait)
                continue
            else:
                # Non-retryable error, raise immediately
                raise

    raise last_error


def parse_args():
    # type: () -> argparse.Namespace
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="NFT Holder USDC Random Airdrop on Solana",
    )
    parser.add_argument(
        "--test",
        nargs=2,
        metavar=("MODE", "AMOUNT"),
        help=(
            "Test mode. Currently supports: --test mapping <usdc_amount>. "
            "Sends <usdc_amount> USDC to each mapped address (ignores dry_run)."
        ),
    )
    return parser.parse_args()


def run_test_mapping(test_amount_str):
    # type: (str) -> None
    """
    Test mode: send a fixed USDC amount to each address that has a mapping.

    Workflow:
    1. Load config and address mapping
    2. Fetch NFT holders
    3. Find holders that exist in address_mapping
    4. Send test_amount USDC to each mapped destination
    5. Ignores dry_run setting
    """
    # Validate amount
    try:
        test_amount = float(test_amount_str)
    except ValueError:
        logger.error("Invalid test amount: %s (must be a number)", test_amount_str)
        sys.exit(1)

    if test_amount <= 0:
        logger.error("Test amount must be positive, got: %s", test_amount)
        sys.exit(1)

    if test_amount > 1000000:
        logger.error("Test amount seems unreasonably large: %s USDC", test_amount)
        sys.exit(1)

    config = load_config()

    logger.info("=" * 60)
    logger.info("TEST MODE: Address Mapping Verification")
    logger.info("=" * 60)
    logger.info(f"Test amount   : {test_amount} USDC per mapped holder")
    logger.info(f"Collection ID : {config['nft_collection_id']}")
    logger.info(f"Addr mapping  : {config['address_mapping_file'] or '(none)'}")
    logger.info(f"dry_run is IGNORED in test mode")

    # Load address mapping (required for this test)
    if not config["address_mapping_file"]:
        logger.error("address_mapping_file is not configured. Cannot run --test mapping.")
        sys.exit(1)

    try:
        address_mapping = load_address_mapping(config["address_mapping_file"])
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as e:
        logger.error("Failed to load address mapping: %s", e)
        sys.exit(1)

    if not address_mapping:
        logger.error("Address mapping is empty. Nothing to test.")
        sys.exit(1)

    # Fetch holders
    holders = fetch_holders(config["nft_worker_url"], config["nft_collection_id"])
    if not holders:
        logger.error("No holders found. Exiting.")
        sys.exit(1)

    # Find holders that have a mapping
    holders_set = set(holders)
    mapped_pairs = []  # type: List[Tuple[str, str]]
    unmapped_keys = []  # type: List[str]

    for orig, dest in address_mapping.items():
        if orig in holders_set:
            mapped_pairs.append((orig, dest))
        else:
            unmapped_keys.append(orig)

    if unmapped_keys:
        logger.warning(
            "The following mapped addresses are NOT current NFT holders (skipped): %s",
            unmapped_keys,
        )

    if not mapped_pairs:
        logger.error(
            "No address mapping entries match current NFT holders. Nothing to send."
        )
        sys.exit(1)

    total_test_usdc = round(test_amount * len(mapped_pairs), USDC_DECIMALS)
    logger.info(f"\nTest plan: {len(mapped_pairs)} mapped holder(s), "
                f"{test_amount} USDC each, total = {total_test_usdc} USDC")

    for i, (orig, dest) in enumerate(mapped_pairs):
        logger.info(f"  [{i+1:>3}/{len(mapped_pairs)}] {orig} -> {dest} : {test_amount:.6f} USDC")

    # Initialize Solana client and keypair
    client = Client(config["rpc_url"])
    try:
        secret_key = base58.b58decode(config["private_key"])
        payer = Keypair.from_bytes(secret_key)
    except Exception as e:
        logger.error(f"Failed to load private key: {e}")
        sys.exit(1)

    logger.info(f"\nSender address: {payer.pubkey()}")

    # Execute test transfers
    sent_total = 0.0
    success_count = 0
    fail_count = 0

    for i, (orig, dest) in enumerate(mapped_pairs):
        recipient = Pubkey.from_string(dest)
        logger.info(
            f"[{i+1:>3}/{len(mapped_pairs)}] Sending {test_amount:.6f} USDC "
            f"to {dest} (on behalf of holder {orig}) ..."
        )

        try:
            sig = send_usdc(client, payer, recipient, test_amount, max_retries=config["tx_max_retries"])
            sent_total = round(sent_total + test_amount, USDC_DECIMALS)
            success_count += 1
            logger.info(f"  ✅ TX: {sig}  (cumulative: {sent_total:.6f} USDC)")
        except Exception as e:
            fail_count += 1
            logger.error(f"  ❌ Failed: {e}")

        if i < len(mapped_pairs) - 1:
            time.sleep(config["tx_sleep_time"])

    logger.info("\n" + "=" * 60)
    logger.info("Test mapping airdrop complete!")
    logger.info(f"  Success: {success_count}")
    logger.info(f"  Failed : {fail_count}")
    logger.info(f"  Total sent: {sent_total:.6f} / {total_test_usdc} USDC")
    logger.info("=" * 60)


def main():
    config = load_config()

    # Validate distribution_alpha
    if config["distribution_alpha"] <= 0:
        logger.error("distribution_alpha must be > 0, got: %s", config["distribution_alpha"])
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("NFT Holder USDC Random Airdrop")
    logger.info("=" * 60)
    logger.info(f"Collection ID : {config['nft_collection_id']}")
    logger.info(f"Total USDC    : {config['total_usdc_amount']}")
    logger.info(f"Min per holder: {config['min_usdc_amount']}")
    logger.info(f"Dry run       : {config['dry_run']}")
    logger.info(f"Sleep time    : {config['tx_sleep_time']}s")
    logger.info(f"Max retries   : {config['tx_max_retries']}")
    logger.info(f"Addr mapping  : {config['address_mapping_file'] or '(none)'}")
    if config["distribution_alpha"] != 1.0:
        logger.info(f"Distr. alpha  : {config['distribution_alpha']} (Dirichlet, {'high' if config['distribution_alpha'] < 1 else 'low'} variance)")
    else:
        logger.info(f"Distr. alpha  : 1.0 (classic cut-the-line)")

    # 1. Fetch holders
    holders = fetch_holders(config["nft_worker_url"], config["nft_collection_id"])
    if not holders:
        logger.error("No holders found. Exiting.")
        sys.exit(1)

    # 1.5. Load and apply address mapping
    try:
        address_mapping = load_address_mapping(config["address_mapping_file"])
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as e:
        logger.error("Failed to load address mapping: %s", e)
        sys.exit(1)

    holder_recipient_pairs = apply_address_mapping(holders, address_mapping)

    # 2. Generate random amounts
    try:
        amounts = generate_random_amounts(
            total=config["total_usdc_amount"],
            n=len(holders),
            min_amount=config["min_usdc_amount"],
            alpha=config["distribution_alpha"],
        )
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)

    # Safety check: total must not exceed configured amount
    actual_total = round(sum(amounts), USDC_DECIMALS)
    assert actual_total <= config["total_usdc_amount"], (
        f"BUG: total {actual_total} exceeds configured {config['total_usdc_amount']}"
    )

    logger.info(f"\nDistribution plan ({len(holders)} holders, total={actual_total} USDC):")
    logger.info(f"  Min amount: {min(amounts):.6f} USDC")
    logger.info(f"  Max amount: {max(amounts):.6f} USDC")
    logger.info(f"  Avg amount: {actual_total / len(amounts):.6f} USDC")

    # Pair holders with amounts: (original_holder, actual_recipient, amount)
    plan = []  # type: List[Tuple[str, str, float]]
    for (orig, recipient), amt in zip(holder_recipient_pairs, amounts):
        plan.append((orig, recipient, amt))

    for i, (orig, recipient, amt) in enumerate(plan):
        if orig != recipient:
            logger.info(f"  [{i+1:>3}/{len(plan)}] {orig} (mapped -> {recipient}) -> {amt:.6f} USDC")
        else:
            logger.info(f"  [{i+1:>3}/{len(plan)}] {orig} -> {amt:.6f} USDC")

    if config["dry_run"]:
        logger.info("\n[DRY RUN] No transactions will be sent.")
        logger.info("Done.")
        return

    # 3. Initialize Solana client and keypair
    client = Client(config["rpc_url"])
    try:
        secret_key = base58.b58decode(config["private_key"])
        payer = Keypair.from_bytes(secret_key)
    except Exception as e:
        logger.error(f"Failed to load private key: {e}")
        sys.exit(1)

    logger.info(f"\nSender address: {payer.pubkey()}")

    # 4. Execute transfers
    sent_total = 0.0
    success_count = 0
    fail_count = 0

    for i, (orig, recipient_addr, amt) in enumerate(plan):
        # Double-check: do not exceed total
        if round(sent_total + amt, USDC_DECIMALS) > config["total_usdc_amount"]:
            logger.warning(
                f"Skipping {orig}: would exceed total "
                f"({sent_total + amt:.6f} > {config['total_usdc_amount']})"
            )
            fail_count += 1
            continue

        recipient = Pubkey.from_string(recipient_addr)
        if orig != recipient_addr:
            logger.info(
                f"[{i+1:>3}/{len(plan)}] Sending {amt:.6f} USDC to {recipient_addr} "
                f"(on behalf of holder {orig}) ..."
            )
        else:
            logger.info(f"[{i+1:>3}/{len(plan)}] Sending {amt:.6f} USDC to {recipient_addr} ...")

        try:
            sig = send_usdc(client, payer, recipient, amt, max_retries=config["tx_max_retries"])
            sent_total = round(sent_total + amt, USDC_DECIMALS)
            success_count += 1
            logger.info(f"  ✅ TX: {sig}  (cumulative: {sent_total:.6f} USDC)")
        except Exception as e:
            fail_count += 1
            logger.error(f"  ❌ Failed: {e}")

        if i < len(plan) - 1:
            time.sleep(config["tx_sleep_time"])

    logger.info("\n" + "=" * 60)
    logger.info("Airdrop complete!")
    logger.info(f"  Success: {success_count}")
    logger.info(f"  Failed : {fail_count}")
    logger.info(f"  Total sent: {sent_total:.6f} / {config['total_usdc_amount']} USDC")
    logger.info("=" * 60)


if __name__ == "__main__":
    args = parse_args()
    if args.test:
        test_mode, test_amount_str = args.test
        if test_mode.lower() != "mapping":
            logger.error("Unknown test mode: '%s'. Supported: mapping", test_mode)
            sys.exit(1)
        run_test_mapping(test_amount_str)
    else:
        main()