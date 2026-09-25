"""Local operator panel for isolated grid and scalper size configuration.

It never collects wallet credentials. Saved sizes are atomically picked up by
the dedicated bots between cycles; existing lots keep their saved size/target.
"""
from __future__ import annotations

import hmac
import os
import sys
from pathlib import Path

import streamlit as st

from bot.isolated_grid import (
    IsolatedGridConfig,
    IsolatedGridConfigError,
    OperatorActivationStore,
    isolated_profile,
)
from bot.live_scalper import ScalperAmountConfig, LiveScalperError, ScalperPaths

GAME_DIR = Path(__file__).resolve().parent / "llama_website" / "llama_game"
if str(GAME_DIR) not in sys.path:
    sys.path.insert(0, str(GAME_DIR))

from opening_warnings import (  # noqa: E402
    DEFAULT_MINIMUM_SAMPLE,
    last_two_completed_utc_months,
    opening_sample_rows,
    resend_uncertain_warning,
)
from storage import ArcadeStorage  # noqa: E402

st.set_page_config(page_title="Secure Trading Panel", page_icon="🔒", layout="centered")
st.title("Secure Trading Panel")
password = os.environ.get("SECURE_PANEL_PASSWORD", "")
if not password:
    st.error("SECURE_PANEL_PASSWORD must be configured as a Replit Secret.")
    st.stop()

if not st.session_state.get("isolated_grid_operator_authenticated", False):
    st.subheader("Unlock live operations")
    st.caption("Standby mode — enter the master password to unlock operator controls.")
    with st.form("master_password_form", clear_on_submit=True):
        supplied = st.text_input(
            "Master password",
            type="password",
            key="master_password",
            autocomplete="current-password",
        )
        submitted = st.form_submit_button("Unlock live operations", type="primary")
    if submitted:
        # Store only the successful boolean; never retain the supplied secret.
        authenticated = hmac.compare_digest(supplied.encode(), password.encode())
        st.session_state.pop("master_password", None)
        if authenticated:
            st.session_state.isolated_grid_operator_authenticated = True
            st.rerun()
        st.error("Master password was not accepted.")
    st.info("Credentials are never collected here; wallet access remains in Replit Secrets.")
    st.stop()

st.warning("No wallet or private-key fields exist here. Credentials remain Replit Secrets.")
st.caption("Changes are atomically saved per bot and picked up between cycles. Open lots retain their original amounts and take-profit targets.")
st.subheader("Uncertain arcade opening warnings")
st.caption(
    "Only warning identifiers and delivery timestamps are shown. No player identity "
    "or free-form analytics data is retained here."
)
arcade_storage = ArcadeStorage(
    Path(os.environ.get("TLAMA_GAME_DB_PATH", GAME_DIR / "data" / "tlama_arcade.sqlite3"))
)
uncertain_warnings = arcade_storage.uncertain_opening_warnings()
if not uncertain_warnings:
    st.info("No opening warnings need manual delivery reconciliation.")
for warning in uncertain_warnings:
    warning_key = str(warning["warning_key"])
    st.markdown(f"**{warning_key}**")
    st.caption(
        f"Delivery became uncertain at Unix time {warning['claimed_at']}."
    )
    confirm, resend = st.columns(2)
    if confirm.button("Mark delivered", key=f"opening_confirm_{warning_key}"):
        if arcade_storage.confirm_uncertain_opening_warning_delivered(warning_key):
            st.success("Warning marked delivered and the operator action was audited.")
            st.rerun()
        st.error("This warning was already resolved by another process.")
    if resend.button(
        "Request one resend",
        key=f"opening_resend_{warning_key}",
        disabled=bool(warning["resend_used"]),
    ):
        outcome = resend_uncertain_warning(arcade_storage, warning_key)
        if outcome == "delivered":
            st.success("The controlled resend was delivered and audited.")
            st.rerun()
        elif outcome in {"rejected", "unknown"}:
            st.error(
                "The resend was not confirmed delivered. It remains listed for "
                "manual confirmation; another resend is blocked."
            )
            st.rerun()
        else:
            st.error("This warning changed or was resolved by another process.")

