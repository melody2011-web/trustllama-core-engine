#!/usr/bin/env node
/**
 * Read-only PancakeSwap Smart Router bridge.
 *
 * Reads one JSON request from stdin and returns JSON on stdout. It deliberately
 * does not receive a private key and never sends a transaction.
 */
import {
  SMART_ROUTER_ADDRESSES,
  SmartRouter,
  SwapRouter,
} from "@pancakeswap/smart-router/evm";
import { CurrencyAmount, Token, TradeType } from "@pancakeswap/swap-sdk-core";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { createPublicClient, getAddress, http, isAddress } from "viem";
import { bsc } from "viem/chains";

const CHAIN_ID = 56;
// `@pancakeswap/sdk` is a declared dependency of the pinned Smart Router
// release. Resolve it from that package so this bridge uses the exact Native
// currency implementation that the router's trade and calldata builders use.
const routerRequire = createRequire(
  createRequire(import.meta.url).resolve("@pancakeswap/smart-router/package.json"),
);
const { Native, Percent } = await import(
  pathToFileURL(routerRequire.resolve("@pancakeswap/sdk")).href,
);
const BNB_CHAIN_TOKENS = Object.freeze({
  "0x1d2f0da169ceb9fc7b3144628db156f3f6c60dbe": Object.freeze({
    address: "0x1d2f0da169ceb9fc7b3144628db156f3f6c60dbe",
    decimals: 18,
    symbol: "XRP",
  }),
  "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c": Object.freeze({
    address: "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c",
    decimals: 18,
    symbol: "BTC",
  }),
  "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c": Object.freeze({
    address: "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
    decimals: 18,
    symbol: "WBNB",
  }),
  "0x2170ed0880ac9a755fd29b2688956bd959f933f8": Object.freeze({
    address: "0x2170ed0880ac9a755fd29b2688956bd959f933f8", decimals: 18, symbol: "ETH",
  }),
  "0x570a5d26f7765ecb712c0924e4de545b89fd43df": Object.freeze({
    address: "0x570a5d26f7765ecb712c0924e4de545b89fd43df", decimals: 18, symbol: "SOL",
  }),
  "0x55d398326f99059ff775485246999027b3197955": Object.freeze({
    address: "0x55d398326f99059fF775485246999027B3197955",
    decimals: 18,
    symbol: "USDT",
  }),
});
const USDT_ADDRESS = "0x55d398326f99059ff775485246999027b3197955";
const DIRECT_V3_STRATEGY_TARGETS = new Set([
  "0x1d2f0da169ceb9fc7b3144628db156f3f6c60dbe",
  "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c",
]);
const BRIDGE_ADDRESSES = Object.freeze([
  "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
  "0x55d398326f99059ff775485246999027b3197955",
]);
const SMART_ROUTER_ADDRESS = SMART_ROUTER_ADDRESSES[CHAIN_ID];

async function readRequest() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function token(spec) {
  const address = String(spec.address).toLowerCase();
  const knownToken = BNB_CHAIN_TOKENS[address];
  if (!knownToken) {
    throw new Error(`Unsupported BNB Chain token address: ${spec.address}`);
  }
  if (Number(spec.decimals) !== knownToken.decimals) {
    throw new Error(`Decimals do not match token address: ${spec.address}`);
  }
  return new Token(
    CHAIN_ID,
    knownToken.address,
    knownToken.decimals,
    knownToken.symbol,
  );
}

function knownToken(address) {
  const details = BNB_CHAIN_TOKENS[address.toLowerCase()];
  if (!details) throw new Error(`Unsupported BNB Chain token address: ${address}`);
  return new Token(CHAIN_ID, details.address, details.decimals, details.symbol);
}

function pairsThroughBridge(inputToken, outputToken, bridgeToken) {
  if (inputToken.equals(bridgeToken)) return [[inputToken, outputToken]];
  if (outputToken.equals(bridgeToken)) return [[inputToken, outputToken]];
  return [
    [inputToken, bridgeToken],
    [bridgeToken, outputToken],
  ];
}

async function unfilteredPoolsForPairs(pairs, client) {
  const [v2Result, v3Result, stableResult] = await Promise.allSettled([
    SmartRouter.getV2PoolsOnChain(pairs, () => client),
    SmartRouter.getV3PoolsWithoutTicksOnChain(pairs, () => client),
    SmartRouter.getStablePoolsOnChain(pairs, () => client),
  ]);
  const v2 = v2Result.status === "fulfilled" ? v2Result.value : [];
  const v3 = v3Result.status === "fulfilled" ? v3Result.value : [];
  const stable = stableResult.status === "fulfilled" ? stableResult.value : [];
  return {
    pools: [...v2, ...v3, ...stable],
    candidatePools: { v2: v2.length, v3: v3.length, stable: stable.length },
  };
}

