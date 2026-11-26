from civ_agent.local_performance import memory_usage


def test_memory_counters_include_process_and_container_without_process_content(tmp_path):
    status = tmp_path / "status"
    status.write_text("Name:\tprivate-process-name\nVmRSS:\t4096 kB\nVmHWM:\t8192 kB\n")
    (tmp_path / "memory.current").write_text("16777216\n")
    (tmp_path / "memory.peak").write_text("33554432\n")
    (tmp_path / "memory.max").write_text("4294967296\n")
    assert memory_usage(status=status, cgroup=tmp_path) == {
        "residentBytes": 4194304,
        "peakResidentBytes": 8388608,
        "cgroupCurrentBytes": 16777216,
        "cgroupPeakBytes": 33554432,
        "cgroupLimitBytes": 4294967296,
    }


def test_memory_counters_allow_unavailable_procfs_and_unlimited_cgroup(tmp_path):
    (tmp_path / "memory.max").write_text("max\n")
    assert memory_usage(status=tmp_path / "absent", cgroup=tmp_path) == {}
