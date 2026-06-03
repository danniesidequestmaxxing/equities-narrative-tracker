"""Cashtag extraction (M0)."""

from narrative_tracker.extract import extract_cashtags


def test_basic_cashtags():
    syms = {m["symbol"] for m in extract_cashtags("$AAPL and $msft up big")}
    assert syms == {"AAPL", "MSFT"}


def test_dollar_amounts_are_not_tickers():
    assert extract_cashtags("target $4200 by friday, paid $3.50") == []


def test_class_suffix_preserved():
    mentions = extract_cashtags("$BRK.B is cheap")
    assert mentions[0]["symbol"] == "BRK.B"


def test_crypto_is_tagged():
    mentions = extract_cashtags("$BTC and $ETH pumping")
    assert mentions and all(m["asset_class"] == "crypto" for m in mentions)


def test_equity_default_asset_class():
    mentions = extract_cashtags("$NVDA")
    assert mentions[0]["asset_class"] == "equity"
    assert mentions[0]["resolution_method"] == "cashtag_exact"


def test_dedupe_within_post():
    assert len(extract_cashtags("$AAPL $AAPL $AAPL")) == 1


def test_no_midword_false_match():
    assert extract_cashtags("email me at foo$BAR") == []


def test_mixed_real_and_amount():
    syms = {m["symbol"] for m in extract_cashtags("long $NVDA, $AMD; $4200 SPX level")}
    assert syms == {"NVDA", "AMD"}
