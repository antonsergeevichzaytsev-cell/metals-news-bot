import facts


def test_money_billion():
    f = facts.extract_facts("This $2.7 billion project at Almalyk")
    assert f["money_musd"] == [2700.0]


def test_money_currency_prefixes():
    assert facts.extract_facts("C$45M budget increase")["money_musd"] == [45.0]
    assert facts.extract_facts("US$450 million initial capital")["money_musd"] == [450.0]


def test_money_multiple_sorted_desc():
    f = facts.extract_facts("$3.7 billion total, $1.2 billion at Navoi, $2.5 billion at Almalyk")
    assert f["money_musd"] == [3700.0, 2500.0, 1200.0]


def test_tonnage_units():
    f = facts.extract_facts("900,000 tonnes of concentrate from 60 million tonnes of ore")
    vals = [t["value"] for t in f["tonnage"]]
    assert 900000.0 in vals and 60000000.0 in vals


def test_recovery_requires_context():
    # процент без контекста извлечения — не факт, а шум
    assert facts.extract_facts("Agreements to Acquire 100% of the Project") == {}
    assert facts.extract_facts("Revenue increased 20% year over year") == {}


def test_recovery_with_scale_bench():
    f = facts.extract_facts("bench-scale testwork returning 92.4% copper recovery")
    assert f["percent"] == [92.4]
    assert f["test_scale"] == "bench-scale"


def test_recovery_scale_missing_is_flagged():
    f = facts.extract_facts("achieved 88% recovery from tailings")
    assert f["test_scale"] == "НЕ УКАЗАН"


def test_horizon():
    f = facts.extract_facts("commissioning pushed from H1 2027 to H2 2028")
    assert f["horizon"] == ["H1 2027", "H2 2028"]


def test_empty_is_normal():
    assert facts.extract_facts("Company appoints new director") == {}
    assert facts.extract_facts("") == {}


def test_compare_money_shift():
    hist = [{"company": "X", "project": "P", "ts": "2026-07-20T00:00:00+00:00",
             "facts": {"money_musd": [100.0]}}]
    notes = facts.compare_facts("X", "P", {"money_musd": [130.0]}, hist)
    assert notes and "100" in notes[0] and "130" in notes[0]


def test_compare_ignores_small_drift():
    hist = [{"company": "X", "project": "P", "ts": "2026-07-20T00:00:00+00:00",
             "facts": {"money_musd": [100.0]}}]
    assert facts.compare_facts("X", "P", {"money_musd": [102.0]}, hist) == []


def test_compare_schedule_slip():
    hist = [{"company": "X", "project": "P", "ts": "2026-07-20T00:00:00+00:00",
             "facts": {"horizon": ["Q1 2027"]}}]
    notes = facts.compare_facts("X", "P", {"horizon": ["Q4 2027"]}, hist)
    assert notes and "Q1 2027" in notes[0]


def test_compare_no_prior_is_silent():
    assert facts.compare_facts("X", "P", {"money_musd": [100.0]}, []) == []
