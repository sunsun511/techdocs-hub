from tools.restart_check import check_restart


def test_real_http_process_restart_and_inflight_crash_recovery(tmp_path):
    report = check_restart(tmp_path / "restart.json")
    assert report["passed"] == 3