function validateBuildRequest(request, inputToken, outputToken) {
  if (!isAddress(request.recipient)) {
    throw new Error("A valid recipient address is required for Smart Router calldata.");
  }
  if (
    !Number.isInteger(request.slippageBps) ||
    request.slippageBps <= 0 ||
    request.slippageBps >= 10_000
  ) {
    throw new Error("slippageBps must be an integer between 1 and 9999.");
  }
  if (
    !Number.isSafeInteger(request.deadline) ||
    request.deadline <= Math.floor(Date.now() / 1_000)
  ) {
    throw new Error("deadline must be a future Unix timestamp.");
  }
  if (request.nativeInput && !inputToken.equals(knownToken(BRIDGE_ADDRESSES[0]))) {
    throw new Error("Only WBNB can be represented as native BNB input.");
  }
  if (request.nativeOutput && !outputToken.equals(knownToken(BRIDGE_ADDRESSES[0]))) {
    throw new Error("Only WBNB can be represented as native BNB output.");
  }
  if (request.nativeInput && request.nativeOutput) {
    throw new Error("A Smart Router trade cannot use native BNB for both input and output.");
  }
  if (request.routePolicy === "direct_v3" && (request.nativeInput || request.nativeOutput)) {
    throw new Error("Direct V3 strategy routes cannot use native BNB input or output.");
  }
}

function validateDirectV3StrategyPair(inputToken, outputToken) {
  const input = inputToken.address.toLowerCase();
  const output = outputToken.address.toLowerCase();
  const target = input === USDT_ADDRESS ? output : input;
  if (
    (input !== USDT_ADDRESS && output !== USDT_ADDRESS) ||
    !DIRECT_V3_STRATEGY_TARGETS.has(target)
  ) {
    throw new Error(
      "Direct V3 strategy routes support USDT/XRP and USDT/BTCB only.",
    );
  }
}

async function bestDirectV3Trade(request, client, inputToken, outputToken, amountIn) {
  const v3Pools = await SmartRouter.getV3PoolsWithoutTicksOnChain(
    [[inputToken, outputToken]],
    () => client,
  );
  const candidatePools = { v2: 0, v3: v3Pools.length, stable: 0 };
  let trade = null;
  if (v3Pools.length) {
    try {
      trade = await SmartRouter.getBestTrade(
        CurrencyAmount.fromRawAmount(inputToken, amountIn),
        outputToken,
        TradeType.EXACT_INPUT,
        {
          gasPriceWei: () => client.getGasPrice(),
          poolProvider: SmartRouter.createStaticPoolProvider(v3Pools),
          quoteProvider: SmartRouter.createQuoteProvider({
            onChainProvider: () => client,
          }),
          maxHops: 1,
          maxSplits: 1,
          distributionPercent: 100,
          quoterOptimization: false,
        },
      );
    } catch (error) {
      if (!String(error).includes("Cannot find a valid swap route")) throw error;
    }
  }
  const attempt = {
    bridge: { address: "direct-v3", symbol: "DIRECT_V3" },
    candidatePools,
    trade,
  };
  return { attempts: [attempt], bestAttempt: trade ? attempt : null, candidatePools };
}

