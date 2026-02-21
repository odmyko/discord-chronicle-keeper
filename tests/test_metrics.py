from chronicle_keeper.metrics import RuntimeMetrics


def test_runtime_metrics_observe_and_snapshot():
    metrics = RuntimeMetrics()
    metrics.observe("asr_transcribe", 0.5, True)
    metrics.observe("asr_transcribe", 0.2, False)

    snapshot = metrics.snapshot()
    asr = snapshot["asr_transcribe"]
    assert asr["calls"] == 2
    assert asr["errors"] == 1
    assert round(asr["total_latency_s"], 6) == 0.7
    assert asr["max_latency_s"] == 0.5
    assert round(asr["avg_latency_s"], 6) == 0.35
