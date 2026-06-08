import time

def poll_until(predicate, timeout, interval=2.0):

    start = time.monotonic()
    deadline = start + timeout

    while True:
        done, value = predicate()
        elapsed = time.monotonic() - start

        if done:
            return True, value, elapsed

        if time.monotonic() >= deadline:
            return False, value, elapsed

        time.sleep(interval)
