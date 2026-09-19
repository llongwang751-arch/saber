"""AsyncMemoryWriter 生命周期与队列语义的确定性回归测试。"""

import threading

from internal.agent.memory_writer import AsyncMemoryWriter


def test_flush_waits_for_all_preceding_jobs():
    writer = AsyncMemoryWriter()
    task_started = threading.Event()
    release_task = threading.Event()
    flush_finished = threading.Event()
    result = []

    def blocked_job():
        task_started.set()
        assert release_task.wait(1.0)
        result.append("job")

    writer.submit(blocked_job)
    assert task_started.wait(1.0)

    flush_result = []

    def run_flush():
        flush_result.append(writer.flush(timeout=1.0))
        flush_finished.set()

    flush_thread = threading.Thread(target=run_flush)
    flush_thread.start()
    assert not flush_finished.wait(0.05)

    release_task.set()
    flush_thread.join(1.0)

    assert not flush_thread.is_alive()
    assert flush_result == [True]
    assert result == ["job"]
    assert writer.stop(timeout=1.0) is True


def test_stop_drains_already_queued_jobs_in_fifo_order():
    writer = AsyncMemoryWriter()
    first_started = threading.Event()
    release_first = threading.Event()
    stop_finished = threading.Event()
    executed = []

    def first_job():
        first_started.set()
        assert release_first.wait(1.0)
        executed.append(1)

    assert writer.submit(first_job) is True
    assert writer.submit(lambda: executed.append(2)) is True
    assert writer.submit(lambda: executed.append(3)) is True
    assert first_started.wait(1.0)

    stop_result = []

    def run_stop():
        stop_result.append(writer.stop(timeout=1.0))
        stop_finished.set()

    stop_thread = threading.Thread(target=run_stop)
    stop_thread.start()
    assert not stop_finished.wait(0.05)

    release_first.set()
    stop_thread.join(1.0)

    assert not stop_thread.is_alive()
    assert stop_result == [True]
    assert executed == [1, 2, 3]
    assert not writer._worker.is_alive()


def test_failing_job_does_not_block_later_jobs():
    writer = AsyncMemoryWriter()
    executed = []

    def failing_job():
        raise RuntimeError("expected test failure")

    assert writer.submit(failing_job) is True
    assert writer.submit(lambda: executed.append("after-error")) is True

    assert writer.flush(timeout=1.0) is True
    assert executed == ["after-error"]
    assert writer.stop(timeout=1.0) is True


def test_submit_and_flush_are_rejected_after_stop():
    writer = AsyncMemoryWriter()
    executed = []

    assert writer.stop(timeout=1.0) is True
    assert writer.submit(lambda: executed.append("too-late")) is False
    assert writer.flush(timeout=0.01) is False
    assert executed == []

    # stop 是幂等的，已退出线程可再次 join。
    assert writer.stop(timeout=1.0) is True
