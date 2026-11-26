from scripts.get_optionable import (
    build_au_rows,
    build_us_rows,
    norm,
    parse_asx_master,
    parse_cboe_dir,
    parse_cboe_weeklys,
    parse_occ,
)

OCC = ("ACME  \tACME  \tAcme Corp                     \t\n"
       "1ACME \tACME  \tAcme Corp (AMER/FLEX)         \t\n"
       "BRKB  \tBRKB  \tBerkshire Hathaway Inc. - Cl B\t\n"
       "FOO   \tFOO   \tFoo Holdings ADR              \t\n"
       "ETFX  \tETFX  \tSome ETF                      \t\n")
CBOE = ('Company Name, Stock Symbol, DPM Name, Post/Station, Global Trading Hours DPM\n'
        '"ACME CORP","ACME","Some LLC","8/1","-"\n'
        '"CBOE ONLY INC","ONLY","Some LLC","8/1","-"\n')
WEEKLYS = ('List of Available Weekly Options\n'
           '"Standard","11/27/26","","",""\n'
           '"ACME","Acme Corp","x"\n')
EODHD_US = [{"Code": "ACME", "Name": "Acme Corp", "Exchange": "NYSE", "Type": "Common Stock"},
            {"Code": "BRK-B", "Name": "Berkshire Hathaway Inc", "Exchange": "NYSE",
             "Type": "Common Stock"},
            {"Code": "FOO", "Name": "Foo Holdings", "Exchange": "NASDAQ", "Type": "Common Stock"},
            {"Code": "ETFX", "Name": "Some ETF", "Exchange": "NYSE ARCA", "Type": "ETF"},
            {"Code": "ONLY", "Name": "Cboe Only", "Exchange": "NYSE", "Type": "Common Stock"}]


def test_parse_us_sources():
    assert parse_occ(OCC) == {"ACME": "Acme Corp", "BRKB": "Berkshire Hathaway Inc. - Cl B",
                              "FOO": "Foo Holdings ADR", "ETFX": "Some ETF"}
    assert parse_cboe_dir(CBOE) == {"ACME": "ACME CORP", "ONLY": "CBOE ONLY INC"}
    assert parse_cboe_weeklys(WEEKLYS) == {"ACME"}
    assert norm("BRK.B") == norm("BRK-B") == "BRKB"


def test_us_rule_keeps_occ_common_stock_not_adr():
    rows = {r["symbol"]: r for r in build_us_rows(parse_occ(OCC), parse_cboe_dir(CBOE),
                                                   parse_cboe_weeklys(WEEKLYS), EODHD_US)}
    assert {s for s, r in rows.items() if r["eligible"]} == {"ACME", "BRKB"}
    assert rows["BRKB"]["code"] == "BRK-B"          # class share matched across spellings
    assert rows["FOO"]["adr"] == 1                  # ADR by OCC's name
    assert rows["ETFX"]["eligible"] == 0            # not common stock
    assert rows["ONLY"]["eligible"] == 0            # Cboe lists it, OCC doesn't
    assert rows["ACME"]["has_weeklies"] == 1 and rows["BRKB"]["has_weeklies"] == 0


ASX = ('"BusDate","Market","ASXCode","Underlying","OptType","ExpDate","Strike","Style",'
       '"ContractSize","DerivativeProduct","ProductType","Category","ExpDateRaw","ListingFrequency"\n'
       '"26/11/2026","ASX","X1","ABC","C","17/12/2026",1,"A",100,"ABC","OS","",x,"M"\n'
       '"26/11/2026","ASX","X2","ABC","P","17/12/2026",1,"A",100,"ABC","OS","",x,"M"\n'
       '"26/11/2026","ASX","X3","ABC","C","24/12/2026",1,"A",100,"ABC","OS","",x,"W1"\n'
       '"26/11/2026","ASX","X4","ABC","C","18/03/2027",1,"A",100,"ABC","OS","",x,"M"\n'
       '"26/11/2026","ASX","X5","ABCN","C","17/12/2026",1,"A",76,"AB9","OS","",x,"M"\n'
       '"26/11/2026","ASX","X6","EFG","C","17/12/2026",1,"E",100,"EFGL","LE","",x,"M"\n'
       '"26/11/2026","ASX","X7","ETF","C","17/12/2026",1,"E",100,"ETF","OS","",x,"M"\n')
EODHD_AU = [{"Code": "ABC", "Name": "Abc Ltd", "Type": "Common Stock"},
            {"Code": "EFG", "Name": "Efg Ltd", "Type": "Common Stock"},
            {"Code": "ETF", "Name": "Some ETF", "Type": "ETF"}]


def test_au_underlyings():
    bus, rows = parse_asx_master(ASX)
    assert bus == "2026-11-26"
    out = {(r["underlying"], r["product_type"]): r for r in build_au_rows(rows, EODHD_AU)}
    abc = out[("ABC", "OS")]
    got = (abc["n_series"], abc["n_expiries"], abc["has_weeklies"], abc["eligible"])
    assert got == (4, 3, 1, 1)
    assert abc["avg_days_between"] == round((7 + 84) / 2, 1)
    assert out[("ABCN", "OS")]["code"] == "ABC" and out[("ABCN", "OS")]["adjusted_class"] == 1
    assert out[("ABCN", "OS")]["eligible"] == 0     # its stock already counts
    assert out[("EFG", "LE")]["eligible"] == 0      # LEPOs only
    assert out[("ETF", "OS")]["eligible"] == 0      # not common stock
