import itertools

from auditor.findings.score import page_score, run_score
from auditor.findings.severity import axe_severity, probe_severity
from auditor.models import Severity

C, S, M, N = Severity.critical, Severity.serious, Severity.moderate, Severity.minor


def score(*f):
    return page_score(list(f))[0]


def test_empty_is_perfect_and_bounded():
    assert score() == 100
    assert 0 <= score(*[(f"r{i}", C) for i in range(500)]) <= 100


def test_one_critical_hurts_more_than_ten_distinct_minors():
    assert score(("a", C)) < score(*[(f"m{i}", N) for i in range(10)])


def test_known_values():
    assert score(("a", C)) == 61  # 100*exp(-25/50)
    assert score(*[(f"m{i}", N) for i in range(10)]) == 82


def test_fixing_any_finding_never_lowers_the_score():
    base = [("a", C), ("b", S), ("b", S), ("c", M), ("d", N), ("d", N), ("d", N)]
    s0 = score(*base)
    for i in range(len(base)):
        assert score(*(base[:i] + base[i + 1:])) >= s0


def test_every_subset_is_monotonic():
    items = [("a", C), ("b", S), ("c", M), ("d", N), ("b", S)]
    for r in range(len(items) + 1):
        for sub in itertools.combinations(range(len(items)), r):
            full = score(*[items[i] for i in sub])
            for drop in sub:
                assert score(*[items[i] for i in sub if i != drop]) >= full


def test_repeats_have_diminishing_returns():
    one, two, hundred = score(("a", S)), score(("a", S), ("a", S)), score(*[("a", S)] * 100)
    assert one > two > hundred > score(*[(f"r{i}", S) for i in range(100)])


def test_breakdown_lists_each_rule_once():
    _, bd = page_score([("a", C), ("a", S), ("b", N)])
    assert [r["rule"] for r in bd["rules"]] == ["a", "b"]
    assert bd["rules"][0]["severity"] == "critical" and bd["rules"][0]["instances"] == 2


def test_run_score_shows_worst_page():
    r = run_score([100, 100, 20])
    assert r == {"score": 73, "worst": 20}
    assert run_score([]) == {"score": 100, "worst": 100}


def test_severity_mapping():
    assert axe_severity("critical") == C and axe_severity(None) == M
    assert probe_severity("keyboard_trap", "trapped_elements") == C
    assert probe_severity("unknown", "thing") == M
