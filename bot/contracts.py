from __future__ import annotations

from web3 import Web3

# Official PancakeSwap V3 BNB Chain deployments:
# https://developer.pancakeswap.finance/contracts/v3/addresses
PANCAKESWAP_V3_ROUTER = Web3.to_checksum_address(
    "0x1b81D678ffb9C0263b24A97847620C99d213eB14"
)
PANCAKESWAP_V3_QUOTER_V2 = Web3.to_checksum_address(
    "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997"
)
# PancakeSwap SwapRouter02 on BNB Chain. This is the router used by the
# PancakeSwap Smart Router SDK for V2, V3, stable, and mixed-route calldata.
PANCAKESWAP_SMART_ROUTER = Web3.to_checksum_address(
    "0x13f4EA83D0bd40E75C8222255bc855a974568Dd4"
)

# Verified BNB Chain Binance-Peg asset contracts:
# - XRP: https://bscscan.com/token/0x1d2f0da169ceb9fc7b3144628db156f3f6c60dbe
# - BTCB: https://bscscan.com/token/0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c
#
# The public CLI uses BTC for the tracked Bitcoin asset; on BNB Chain that asset
# is Binance-Peg BTCB, not native Bitcoin.
TOKENS = {
    "XRP": Web3.to_checksum_address("0x1d2f0da169ceb9fc7b3144628db156f3f6c60dbe"),
    "BTC": Web3.to_checksum_address("0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c"),
    "USDT": Web3.to_checksum_address("0x55d398326f99059fF775485246999027B3197955"),
    "WBNB": Web3.to_checksum_address("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"),
    "ETH": Web3.to_checksum_address("0x2170ed0880ac9a755fd29b2688956bd959f933f8"),
    "SOL": Web3.to_checksum_address("0x570a5d26f7765ecb712c0924e4de545b89fd43df"),
}

# The tracked Binance-Peg assets, USDT, and WBNB each use 18 decimals. Keeping these
# units local lets a read-only route query fail cleanly when a token metadata
# call is unavailable from the RPC.
TOKEN_DECIMALS = {"XRP": 18, "BTC": 18, "USDT": 18, "WBNB": 18, "ETH": 18, "SOL": 18}

# Routing-only assets. The bot only accepts XRP and BTC as trade targets; USDT
# is the dedicated strategy base asset.
ROUTING_TOKENS = {
    "BUSD": Web3.to_checksum_address("0xe9e7CEA3Dedca5984780Bafc599bd69ADd087D56"),
}

# PancakeSwap V3 fee tiers supported by the BNB Chain deployment.
V3_FEE_TIERS = (100, 500, 2500, 10000)

V3_QUOTER_V2_ABI = [
    {
        "inputs": [
            {"internalType": "bytes", "name": "path", "type": "bytes"},
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
        ],
        "name": "quoteExactInput",
        "outputs": [
            {"internalType": "uint256", "name": "amountOut", "type": "uint256"},
            {
                "internalType": "uint160[]",
                "name": "sqrtPriceX96AfterList",
                "type": "uint160[]",
            },
            {
                "internalType": "uint32[]",
                "name": "initializedTicksCrossedList",
                "type": "uint32[]",
            },
            {"internalType": "uint256", "name": "gasEstimate", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

V3_ROUTER_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "bytes", "name": "path", "type": "bytes"},
                    {"internalType": "address", "name": "recipient", "type": "address"},
                    {"internalType": "uint256", "name": "deadline", "type": "uint256"},
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {
                        "internalType": "uint256",
                        "name": "amountOutMinimum",
                        "type": "uint256",
                    },
                ],
                "internalType": "struct IV3SwapRouter.ExactInputParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "exactInput",
        "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "bytes[]", "name": "data", "type": "bytes[]"}],
        "name": "multicall",
        "outputs": [{"internalType": "bytes[]", "name": "results", "type": "bytes[]"}],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountMinimum", "type": "uint256"},
            {"internalType": "address", "name": "recipient", "type": "address"},
        ],
        "name": "unwrapWETH9",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
]

ERC20_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "owner", "type": "address"},
            {"internalType": "address", "name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "spender", "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]