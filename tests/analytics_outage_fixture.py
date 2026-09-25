from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
from typing import Any, Iterator, Literal, Sequence

from playwright.sync_api import Browser, BrowserContext, Page


AnalyticsFailureMode = Literal["throw", "reject"]
ANALYTICS_FAILURE_MODES: tuple[AnalyticsFailureMode, ...] = ("throw", "reject")


@dataclass(frozen=True)
class PurchaseOutcome:
    result: Any = None
    error: str | None = None
    deferred: bool = False

    @classmethod
    def successful(cls, result: Any = None, *, deferred: bool = False):
        return cls(result=result, deferred=deferred)

    @classmethod
    def rejected(cls, error: str, *, deferred: bool = False):
        return cls(error=error, deferred=deferred)


def install_purchase_behavior_script() -> str:
    return """window.__confirmedPurchaseOutcomes = 0;
window.__purchaseCalls = 0;
window.__purchaseItemIds = [];
window.TLAMA_WEB3.purchaseGameItem = async function(item) {
    window.__purchaseItemIds.push(item.id);
    const callIndex = window.__purchaseCalls++;
    const outcome = purchaseOutcomes[callIndex];
    if (!outcome) {
        throw new Error(`No purchase outcome configured for call ${callIndex + 1}`);
    }
    if (outcome.deferred) {
        await new Promise((resolve) => {
            window.__settlePurchase = resolve;
        });
    }
    if (outcome.error) throw new Error(outcome.error);
    window.__confirmedPurchaseOutcomes += 1;
    return outcome.result;
};"""


@dataclass
class AnalyticsOutagePage:
    context: BrowserContext
    page: Page
    errors: list[str]


@contextmanager
def analytics_outage_page(
    browser: Browser,
    failure_mode: AnalyticsFailureMode,
    *,
    viewport: dict[str, int] | None = None,
    setup_script: str = "",
    payment_config: dict[str, Any] | None = None,
    purchase_outcomes: Sequence[PurchaseOutcome] = (),
) -> Iterator[AnalyticsOutagePage]:
    context = browser.new_context(viewport=viewport)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    init_script = """(() => {
            const [failureMode, paymentConfig, purchaseOutcomes] = __FIXTURE_ARGS__;
            __SETUP_SCRIPT__
            window.__analyticsAttempts = [];
            window.__connectCalls = 0;
            if (paymentConfig) {
                window.TLAMA_PAYMENT_CONFIG = paymentConfig;
                window.TLAMA_WEB3 = {
                    getWallet() { return null; },
                    async connect() {
                        window.__connectCalls += 1;
                        const wallet = {
                            publicKey: {
                                toString: () => "WalletAddressMustNeverReachAnalytics",
                            },
                        };
                        window.dispatchEvent(new CustomEvent("tlama-wallet-connected"));
                        return wallet;
                    },
                };
                __PURCHASE_BEHAVIOR_SCRIPT__
            }
            class TestWebSocket {
                static CONNECTING = 0;
                static OPEN = 1;
                constructor() {
                    this.readyState = TestWebSocket.OPEN;
                    this.listeners = {};
                    window.__socket = this;
                }
                addEventListener(name, callback) {
                    (this.listeners[name] ||= []).push(callback);
                }
                send() {}
                message(payload) {
                    (this.listeners.message || []).forEach((callback) =>
                        callback({ data: JSON.stringify(payload) })
                    );
                }
            }
            window.WebSocket = TestWebSocket;
            window.umami = {
                track(name) {
                    window.__analyticsAttempts.push(name);
                    const error = new Error(`analytics unavailable: ${name}`);
                    if (failureMode === "throw") throw error;
                    return Promise.reject(error);
                },
            };
        })();"""
    page.add_init_script(
        script=init_script.replace(
            "__FIXTURE_ARGS__",
            json.dumps(
                [
                    failure_mode,
                    payment_config,
                    [asdict(outcome) for outcome in purchase_outcomes],
                ]
            ),
        ).replace("__SETUP_SCRIPT__", setup_script).replace(
            "__PURCHASE_BEHAVIOR_SCRIPT__", install_purchase_behavior_script()
        )
    )
    page.route("**/config.js", lambda route: route.fulfill(body=""))
    page.route("**/web3-onboarding.js", lambda route: route.fulfill(body=""))

    try:
        yield AnalyticsOutagePage(context=context, page=page, errors=errors)
    finally:
        context.close()