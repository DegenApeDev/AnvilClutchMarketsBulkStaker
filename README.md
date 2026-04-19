# Anvil Staking Tool

Automated NFT staking tool for the [Anvil](https://anvil.clutch.market/) staking vaults on ApeChain. Stake, unstake, and claim rewards for your NFTs through a web UI or API.

## Prerequisites

- Python 3.10+
- An ApeChain wallet with APE for gas
- [Etherscan API key](https://etherscan.io/myapikey) (free — used for token discovery)
- The staking contract address for your collection (found via ApeScan)

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure your `.env` file

Copy `.env.example` to `.env` and fill in your details:

```bash
cp .env.example .env
```

```env
# Your wallet private key (without 0x prefix)
PRIVATE_KEY=abc123...

# ApeChain RPC
APECHAIN_RPC_URL=https://apechain.api.onfinality.io/public

# Etherscan API v2 key (free at https://etherscan.io/myapikey)
APESCAN_API_KEY=YOUR_API_KEY_HERE

# Collections to manage
COLLECTIONS=[{"name":"Savage Apes","nft_address":"0x7e3cba2eb90cc27d34bb9475fc85cfafe12df47a","staking_address":"0xd5B10C91568CE8E841d88cDa880846705A423b91"}]

# Auto-stake check interval in seconds (0 = disabled)
AUTO_STAKE_INTERVAL=60
```

### 3. Find your staking contract address

1. Stake 1 NFT manually on [anvil.clutch.market](https://anvil.clutch.market/)
2. Find the transaction on [ApeScan](https://apescan.io)
3. The **"To"** address is your staking vault contract
4. Add it to `COLLECTIONS` in your `.env`

## Running

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000) in your browser.

## How It Works

### Token Discovery

The tool finds your NFTs using two methods:

1. **Etherscan API v2** (primary) — fetches all NFT transfer history for your wallet, then verifies current ownership on-chain via `ownerOf()`
2. **ownerOf scan** (fallback) — if the API is unavailable, scans token IDs 0 to `NFT_SCAN_MAX_RANGE` calling `ownerOf()` on each

Staked tokens are detected by checking if the staking vault `owns` the NFT and your wallet is the staker in the vault's `stakes()` mapping.

Results are cached for 5 minutes. Click **"Refresh Tokens"** to force a re-scan.

### Staking

NFTs must be staked **one at a time** (royalty enforcement prevents batch operations). The "Stake All" button stakes tokens sequentially with a 2-second delay between each.

Flow:
1. **Approve** — Call `setApprovalForAll` on the NFT contract (one-time per collection)
2. **Stake** — Call `stake(tokenId)` on the vault for each NFT
3. **Claim** — Call `claimRewards()` to collect accumulated rewards
4. **Unstake** — Call `unstake(tokenId)` to withdraw (note: cooldown period applies)

### Auto-Stake

When enabled, the tool checks every `AUTO_STAKE_INTERVAL` seconds for unstaked NFTs in your wallet and stakes them automatically.

```
POST /api/auto_stake/start   # Enable
POST /api/auto_stake/stop     # Disable
GET  /api/auto_stake/status   # Check status
```

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/approve/<idx>` | Approve staking contract for NFT transfers |
| POST | `/api/stake/<idx>/<token_id>` | Stake a single NFT |
| POST | `/api/stake_all/<idx>` | Stake all unstaked NFTs sequentially |
| POST | `/api/unstake/<idx>/<token_id>` | Unstake a single NFT |
| POST | `/api/unstake_all/<idx>` | Unstake all staked NFTs sequentially |
| POST | `/api/claim/<idx>` | Claim accumulated rewards |
| GET | `/api/collection/<idx>` | Get collection stats |
| GET | `/api/discover/<idx>` | Force re-scan of tokens |
| GET | `/api/status` | Wallet & connection status |
| POST | `/api/collections` | Add a new collection (JSON body) |
| DELETE | `/api/collections/<idx>` | Remove a collection |

`<idx>` is the 0-based collection index from your `COLLECTIONS` config.

`stake_all` and `unstake_all` accept an optional JSON body with `token_ids` array to stake/unstake specific tokens:

```json
{"token_ids": [874, 876, 877]}
```

## Adding More Collections

Add entries to the `COLLECTIONS` JSON array in `.env`:

```json
[
  {"name": "Savage Apes", "nft_address": "0x7e3cba2eb90cc27d34bb9475fc85cfafe12df47a", "staking_address": "0xd5B10C91568CE8E841d88cDa880846705A423b91"},
  {"name": "My Other Collection", "nft_address": "0x...", "staking_address": "0x..."}
]
```

Or via API:

```bash
curl -X POST http://localhost:5000/api/collections \
  -H "Content-Type: application/json" \
  -d '{"name":"My Collection","nft_address":"0x...","staking_address":"0x..."}'
```

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `PRIVATE_KEY` | — | Wallet private key (no 0x prefix) |
| `APECHAIN_RPC_URL` | `https://apechain.api.onfinality.io/public` | ApeChain RPC endpoint |
| `APESCAN_API_KEY` | — | Etherscan API v2 key (required for token discovery) |
| `COLLECTIONS` | `[]` | JSON array of collection configs |
| `AUTO_STAKE_INTERVAL` | `60` | Auto-stake check interval in seconds (0 = off) |
| `NFT_SCAN_MAX_RANGE` | `20000` | Max token IDs to scan in ownerOf fallback mode |

## Troubleshooting

**"Invalid PRIVATE_KEY configured"** — Make sure your private key is correct and doesn't have extra spaces or quotes.

**"Etherscan API v2: Max rate limit reached"** — You're hitting the free tier rate limit. Either add an API key or wait and retry. The ownerOf fallback will still work but is slower.

**Tokens not showing up** — Click "Refresh Tokens" to force a re-scan. Check your `APESCAN_API_KEY` is set correctly.

**Stake transaction fails** — Make sure you have enough APE for gas. Some collections may have a cooldown period before unstaking.

## Security

- **Never share your private key or commit your `.env` file**
- `.gitignore` includes `.env` and `data/`
- Token data is cached locally in `data/token_cache/`
- Staked token records are stored in `data/staked_tokens.json`