async function bestTrade(request, client, inputToken, outputToken, amountIn) {
  const inputCurrency = request.nativeInput ? Native.onChain(CHAIN_ID) : inputToken;
  const outputCurrency = request.nativeOutput ? Native.onChain(CHAIN_ID) : outputToken;
  const attempts = await Promise.all(
    BRIDGE_ADDRESSES.map(async (bridgeAddress) => {
      const bridgeToken = knownToken(bridgeAddress);
      const { pools, candidatePools } = await unfilteredPoolsForPairs(
        pairsThroughBridge(inputToken, outputToken, bridgeToken),
        client,
      );
      let trade = null;
      if (pools.length) {
        try {
          trade = await SmartRouter.getBestTrade(
            CurrencyAmount.fromRawAmount(inputCurrency, amountIn),
            outputCurrency,
            TradeType.EXACT_INPUT,
            {
              gasPriceWei: () => client.getGasPrice(),
              poolProvider: SmartRouter.createStaticPoolProvider(pools),
              quoteProvider: SmartRouter.createQuoteProvider({
                onChainProvider: () => client,
              }),
              // Quote the full requested amount on the explicitly supplied
              // bridge legs. This disables TVL ranking and split-size sampling.
              maxHops: 2,
              maxSplits: 1,
              distributionPercent: 100,
              quoterOptimization: false,
            },
          );
        } catch (error) {
          if (!String(error).includes("Cannot find a valid swap route")) throw error;
        }
      }
      return {
        bridge: { address: bridgeToken.address, symbol: bridgeToken.symbol },
        candidatePools,
        trade,
      };
    }),
  );
  const successfulAttempts = attempts.filter((attempt) => attempt.trade);
  const bestAttempt = successfulAttempts.reduce(
    (best, attempt) =>
      !best || attempt.trade.outputAmount.quotient > best.trade.outputAmount.quotient
        ? attempt
        : best,
    null,
  );
  const candidatePools = attempts.reduce(
    (totals, attempt) => ({
      v2: totals.v2 + attempt.candidatePools.v2,
      v3: totals.v3 + attempt.candidatePools.v3,
      stable: totals.stable + attempt.candidatePools.stable,
    }),
    { v2: 0, v3: 0, stable: 0 },
  );
  return { attempts, bestAttempt, candidatePools };
}

function noRouteResponse(attempts, candidatePools) {
  return {
    ok: false,
    reason: "no_valid_explicit_bridge_route",
    candidatePools,
    bridgeAttempts: attempts.map(({ bridge, candidatePools: pools }) => ({
      bridge,
      candidatePools: pools,
    })),
  };
}

try {
  const request = await readRequest();
  if (!["quote", "build"].includes(request.operation)) {
    throw new Error("Smart Router bridge operation must be quote or build.");
  }
  if (!process.env.BSC_RPC_URL) {
    throw new Error("BSC_RPC_URL is required for Smart Router quoting.");
  }

  const inputToken = token(request.input);
  const outputToken = token(request.output);
  if (inputToken.equals(outputToken)) {
    throw new Error("Smart Router input and output tokens must differ.");
  }
  const amountIn = BigInt(request.amountIn);
  if (amountIn <= 0n) throw new Error("amountIn must be greater than zero.");
  if (!["hybrid", "direct_v3"].includes(request.routePolicy ?? "hybrid")) {
    throw new Error("Unsupported Smart Router route policy.");
  }
  if (request.operation === "build") {
    validateBuildRequest(request, inputToken, outputToken);
  }
  if (request.routePolicy === "direct_v3") {
    validateDirectV3StrategyPair(inputToken, outputToken);
  }

  const client = createPublicClient({
    chain: bsc,
    transport: http(process.env.BSC_RPC_URL),
  });
  const { attempts, bestAttempt, candidatePools } = request.routePolicy === "direct_v3"
    ? await bestDirectV3Trade(request, client, inputToken, outputToken, amountIn)
    : await bestTrade(request, client, inputToken, outputToken, amountIn);

  if (!bestAttempt) {
    console.log(JSON.stringify(noRouteResponse(attempts, candidatePools)));
  } else {
    const response = {
      ok: true,
      amountOut: bestAttempt.trade.outputAmount.quotient.toString(),
      candidatePools,
      bridge: bestAttempt.bridge,
      routeCount: bestAttempt.trade.routes?.length ?? 1,
      bridgeAttempts: attempts.map(({ bridge, candidatePools: pools }) => ({
        bridge,
        candidatePools: pools,
      })),
    };
    if (request.operation === "build") {
      const parameters = SwapRouter.swapCallParameters(bestAttempt.trade, {
        recipient: getAddress(request.recipient),
        slippageTolerance: new Percent(BigInt(request.slippageBps), 10_000n),
        deadlineOrPreviousBlockhash: BigInt(request.deadline),
      });
      if (
        !SMART_ROUTER_ADDRESS ||
        !parameters.calldata.startsWith("0x") ||
        !parameters.value.startsWith("0x")
      ) {
        throw new Error("Smart Router returned invalid transaction parameters.");
      }
      Object.assign(response, {
        routerAddress: SMART_ROUTER_ADDRESS,
        calldata: parameters.calldata,
        value: BigInt(parameters.value).toString(),
        minimumOut: SmartRouter.minimumAmountOut(
          bestAttempt.trade,
          new Percent(BigInt(request.slippageBps), 10_000n),
        ).quotient.toString(),
      });
    }
    console.log(
      JSON.stringify(response),
    );
  }
} catch (error) {
  console.log(
    JSON.stringify({
      ok: false,
      reason: "smart_router_error",
      error: error instanceof Error ? error.message : String(error),
    }),
  );
}