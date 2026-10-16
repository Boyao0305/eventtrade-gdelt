"""ADR 0017 reading rules (amendments 2, 8): same horizon in both periods."""

from eventtrade.cli import pipelines_response as pr


def _note(ev, s, c):
    def tri(sig):
        return [(0.1 if x == 1 else -0.2 if x == -1 else -0.1) for x in sig], \
               [(0.2 if x == 1 else -0.1 if x == -1 else 0.1) for x in sig]

    out = {"curves": {}}
    for tag, sig in (("EV", ev), ("S", s), ("C", c)):
        lo, hi = tri(sig)
        out["curves"][f"{tag}_lo"], out["curves"][f"{tag}_hi"] = lo, hi
    return out


def _run(a, b):
    res = {("X", "A"): a, ("X", "B"): b}
    pr._reading(res, "X", [1, 2])
    return res[("X", "A")]["reading"]


def test_reading_rules():
    zero = [0, 0]
    assert _run(_note([1, 0], zero, zero), _note([1, 0], zero, zero)) == \
        "directionless (volatility)"
    assert _run(_note([1, 0], zero, zero), _note([0, 1], zero, zero)) == "no response"
    assert _run(_note(zero, [1, 0], zero), _note(zero, [1, 0], zero)) == "directional"
    assert _run(_note(zero, [1, 0], zero), _note(zero, [0, 1], zero)) == "no response"
    assert _run(_note([1, 0], zero, [-1, 0]), _note([1, 0], zero, [-1, 0])) == "directional"
    assert _run(_note([1, 0], [1, 0], zero), _note([1, 0], [-1, 0], zero)) == \
        "moves prices (direction unstable across periods)"
    assert _run({"insufficient": True}, _note(zero, zero, zero)) == "insufficient"
