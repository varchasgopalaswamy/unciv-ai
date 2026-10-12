package uncivai

import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.LongAdder

/** Bounded, payload-free measurements. Recording timing never creates a checkpoint or does I/O. */
internal object HostTimings {
    private class Counter {
        val calls = LongAdder()
        val nanos = LongAdder()
        val maximum = AtomicLong()
    }
    private val counters = ConcurrentHashMap<String, Counter>()

    fun record(name: String, nanos: Long) {
        val counter = counters.computeIfAbsent(name) { Counter() }
        counter.calls.increment()
        counter.nanos.add(nanos)
        counter.maximum.accumulateAndGet(nanos, ::maxOf)
    }

    inline fun <T> measure(name: String, block: () -> T): T {
        val started = System.nanoTime()
        try {
            return block()
        } finally {
            record(name, System.nanoTime() - started)
        }
    }

    fun snapshot() = counters.mapValues { (_, value) -> obj("calls" to value.calls.sum(), "elapsedMs" to value.nanos.sum() / 1_000_000, "maxMs" to value.maximum.get() / 1_000_000) }
}
