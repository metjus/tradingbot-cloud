from argus.marketdata.aggregator import CandleAggregator
from argus.marketdata.model import Candle, Timeframe


def m1(ts: int, o: float, h: float, lo: float, c: float, v: float, sym: str = "BTC/USD") -> Candle:
    return Candle(symbol=sym, timeframe=Timeframe.M1, ts=ts, open=o, high=h,
                  low=lo, close=c, volume=v, source="test", complete=True)


def collect():
    closed: list[Candle] = []
    agg = CandleAggregator(on_close=closed.append)
    return agg, closed


def test_5m_aggregation_ohlcv():
    agg, closed = collect()
    base = 1_700_000_100 - 1_700_000_100 % 300  # aligned 5m boundary
    bars = [
        m1(base + 0, 10, 12, 9, 11, 1),
        m1(base + 60, 11, 15, 11, 14, 2),
        m1(base + 120, 14, 14, 8, 9, 3),
        m1(base + 180, 9, 10, 9, 10, 4),
        m1(base + 240, 10, 11, 10, 11, 5),  # last slice -> closes the 5m bar
    ]
    for b in bars:
        agg.add_m1(b)
    five = [c for c in closed if c.timeframe is Timeframe.M5]
    assert len(five) == 1
    c = five[0]
    assert (c.ts, c.open, c.high, c.low, c.close, c.volume) == (base, 10, 15, 8, 11, 15)
    assert c.complete


def test_closes_on_next_bucket_after_gap():
    agg, closed = collect()
    base = 1_700_003_400 - 1_700_003_400 % 300
    agg.add_m1(m1(base + 0, 1, 2, 1, 2, 1))
    agg.add_m1(m1(base + 60, 2, 3, 2, 3, 1))
    # gap: minutes 2-4 missing; next bar lands in the following 5m bucket
    agg.add_m1(m1(base + 300, 3, 4, 3, 4, 1))
    five = [c for c in closed if c.timeframe is Timeframe.M5]
    assert len(five) == 1
    assert five[0].ts == base
    assert five[0].close == 3
    assert five[0].volume == 2


def test_duplicate_and_out_of_order_ignored():
    agg, closed = collect()
    base = 1_700_006_700 - 1_700_006_700 % 300
    agg.add_m1(m1(base + 60, 5, 6, 5, 6, 1))
    agg.add_m1(m1(base + 60, 50, 60, 50, 60, 9))  # duplicate -> ignored
    agg.add_m1(m1(base + 0, 1, 1, 1, 1, 1))       # out of order -> ignored
    agg.flush_before("BTC/USD", base + 600)
    five = [c for c in closed if c.timeframe is Timeframe.M5]
    assert len(five) == 1
    assert five[0].high == 6
    assert five[0].volume == 1


def test_all_derived_timeframes_close():
    agg, closed = collect()
    base = 1_700_000_000 - 1_700_000_000 % 14400  # 4h boundary
    for i in range(240):  # four full hours of 1m bars
        agg.add_m1(m1(base + i * 60, 1, 2, 1, 2, 1))
    tf_counts = {}
    for c in closed:
        tf_counts[c.timeframe] = tf_counts.get(c.timeframe, 0) + 1
    assert tf_counts[Timeframe.M5] == 48
    assert tf_counts[Timeframe.M15] == 16
    assert tf_counts[Timeframe.H1] == 4
    assert tf_counts[Timeframe.H4] == 1


def test_symbols_do_not_mix():
    agg, closed = collect()
    base = 1_700_000_000 - 1_700_000_000 % 300
    for i in range(5):
        agg.add_m1(m1(base + i * 60, 1, 1, 1, 1, 1, sym="BTC/USD"))
        agg.add_m1(m1(base + i * 60, 9, 9, 9, 9, 9, sym="ETH/USD"))
    five = [c for c in closed if c.timeframe is Timeframe.M5]
    assert {c.symbol for c in five} == {"BTC/USD", "ETH/USD"}
    btc = next(c for c in five if c.symbol == "BTC/USD")
    assert btc.volume == 5
