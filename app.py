import json
import logging
import threading
import time
from pathlib import Path

import requests
from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    redirect,
    url_for,
    flash,
)
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from config import (
    APECHAIN_CHAIN_ID,
    APECHAIN_RPC_URL,
    APECHAIN_EXPLORER,
    ETHERSCAN_API_V2_URL,
    APESCAN_API_KEY,
    PRIVATE_KEY,
    AUTO_STAKE_INTERVAL,
    NFT_SCAN_MAX_RANGE,
    STAKING_VAULT_ABI,
    ERC721_ABI,
    COLLECTIONS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

w3 = Web3(Web3.HTTPProvider(APECHAIN_RPC_URL))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

app = Flask(__name__)
app.secret_key = "anvil-staking-tool-change-me"

COLLECTIONS_STORE = list(COLLECTIONS)
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
STAKED_DB = DATA_DIR / "staked_tokens.json"
TOKEN_CACHE_DIR = DATA_DIR / "token_cache"
TOKEN_CACHE_DIR.mkdir(exist_ok=True)


def _get_pk():
    pk = PRIVATE_KEY
    if pk and not pk.startswith("0x"):
        pk = "0x" + pk
    return pk


def get_account():
    pk = _get_pk()
    if not pk:
        return None
    try:
        return w3.eth.account.from_key(pk)
    except Exception:
        logger.warning("Invalid PRIVATE_KEY configured")
        return None


account = get_account()


# ── Local staked-token DB ───────────────────────────────────────


def load_staked_db():
    if STAKED_DB.exists():
        with open(STAKED_DB) as f:
            return json.load(f)
    return {}


def save_staked_db(db):
    with open(STAKED_DB, "w") as f:
        json.dump(db, f, indent=2)


def record_staked(staking_address, wallet_address, token_id):
    db = load_staked_db()
    key = f"{staking_address}_{wallet_address}".lower()
    db.setdefault(key, [])
    if token_id not in db[key]:
        db[key].append(token_id)
    save_staked_db(db)


def record_unstaked(staking_address, wallet_address, token_id):
    db = load_staked_db()
    key = f"{staking_address}_{wallet_address}".lower()
    if key in db and token_id in db[key]:
        db[key].remove(token_id)
    save_staked_db(db)


def get_recorded_staked(staking_address, wallet_address):
    db = load_staked_db()
    key = f"{staking_address}_{wallet_address}".lower()
    return db.get(key, [])


# ── Token cache ─────────────────────────────────────────────────


def _cache_path(nft_address):
    return TOKEN_CACHE_DIR / f"{nft_address.lower()}.json"


def load_token_cache(nft_address, wallet_address):
    p = _cache_path(nft_address)
    if p.exists():
        data = json.load(open(p))
        if data.get("wallet", "").lower() == wallet_address.lower():
            age = time.time() - data.get("timestamp", 0)
            if age < 300:
                return data
    return None


def save_token_cache(nft_address, wallet_address, wallet_tokens, staked_tokens):
    p = _cache_path(nft_address)
    data = {
        "wallet": wallet_address,
        "wallet_tokens": wallet_tokens,
        "staked_tokens": staked_tokens,
        "timestamp": time.time(),
    }
    with open(p, "w") as f:
        json.dump(data, f)


# ── Contract helpers ─────────────────────────────────────────────


def get_staking_contract(staking_address):
    return w3.eth.contract(
        address=Web3.to_checksum_address(staking_address),
        abi=STAKING_VAULT_ABI,
    )


def get_nft_contract(nft_address):
    return w3.eth.contract(
        address=Web3.to_checksum_address(nft_address),
        abi=ERC721_ABI,
    )


def send_tx(build_fn):
    pk = _get_pk()
    if not pk:
        raise ValueError("No private key configured")
    acct = w3.eth.account.from_key(pk)
    tx = build_fn(acct.address)
    tx["from"] = acct.address
    tx["chainId"] = APECHAIN_CHAIN_ID
    tx["nonce"] = w3.eth.get_transaction_count(acct.address)
    try:
        tx["gas"] = w3.eth.estimate_gas(tx)
    except Exception:
        tx["gas"] = 300_000
    tx["maxFeePerGas"] = w3.eth.gas_price * 2
    tx["maxPriorityFeePerGas"] = w3.to_wei(0.02, "gwei")
    tx["type"] = 2
    tx.pop("gasPrice", None)
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    return {
        "tx_hash": tx_hash.hex(),
        "status": "success" if receipt.status == 1 else "failed",
        "gas_used": receipt.gasUsed,
        "block_number": receipt.blockNumber,
        "explorer_url": f"{APECHAIN_EXPLORER}/tx/{tx_hash.hex()}",
    }


# ── Token Discovery ──────────────────────────────────────────────


def fetch_tokens_from_apescan(nft_address, wallet_address):
    token_ids = set()
    page = 1
    while True:
        params = {
            "chainid": str(APECHAIN_CHAIN_ID),
            "module": "account",
            "action": "tokennfttx",
            "address": wallet_address,
            "contractaddress": nft_address,
            "startblock": "0",
            "endblock": "99999999",
            "sort": "asc",
            "page": str(page),
        }
        if APESCAN_API_KEY:
            params["apikey"] = APESCAN_API_KEY
        try:
            resp = requests.get(ETHERSCAN_API_V2_URL, params=params, timeout=30)
            data = resp.json()
            if data.get("status") != "1":
                msg = data.get("message", "")
                result = data.get("result", "")
                if isinstance(result, str) and ("api key" in result.lower() or "rate" in result.lower()):
                    logger.warning(f"Etherscan API v2: {msg} - {result}")
                break
            results = data.get("result", [])
            if not isinstance(results, list) or len(results) == 0:
                break
            for tx in results:
                tid = tx.get("tokenID") or tx.get("tokenId")
                if tid is not None:
                    try:
                        token_ids.add(int(tid))
                    except (ValueError, TypeError):
                        pass
            if len(results) < 10000:
                break
            page += 1
        except Exception as e:
            logger.error(f"Etherscan API v2 error: {e}")
            break
    return sorted(token_ids)


def get_collection_size(nft_address):
    nft = get_nft_contract(nft_address)
    try:
        return nft.functions.collectionSize().call()
    except Exception:
        pass
    try:
        return nft.functions.totalSupply().call()
    except Exception:
        pass
    return None


def find_owned_tokens(nft_address, wallet_address, candidate_ids=None):
    nft = get_nft_contract(nft_address)
    wallet = Web3.to_checksum_address(wallet_address)
    owned = []
    not_exist_count = 0
    ids_to_check = candidate_ids if candidate_ids is not None else range(NFT_SCAN_MAX_RANGE)
    if candidate_ids is None:
        cs = get_collection_size(nft_address)
        if cs:
            ids_to_check = range(cs)
    for tid in ids_to_check:
        try:
            owner = nft.functions.ownerOf(tid).call()
            not_exist_count = 0
            if owner.lower() == wallet.lower():
                owned.append(tid)
        except Exception as e:
            err = str(e)
            if "nonexistent" in err.lower() or "overflow" in err.lower():
                not_exist_count += 1
                if candidate_ids is None and not_exist_count > 50:
                    break
                continue
            not_exist_count = 0
    return owned


def _resolve_tokens_onchain(nft_address, staking_address, wallet_address, candidate_ids):
    nft = get_nft_contract(nft_address)
    vault = get_staking_contract(staking_address)
    wallet = Web3.to_checksum_address(wallet_address)
    vault_addr = Web3.to_checksum_address(staking_address)
    wallet_tokens = []
    staked_tokens = []

    for tid in candidate_ids:
        try:
            owner = nft.functions.ownerOf(tid).call()
            if owner.lower() == wallet.lower():
                wallet_tokens.append(tid)
            elif owner.lower() == vault_addr.lower():
                try:
                    staker = vault.functions.stakes(tid).call()
                    if staker[0].lower() == wallet.lower():
                        staked_tokens.append(tid)
                except Exception:
                    pass
        except Exception:
            pass

    return wallet_tokens, staked_tokens


def discover_tokens(nft_address, staking_address, wallet_address, force=False):
    wallet = Web3.to_checksum_address(wallet_address)
    nft = get_nft_contract(nft_address)
    vault = get_staking_contract(staking_address)

    use_cache = not force
    cached = load_token_cache(nft_address, wallet_address) if use_cache else None

    if cached:
        candidate_ids = list(set(cached["wallet_tokens"] + cached["staked_tokens"]))
        wallet_tokens, staked_tokens = _resolve_tokens_onchain(
            nft_address, staking_address, wallet_address, candidate_ids
        )
    else:
        candidate_ids = fetch_tokens_from_apescan(nft_address, wallet_address)
        if candidate_ids:
            logger.info(f"API found {len(candidate_ids)} candidate IDs for {nft_address[:10]}")
            wallet_tokens, staked_tokens = _resolve_tokens_onchain(
                nft_address, staking_address, wallet_address, candidate_ids
            )
        else:
            logger.info(f"API empty, falling back to ownerOf scan for {nft_address[:10]}")
            wallet_tokens = find_owned_tokens(nft_address, wallet_address)
            vault_owned = find_owned_tokens(nft_address, staking_address)
            recorded = get_recorded_staked(staking_address, wallet_address)
            staked_tokens = []
            for tid in set(vault_owned + recorded):
                try:
                    staker = vault.functions.stakes(tid).call()
                    if staker[0].lower() == wallet.lower():
                        if tid not in staked_tokens:
                            staked_tokens.append(tid)
                except Exception:
                    pass
            wallet_tokens = [t for t in wallet_tokens if t not in staked_tokens]

    save_token_cache(nft_address, wallet_address, wallet_tokens, staked_tokens)

    staking_details = []
    for tid in staked_tokens:
        try:
            stake_info = vault.functions.stakes(tid).call()
            ts = stake_info[1]
            staking_details.append({
                "token_id": tid,
                "staked_at": ts,
                "staked_at_date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "Unknown",
            })
        except Exception:
            staking_details.append({"token_id": tid, "staked_at": 0, "staked_at_date": "Unknown"})

    try:
        name = nft.functions.name().call()
        symbol = nft.functions.symbol().call()
    except Exception:
        name = nft_address[:10] + "..."
        symbol = "NFT"

    balance = len(wallet_tokens) + len(staked_tokens)

    return {
        "name": name,
        "symbol": symbol,
        "balance": balance,
        "token_ids": wallet_tokens,
        "staked_ids": staked_tokens,
        "staking_details": staking_details,
    }


def get_staking_info(staking_address, wallet_address):
    vault = get_staking_contract(staking_address)
    wallet = Web3.to_checksum_address(wallet_address)
    nft_addr = vault.functions.collection().call()
    reward_token = vault.functions.token().call()
    total_staked = vault.functions.totalStaked().call()
    staked_count = vault.functions.stakedCount(wallet).call()
    cooldown = vault.functions.cooldownPeriod().call()
    rate = vault.functions.rewardRate().call()
    period_finish = vault.functions.periodFinish().call()
    try:
        pending = vault.functions.pendingRewards(wallet).call()
        pending_fmt = str(Web3.from_wei(pending, "ether"))
    except Exception:
        pending = 0
        pending_fmt = "0"
    return {
        "nft_address": nft_addr,
        "reward_token_address": reward_token,
        "total_staked": total_staked,
        "staked_count": staked_count,
        "cooldown_period": cooldown,
        "cooldown_period_days": round(cooldown / 86400, 2),
        "reward_rate": rate,
        "period_finish": period_finish,
        "rewards_active": period_finish > int(time.time()) if period_finish else False,
        "pending_rewards_wei": pending,
        "pending_rewards": pending_fmt,
    }


def check_approval(nft_address, staking_address, wallet_address):
    nft = get_nft_contract(nft_address)
    return nft.functions.isApprovedForAll(
        Web3.to_checksum_address(wallet_address),
        Web3.to_checksum_address(staking_address),
    ).call()


def invalidate_token_cache(nft_address):
    p = _cache_path(nft_address)
    if p.exists():
        p.unlink()


def load_collection_stats(col):
    if not account:
        return {**col, "error": "No wallet configured"}
    try:
        nft_info = discover_tokens(col["nft_address"], col["staking_address"], account.address)
        staking_info = get_staking_info(col["staking_address"], account.address)
        wallet_tokens = nft_info["token_ids"]
        staked_ids = nft_info["staked_ids"]
        staking_details = nft_info["staking_details"]
        is_approved = check_approval(col["nft_address"], col["staking_address"], account.address)
        return {
            **col,
            "nft_name": nft_info["name"],
            "nft_symbol": nft_info["symbol"],
            "wallet_balance": nft_info["balance"],
            "wallet_tokens": wallet_tokens,
            "staked_count": len(staked_ids),
            "staked_ids": staked_ids,
            "staking_details": staking_details,
            "is_approved": is_approved,
            "cooldown_period_days": staking_info["cooldown_period_days"],
            "pending_rewards": staking_info["pending_rewards"],
            "rewards_active": staking_info["rewards_active"],
            "total_staked": staking_info["total_staked"],
            "reward_token_address": staking_info["reward_token_address"],
        }
    except Exception as e:
        logger.error(f"Error loading collection {col.get('name', '?')}: {e}")
        return {**col, "error": str(e)}


# ── Template context ────────────────────────────────────────────


@app.context_processor
def inject_globals():
    return {
        "explorer": APECHAIN_EXPLORER,
        "chain_id": APECHAIN_CHAIN_ID,
        "wallet": account.address if account else None,
        "collections": COLLECTIONS_STORE,
        "connected": account is not None,
    }


# ── Pages ───────────────────────────────────────────────────────


@app.route("/")
def index():
    collection_stats = []
    if account:
        for col in COLLECTIONS_STORE:
            collection_stats.append(load_collection_stats(col))
    return render_template("index.html", collections_data=collection_stats)


@app.route("/collection/<int:idx>")
def collection_detail(idx):
    if idx < 0 or idx >= len(COLLECTIONS_STORE):
        flash("Collection not found", "error")
        return redirect(url_for("index"))
    col = COLLECTIONS_STORE[idx]
    detail = load_collection_stats(col) if account else {**col, "idx": idx, "error": "No wallet"}
    detail["idx"] = idx
    return render_template("collection.html", detail=detail)


@app.route("/collection/<int:idx>/refresh")
def refresh_collection(idx):
    if idx < 0 or idx >= len(COLLECTIONS_STORE):
        flash("Collection not found", "error")
        return redirect(url_for("index"))
    col = COLLECTIONS_STORE[idx]
    invalidate_token_cache(col["nft_address"])
    flash("Refreshing token data...", "success")
    return redirect(url_for("collection_detail", idx=idx))


# ── API Endpoints ────────────────────────────────────────────────


@app.route("/api/status")
def api_status():
    return jsonify({
        "connected": account is not None,
        "wallet": account.address if account else None,
        "chain_id": APECHAIN_CHAIN_ID,
        "rpc_connected": w3.is_connected(),
    })


@app.route("/api/approve/<int:idx>", methods=["POST"])
def api_approve(idx):
    if not account:
        return jsonify({"error": "No wallet configured"}), 400
    if idx < 0 or idx >= len(COLLECTIONS_STORE):
        return jsonify({"error": "Invalid collection"}), 400
    col = COLLECTIONS_STORE[idx]
    try:
        nft = get_nft_contract(col["nft_address"])
        spender = Web3.to_checksum_address(col["staking_address"])

        def build(from_addr):
            return nft.functions.setApprovalForAll(spender, True).build_transaction(
                {"from": from_addr}
            )

        return jsonify(send_tx(build))
    except Exception as e:
        logger.error(f"Approve error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/stake/<int:idx>/<int:token_id>", methods=["POST"])
def api_stake(idx, token_id):
    if not account:
        return jsonify({"error": "No wallet configured"}), 400
    if idx < 0 or idx >= len(COLLECTIONS_STORE):
        return jsonify({"error": "Invalid collection"}), 400
    col = COLLECTIONS_STORE[idx]
    try:
        nft = get_nft_contract(col["nft_address"])
        wallet = Web3.to_checksum_address(account.address)
        try:
            owner = nft.functions.ownerOf(token_id).call()
            if owner.lower() != wallet.lower():
                return jsonify({"error": f"Token #{token_id} not owned by wallet (owner: {owner})"}), 400
        except Exception as e:
            logger.warning(f"Could not verify owner of #{token_id}: {e}")

        vault = get_staking_contract(col["staking_address"])

        def build(from_addr):
            return vault.functions.stake(token_id).build_transaction({"from": from_addr})

        result = send_tx(build)
        if result["status"] == "success":
            record_staked(col["staking_address"], account.address, token_id)
            invalidate_token_cache(col["nft_address"])
        return jsonify(result)
    except Exception as e:
        logger.error(f"Stake error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/stake_all/<int:idx>", methods=["POST"])
def api_stake_all(idx):
    if not account:
        return jsonify({"error": "No wallet configured"}), 400
    if idx < 0 or idx >= len(COLLECTIONS_STORE):
        return jsonify({"error": "Invalid collection"}), 400
    col = COLLECTIONS_STORE[idx]
    try:
        data = request.get_json(silent=True) or {}
        token_ids = data.get("token_ids")
        if not token_ids:
            nft_info = discover_tokens(col["nft_address"], col["staking_address"], account.address)
            token_ids = nft_info["token_ids"]
        if not token_ids:
            return jsonify({"results": [{"action": "none", "message": "No unstaked NFTs in wallet"}]})

        token_ids = sorted(token_ids)
        logger.info(f"Stake all: {len(token_ids)} tokens to stake: {token_ids[:5]}...")

        results = []
        is_approved = check_approval(col["nft_address"], col["staking_address"], account.address)
        if not is_approved:
            logger.info(f"Approving {col['staking_address']}...")
            nft = get_nft_contract(col["nft_address"])
            spender = Web3.to_checksum_address(col["staking_address"])

            def build_approve(from_addr):
                return nft.functions.setApprovalForAll(spender, True).build_transaction(
                    {"from": from_addr}
                )

            r = send_tx(build_approve)
            results.append({"action": "approve", **r})
            if r["status"] != "success":
                return jsonify({"results": results})
            time.sleep(3)

        vault = get_staking_contract(col["staking_address"])
        for tid in token_ids:
            logger.info(f"Staking token #{tid} ({token_ids.index(tid)+1}/{len(token_ids)})...")

            def build_stake(from_addr, _tid=tid):
                return vault.functions.stake(_tid).build_transaction({"from": from_addr})

            try:
                r = send_tx(build_stake)
            except Exception as e:
                logger.error(f"Stake tx failed for #{tid}: {e}")
                results.append({"action": "stake", "token_id": tid, "status": "error", "error": str(e)})
                continue

            results.append({"action": "stake", "token_id": tid, **r})
            if r["status"] == "success":
                record_staked(col["staking_address"], account.address, tid)
                time.sleep(2)
            else:
                logger.warning(f"Stake failed for #{tid}, status: {r.get('status')}")
                time.sleep(1)

        invalidate_token_cache(col["nft_address"])
        staked = sum(1 for r in results if r.get("action") == "stake" and r.get("status") == "success")
        skipped = sum(1 for r in results if r.get("action") == "stake" and r.get("status") != "success")
        logger.info(f"Stake all complete: {staked} staked, {skipped} failed/skipped out of {len(token_ids)}")
        return jsonify({"results": results, "total": len(token_ids), "staked": staked, "failed": skipped})
    except Exception as e:
        logger.error(f"Stake all error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/unstake/<int:idx>/<int:token_id>", methods=["POST"])
def api_unstake(idx, token_id):
    if not account:
        return jsonify({"error": "No wallet configured"}), 400
    if idx < 0 or idx >= len(COLLECTIONS_STORE):
        return jsonify({"error": "Invalid collection"}), 400
    col = COLLECTIONS_STORE[idx]
    try:
        vault = get_staking_contract(col["staking_address"])

        def build(from_addr):
            return vault.functions.unstake(token_id).build_transaction({"from": from_addr})

        result = send_tx(build)
        if result["status"] == "success":
            record_unstaked(col["staking_address"], account.address, token_id)
            invalidate_token_cache(col["nft_address"])
        return jsonify(result)
    except Exception as e:
        logger.error(f"Unstake error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/unstake_all/<int:idx>", methods=["POST"])
def api_unstake_all(idx):
    if not account:
        return jsonify({"error": "No wallet configured"}), 400
    if idx < 0 or idx >= len(COLLECTIONS_STORE):
        return jsonify({"error": "Invalid collection"}), 400
    col = COLLECTIONS_STORE[idx]
    try:
        data = request.get_json(silent=True) or {}
        staked_ids = data.get("token_ids")
        if not staked_ids:
            nft_info = discover_tokens(col["nft_address"], col["staking_address"], account.address)
            staked_ids = nft_info["staked_ids"]
        if not staked_ids:
            return jsonify({"results": [{"action": "none", "message": "No staked NFTs to unstake"}]})

        staked_ids = sorted(staked_ids)
        logger.info(f"Unstake all: {len(staked_ids)} tokens to unstake")

        vault = get_staking_contract(col["staking_address"])
        results = []
        for tid in staked_ids:
            logger.info(f"Unstaking token #{tid} ({staked_ids.index(tid)+1}/{len(staked_ids)})...")

            def build_unstake(from_addr, _tid=tid):
                return vault.functions.unstake(_tid).build_transaction({"from": from_addr})

            try:
                r = send_tx(build_unstake)
            except Exception as e:
                logger.error(f"Unstake tx failed for #{tid}: {e}")
                results.append({"action": "unstake", "token_id": tid, "status": "error", "error": str(e)})
                continue

            results.append({"action": "unstake", "token_id": tid, **r})
            if r["status"] == "success":
                record_unstaked(col["staking_address"], account.address, tid)
                time.sleep(2)
            else:
                logger.warning(f"Unstake failed for #{tid}, status: {r.get('status')}")
                time.sleep(1)

        invalidate_token_cache(col["nft_address"])
        unstaked = sum(1 for r in results if r.get("action") == "unstake" and r.get("status") == "success")
        failed = sum(1 for r in results if r.get("action") == "unstake" and r.get("status") != "success")
        logger.info(f"Unstake all complete: {unstaked} unstaked, {failed} failed out of {len(staked_ids)}")
        return jsonify({"results": results, "total": len(staked_ids), "unstaked": unstaked, "failed": failed})
    except Exception as e:
        logger.error(f"Unstake all error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/claim/<int:idx>", methods=["POST"])
def api_claim(idx):
    if not account:
        return jsonify({"error": "No wallet configured"}), 400
    if idx < 0 or idx >= len(COLLECTIONS_STORE):
        return jsonify({"error": "Invalid collection"}), 400
    col = COLLECTIONS_STORE[idx]
    try:
        vault = get_staking_contract(col["staking_address"])

        def build(from_addr):
            return vault.functions.claimRewards().build_transaction({"from": from_addr})

        return jsonify(send_tx(build))
    except Exception as e:
        logger.error(f"Claim error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/collection/<int:idx>")
def api_collection_info(idx):
    if not account:
        return jsonify({"error": "No wallet configured"}), 400
    if idx < 0 or idx >= len(COLLECTIONS_STORE):
        return jsonify({"error": "Invalid collection"}), 400
    col = COLLECTIONS_STORE[idx]
    try:
        stats = load_collection_stats(col)
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/discover/<int:idx>")
def api_discover(idx):
    if not account:
        return jsonify({"error": "No wallet configured"}), 400
    if idx < 0 or idx >= len(COLLECTIONS_STORE):
        return jsonify({"error": "Invalid collection"}), 400
    col = COLLECTIONS_STORE[idx]
    try:
        invalidate_token_cache(col["nft_address"])
        nft_info = discover_tokens(col["nft_address"], col["staking_address"], account.address)
        return jsonify(nft_info)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/collections", methods=["POST"])
def api_add_collection():
    data = request.get_json()
    name = data.get("name", "").strip()
    nft_address = data.get("nft_address", "").strip()
    staking_address = data.get("staking_address", "").strip()
    if not name or not nft_address or not staking_address:
        return jsonify({"error": "All fields required"}), 400
    if not w3.is_address(nft_address) or not w3.is_address(staking_address):
        return jsonify({"error": "Invalid addresses"}), 400
    COLLECTIONS_STORE.append({
        "name": name,
        "nft_address": Web3.to_checksum_address(nft_address),
        "staking_address": Web3.to_checksum_address(staking_address),
    })
    return jsonify({"success": True, "index": len(COLLECTIONS_STORE) - 1})


@app.route("/api/collections/<int:idx>", methods=["DELETE"])
def api_remove_collection(idx):
    if idx < 0 or idx >= len(COLLECTIONS_STORE):
        return jsonify({"error": "Invalid index"}), 400
    COLLECTIONS_STORE.pop(idx)
    return jsonify({"success": True})


# ── Auto-Stake ───────────────────────────────────────────────────

auto_stake_running = False


def auto_stake_loop():
    global auto_stake_running
    while auto_stake_running:
        logger.info("Auto-stake cycle starting...")
        for col in COLLECTIONS_STORE:
            try:
                nft_info = discover_tokens(
                    col["nft_address"], col["staking_address"], account.address
                )
                wallet_tokens = nft_info["token_ids"]
                if not wallet_tokens:
                    logger.info(f"[Auto] No unstaked tokens for {col['name']}")
                    continue

                nft = get_nft_contract(col["nft_address"])
                vault = get_staking_contract(col["staking_address"])
                wallet = Web3.to_checksum_address(account.address)
                spender = Web3.to_checksum_address(col["staking_address"])

                approved = nft.functions.isApprovedForAll(wallet, spender).call()
                if not approved:
                    logger.info(f"[Auto] Approving {spender}...")

                    def build_approve(from_addr):
                        return nft.functions.setApprovalForAll(
                            spender, True
                        ).build_transaction({"from": from_addr})

                    r = send_tx(build_approve)
                    if r["status"] != "success":
                        continue
                    time.sleep(3)

                for tid in wallet_tokens:
                    if not auto_stake_running:
                        break

                    def build_stake(from_addr, _tid=tid):
                        return vault.functions.stake(_tid).build_transaction(
                            {"from": from_addr}
                        )

                    r = send_tx(build_stake)
                    if r["status"] == "success":
                        record_staked(col["staking_address"], account.address, tid)
                        logger.info(f"[Auto] Staked {col['name']} #{tid}")
                        invalidate_token_cache(col["nft_address"])
                    time.sleep(3)

            except Exception as e:
                logger.error(f"[Auto] Error for {col['name']}: {e}")
        logger.info(f"Auto-stake cycle complete. Next in {AUTO_STAKE_INTERVAL}s")
        time.sleep(AUTO_STAKE_INTERVAL)


@app.route("/api/auto_stake/start", methods=["POST"])
def api_auto_stake_start():
    global auto_stake_running, auto_stake_thread
    if not account:
        return jsonify({"error": "No wallet configured"}), 400
    if auto_stake_running:
        return jsonify({"error": "Already running"}), 400
    if AUTO_STAKE_INTERVAL <= 0:
        return jsonify({"error": "Auto-stake interval not configured"}), 400
    auto_stake_running = True
    auto_stake_thread = threading.Thread(target=auto_stake_loop, daemon=True)
    auto_stake_thread.start()
    return jsonify({"status": "started", "interval": AUTO_STAKE_INTERVAL})


@app.route("/api/auto_stake/stop", methods=["POST"])
def api_auto_stake_stop():
    global auto_stake_running
    auto_stake_running = False
    return jsonify({"status": "stopped"})


@app.route("/api/auto_stake/status")
def api_auto_stake_status():
    return jsonify({"running": auto_stake_running, "interval": AUTO_STAKE_INTERVAL})


if __name__ == "__main__":
    if not w3.is_connected():
        logger.warning("Failed to connect to ApeChain RPC!")
    else:
        logger.info(f"Connected to ApeChain (Chain ID: {APECHAIN_CHAIN_ID})")
    if account:
        logger.info(f"Wallet: {account.address}")
        balance = w3.eth.get_balance(account.address)
        logger.info(f"Balance: {Web3.from_wei(balance, 'ether')} APE")
    app.run(host="0.0.0.0", port=5000, debug=True)