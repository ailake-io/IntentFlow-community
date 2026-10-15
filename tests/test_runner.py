

def test_watch_survives_a_dead_database_connection(monkeypatch, tmp_path):
    """A database restart kills the scheduler's connection; the cycle's error handler must reopen it, not crash."""
    from intentflow import runner
    from intentflow.store import Store
    st = Store(str(tmp_path / "w.db"))
    monkeypatch.setattr(runner, "collect_runs", lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(st, "audit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("connection already closed")))
    assert runner.watch(st, interval_s=0, iterations=3, sleep=lambda s: None) == 3
