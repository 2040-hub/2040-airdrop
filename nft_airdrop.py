#!/usr/bin/env python3
"""
Random USDC Airdrop to NFT Holders on Solana
- Fetches NFT holder addresses from a Cloudflare Worker API
- Randomly distributes USDC (like random red envelopes) to holders
- Supports dry_run mode for testing
"""

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


def generate_random_amounts(total: float, n: int, min_amount: float) -> List[float]:
    """
    Generate n random amounts that sum to total, each >= min_amount.
    Uses the 'cut the line' algorithm (similar to WeChat random red envelopes).
    """
    if n <= 0:
        return []
    if min_amount * n > total:
        raise ValueError(
            f"Cannot distribute {total} USDC to {n} holders with min {min_amount} each. "
            f"Need at least {min_amount * n} USDC."
        )

    # Remaining pool after guaranteeing minimums
    remaining = total - min_amount * n

    # Generate n-1 random cut points in [0, remaining]
    cuts = sorted(random.uniform(0, remaining) for _ in range(n - 1))
    cuts = [0.0] + cuts + [remaining]

    amounts = []
    for i in range(n):
        amt = min_amount + (cuts[i + 1] - cuts[i])
        # Round to 6 decimals (USDC precision)
        amt = round(amt, USDC_DECIMALS)
        amounts.append(amt)

    # Fix any floating point drift so total is exact
    diff = round(total - sum(amounts), USDC_DECIMALS)
    if diff != 0:
        amounts[-1] = round(amounts[-1] + diff, USDC_DECIMALS)

    # Shuffle so the last person isn't always the adjustment target
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


def main():
    config = load_config()

    logger.info("=" * 60)
    logger.info("NFT Holder USDC Random Airdrop")
    logger.info("=" * 60)
    logger.info(f"Collection ID : {config['nft_collection_id']}")
    logger.info(f"Total USDC    : {config['total_usdc_amount']}")
    logger.info(f"Min per holder: {config['min_usdc_amount']}")
    logger.info(f"Dry run       : {config['dry_run']}")
    logger.info(f"Sleep time    : {config['tx_sleep_time']}s")
    logger.info(f"Max retries   : {config['tx_max_retries']}")

    # 1. Fetch holders
    holders = fetch_holders(config["nft_worker_url"], config["nft_collection_id"])
    if not holders:
        logger.error("No holders found. Exiting.")
        sys.exit(1)

    # 2. Generate random amounts
    try:
        amounts = generate_random_amounts(
            total=config["total_usdc_amount"],
            n=len(holders),
            min_amount=config["min_usdc_amount"],
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

    # Pair holders with amounts
    plan: List[Tuple[str, float]] = list(zip(holders, amounts))

    for i, (addr, amt) in enumerate(plan):
        logger.info(f"  [{i+1:>3}/{len(plan)}] {addr} -> {amt:.6f} USDC")

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

    for i, (addr, amt) in enumerate(plan):
        # Double-check: do not exceed total
        if round(sent_total + amt, USDC_DECIMALS) > config["total_usdc_amount"]:
            logger.warning(
                f"Skipping {addr}: would exceed total "
                f"({sent_total + amt:.6f} > {config['total_usdc_amount']})"
            )
            fail_count += 1
            continue

        recipient = Pubkey.from_string(addr)
        logger.info(f"[{i+1:>3}/{len(plan)}] Sending {amt:.6f} USDC to {addr} ...")

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
    main()