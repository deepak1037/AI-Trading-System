
import pytest, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from discord_alerts.discord_signal_reader import AlertParser

P = AlertParser()
def parse(text): return P.parse(text, "test", "ch", "Ravish")

class TestSimple:
    def test_sto_tqqq(self):
        s = parse("STO TQQQ 60 Put @ 3.15"); assert s and s.action=="STO" and s.symbol=="TQQQ" and s.strike==60.0 and s.limit_price==3.15 and s.is_tradeable
    def test_sto_upro(self):
        s = parse("STO UPRO 135 Put @ 5.5"); assert s and s.symbol=="UPRO" and s.limit_price==5.5
    def test_sto_leaps(self):
        s = parse("STO TSM 360 Put LEAPS @ 30.45"); assert s and s.strategy_type=="short_put_leaps" and s.limit_price==30.45
    def test_bto_diagonal(self):
        s = parse("BTO QQQ Double Diagonal Sept-25 @ 0.05 credit"); assert s and s.action=="BTO" and s.symbol=="QQQ" and s.limit_price==0.05

class TestTOS:
    CAL = "BTO SPX Call Calendar @ 11.50 Debit\nSELL -1 7740.00 CALL (Sep 30)\nBUY  +1 7740.00 CALL (Oct 02)\nTOS Code\nBUY +1 CALENDAR SPX 100 (Weeklys) 2 OCT 26/30 SEP 26 7740 CALL @11.50 LMT"
    def test_calendar(self):
        s = parse(self.CAL); assert s and s.symbol=="SPX" and s.limit_price==11.50 and s.strategy_type=="calendar" and s.parse_confidence=="high"
    def test_strike(self):
        s = parse(self.CAL); assert s and s.strike==7740.0
    def test_expiries(self):
        s = parse(self.CAL); assert s and s.expiry_short and s.expiry_long

class TestHype:
    def test_hype_none(self):
        assert parse("$450k Premium Collected - MRVL\nFun fact our group made millions") is None
    def test_link_only(self):
        s = parse("https://optionstrat.com/UMzAxGQh3mlC\nView my strategy"); assert s is None
    def test_filled_none(self):
        s = parse("Filled at 3.15"); assert s is None or s.parse_confidence=="low"

class TestPaper:
    def test_is_tradeable(self):
        s = parse("STO TQQQ 60 Put @ 3.15"); assert s and s.is_tradeable and s.paper_entry==3.15 and s.paper_status=="open"
    def test_summary(self):
        s = parse("STO TQQQ 60 Put @ 3.15"); assert "STO" in s.summary() and "TQQQ" in s.summary()
