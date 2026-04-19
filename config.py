import json
import os

from dotenv import load_dotenv

load_dotenv()

APECHAIN_CHAIN_ID = 33139
APECHAIN_RPC_URL = os.getenv("APECHAIN_RPC_URL", "https://apechain.api.onfinality.io/public")
APECHAIN_EXPLORER = "https://apescan.io"
ETHERSCAN_API_V2_URL = "https://api.etherscan.io/v2/api"
APESCAN_API_KEY = os.getenv("APESCAN_API_KEY", "")
NFT_SCAN_MAX_RANGE = int(os.getenv("NFT_SCAN_MAX_RANGE", "20000"))

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
if PRIVATE_KEY and not PRIVATE_KEY.startswith("0x"):
    PRIVATE_KEY = "0x" + PRIVATE_KEY

AUTO_STAKE_INTERVAL = int(os.getenv("AUTO_STAKE_INTERVAL", "60"))

COLLECTIONS_RAW = os.getenv("COLLECTIONS", "[]")
try:
    COLLECTIONS = json.loads(COLLECTIONS_RAW)
except json.JSONDecodeError:
    COLLECTIONS = []

ABI_DIR = os.path.join(os.path.dirname(__file__), "abis")

with open(os.path.join(ABI_DIR, "staking_vault.json")) as f:
    STAKING_VAULT_ABI = json.load(f)

with open(os.path.join(ABI_DIR, "erc721.json")) as f:
    ERC721_ABI = json.load(f)