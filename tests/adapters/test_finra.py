"""ADR 0019 FINRA adapter and store (fake HTTP)."""

from datetime import date
from types import SimpleNamespace

import pytest

from eventtrade.adapters import finra
from eventtrade.adapters.shortvol_store import ShortVolStore

TXT = ("Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
       "20180102|AAA|100|0|300|Q\n20180102|BBB|10|0|40|Q\n")


class _Sess:
    def __init__(self, by_fac):
        self.by_fac, self.calls = by_fac, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        for fac, txt in self.by_fac.items():
            if f"/{fac}shvol" in url:
                return SimpleNamespace(status_code=200, text=txt)
        return SimpleNamespace(status_code=403, text="")


def test_day_sums_facilities_and_filters_symbols(tmp_path):
    s = _Sess({"FNSQ": TXT, "FNYX": TXT.replace("|100|", "|50|")})
    c = finra.FinraClient(session=s, sleep=lambda t: None, clock=lambda: 0.0)
    df = finra.day_short_volume(c, date(2018, 1, 2), {"AAA"})
    assert len(s.calls) == 4
    assert df.to_dict("records") == [{"date": "2018-01-02", "symbol": "AAA", "short": 150.0,
                                      "total": 600.0, "n_fac": 2}]
    st = ShortVolStore(tmp_path / "sv.sqlite", date(2025, 4, 1))
    st.init()
    assert st.write_day(date(2018, 1, 2), df) == 1 and st.done() == {"2018-01-02"}
    with pytest.raises(ValueError, match="hold-out"):
        st.write_day(date(2025, 4, 1), df)
    empty = finra.day_short_volume(finra.FinraClient(session=_Sess({}), sleep=lambda t: None,
                                                     clock=lambda: 0.0), date(2018, 1, 3), None)
    assert empty.empty
