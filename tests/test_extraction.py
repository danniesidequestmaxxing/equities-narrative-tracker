"""Unit tests for the M1 extraction components (all pure / sync)."""

from narrative_tracker.extract import symbology
from narrative_tracker.extract.collision import is_real_ticker
from narrative_tracker.extract.entity import link_entities
from narrative_tracker.extract.options import parse_options
from narrative_tracker.extract.stance import RuleBasedStanceClassifier
from narrative_tracker.schemas.mention import OptionRight, Stance

# --- options ---------------------------------------------------------------


def test_options_strike_then_right_with_dte():
    res = parse_options("grabbing $SPY 600c 0DTE")
    assert len(res) == 1
    root, od, _ = res[0]
    assert root == "SPY"
    assert od.right is OptionRight.CALL
    assert od.strike == 600.0
    assert od.dte_relative == 0


def test_options_word_form_puts():
    root, od, _ = parse_options("$NVDA 200 puts here")[0]
    assert root == "NVDA" and od.right is OptionRight.PUT and od.strike == 200.0


def test_options_right_then_strike():
    root, od, _ = parse_options("$TSLA c250")[0]
    assert od.right is OptionRight.CALL and od.strike == 250.0


def test_options_ambiguous_no_right_not_parsed():
    # "200 LEAPS" with no call/put -> we do NOT invent a direction.
    assert parse_options("$NVDA Jan'27 200 LEAPS") == []


# --- collision gate --------------------------------------------------------


def test_collision_non_collider_is_real():
    assert is_real_ticker("AAPL", "$AAPL up big") == (True, 0.97)


def test_collision_finance_context_is_real():
    verdict, conf = is_real_ticker("ALL", "$ALL calls printing into earnings")
    assert verdict is True and conf == 0.85


def test_collision_word_usage_dropped():
    verdict, _ = is_real_ticker("ON", "$ON and then walk on by")
    assert verdict is False


def test_collision_uncertain_is_punted():
    verdict, _ = is_real_ticker("ON", "$ON")
    assert verdict is None


# --- symbology -------------------------------------------------------------


def test_classify_crypto_default():
    assert symbology.classify_symbol("ETH", "$ETH looking strong") == "crypto"


def test_classify_eth_etf_cue():
    assert symbology.classify_symbol("ETH", "$ETH spot ETF inflows") == "etf"


def test_classify_mstr_always_equity():
    assert symbology.classify_symbol("MSTR", "$MSTR bitcoin proxy on-chain") == "equity"


def test_classify_ambiguous_crypto_lexicon():
    assert symbology.classify_symbol("FOO", "$FOO staking on mainnet") == "crypto"


def test_find_aliases_company():
    hits = symbology.find_aliases("Nvidia keeps printing")
    assert hits and hits[0][1].symbol == "NVDA"


def test_find_aliases_product_longest_match():
    syms = [entry.symbol for _, entry in symbology.find_aliases("the Ozempic maker is up")]
    assert "NVO" in syms


# --- entity link -----------------------------------------------------------


def test_link_entities_indirect_capped():
    mentions = link_entities("Zuck's company is crushing it")
    assert mentions and mentions[0].symbol == "META"
    assert mentions[0].mention_confidence <= 0.85  # indirect reference


# --- stance ----------------------------------------------------------------


def _stance(text):
    return RuleBasedStanceClassifier().classify(text)


def test_stance_negation_makes_bearish():
    r = _stance("$NVDA is NOT a buy up here")
    assert r.stance is Stance.BEARISH and r.negation_flag is True


def test_stance_bullish():
    assert _stance("loaded $SOFI calls lfg \U0001f680").stance is Stance.BULLISH


def test_stance_sarcasm_inverts():
    assert _stance("imagine being long $TSLA here \U0001f480").stance is Stance.BEARISH


def test_stance_question_is_neutral():
    assert _stance("is $AMD a buy at 200?").stance is Stance.NEUTRAL


def test_stance_double_negative_bullish():
    r = _stance("def not selling my $MSFT")
    assert r.stance is Stance.BULLISH and r.negation_flag is True