st.divider()

st.subheader("Arcade opening warning eligibility")
st.caption(
    "Read-only privacy-safe aggregates for the two completed UTC months used by "
    f"the warning scheduler. Each month needs {DEFAULT_MINIMUM_SAMPLE} joins; "
    "sparse months are not evidence that the opening is healthy."
)
arcade_database_path = Path(
    os.environ.get(
        "TLAMA_GAME_DB_PATH",
        GAME_DIR / "data" / "tlama_arcade.sqlite3",
    )
)
try:
    opening_months = last_two_completed_utc_months()
    opening_rows = opening_sample_rows(
        arcade_storage.opening_counts_for_months(opening_months),
        opening_months,
    )
    if opening_rows:
        st.dataframe(opening_rows, use_container_width=True, hide_index=True)
    else:
        st.info(
            "Insufficient sample — no opening aggregates exist for either "
            "completed month."
        )
except Exception as error:
    st.error(f"Opening aggregate counts are unavailable: {error}")

st.divider()
st.subheader("BTC/XRP deployment activation")
st.warning(
    "This creates a 24-hour approval receipt only. Both ENABLE_LIVE_TRADING=true "
    "and LIVE_STRATEGY_MODE=grid are still required in the Reserved VM environment."
)
for bot in ("grid_bot_1", "grid_bot_2"):
    profile = isolated_profile(bot)
    activation = OperatorActivationStore(profile.activation, bot)
    left, right = st.columns(2)
    if left.button(f"Approve {bot} · {profile.symbol}/USDT", key=f"{bot}_approve"):
        activation.activate()
        st.success(f"{bot} activation approved for 24 hours.")
    if right.button(f"Revoke {bot}", key=f"{bot}_revoke"):
        activation.revoke()
        st.success(f"{bot} activation revoked.")

st.divider()
for bot, symbol in (
    ("grid_bot_1", "BTC"),
    ("grid_bot_2", "XRP"),
    ("grid_bot_4", "BNB"),
    ("grid_bot_5", "ETH"),
    ("grid_bot_6", "SOL"),
):
    path = Path.home() / ".local/state/bnb-defi-bot" / bot / "config.json"
    config = IsolatedGridConfig(path)
    try:
        current = float(config.initialize_default())
    except IsolatedGridConfigError as error:
        st.error(f"{bot}: {error}")
        continue
    st.subheader(f"{bot} · {symbol}/USDT")
    line = st.number_input(f"{bot} per-line USDT (six lines; max 50)", 0.01, 50.0, current, 0.01, key=f"{bot}_line")
    if st.button(f"Save {bot} size", key=f"{bot}_save"):
        try:
            saved = config.save(str(line))
            st.success(f"Saved {saved} USDT per line (audit timestamp recorded).")
        except IsolatedGridConfigError as error:
            st.error(str(error))

st.divider()
st.subheader("Independent live scalper entry amounts")
st.caption("Each amount is isolated to its named bot. Missing or invalid values block new entries.")
for bot, symbol in (
    ("scalper_bot_7", "BNB"), ("scalper_bot_8", "ETH"),
    ("scalper_bot_9", "SOL"), ("scalper_bot_10", "XRP"),
    ("scalper_bot_11", "BTC"),
):
    config = ScalperAmountConfig(ScalperPaths.for_bot(bot).config)
    st.markdown(f"**{bot} · {symbol}/USDT**")
    amount = st.number_input(f"{bot} entry USDT", min_value=0.01, value=1.0,
                             step=0.01, key=f"{bot}_amount")
    if st.button(f"Save {bot} entry amount", key=f"{bot}_amount_save"):
        try:
            saved = config.save(str(amount))
            st.success(f"Saved {saved} USDT for {bot}.")
        except LiveScalperError as error:
            st.error(str(error))